"""Bulk import recipes from the RecipeNLG ``full_dataset.csv`` into the
Supabase ``programs`` table.

Skips image generation entirely — covers are produced separately by
``rhylthyme_importers.generate_thumbnails`` (which finds programs whose
``metadata.thumbnail`` is unset and fills them via fal.ai FLUX).

Strategy
--------
1. Stream rows from the CSV (it's 2.3 GB — never load it all in memory).
2. Filter to the host allowlist (default: green-tier sites that have
   reliable JSON-LD, though we don't need to refetch them — the CSV
   already contains title/ingredients/directions).
3. For each row:
   * Parse ingredient lines into ``{name, measure}`` pairs using the
     RecipeNLG ``NER`` column as the canonical name.
   * Build a Rhylthyme program JSON via
     ``rhylthyme_server._universal.program_builder.build_program``.
   * Stamp ``metadata.importSource = 'RecipeNLG'`` and
     ``metadata.sourceUrl`` for provenance.
4. Dedup against rows already imported (queries the canonical owner's
   ``metadata->>'sourceUrl'`` once at start, holds the set in memory).
5. Bulk insert into ``programs`` in batches of up to 50 rows.

Env vars (loaded from rhylthyme-server/.env):
- SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY
- RHYLTHYME_RECIPES_USER_ID (optional, defaults to canonical UID)

Usage
-----
::

    # Sanity: 5 recipes from food.com only
    python -m rhylthyme_importers.bulk_recipenlg \\
        --csv /Volumes/My12tb/recipenlg/dataset/full_dataset.csv \\
        --hosts food.com --limit 5

    # Green-tier batch
    python -m rhylthyme_importers.bulk_recipenlg \\
        --csv /Volumes/My12tb/recipenlg/dataset/full_dataset.csv \\
        --hosts food.com,allrecipes.com,foodnetwork.com,cookpad.com,\\
recipeland.com,cooking.nytimes.com,seriouseats.com \\
        --limit 50000

    # Dry-run: count what WOULD be inserted but don't write
    python -m rhylthyme_importers.bulk_recipenlg --csv ... --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

# Allow running this script from a checkout — add the web app's src to sys.path
# so we can reuse the universal importer's program-builder + types.
_THIS_DIR = Path(__file__).resolve().parent
_IMPORTERS_ROOT = _THIS_DIR.parent.parent  # rhylthyme-importers/
_REPO_ROOT = _IMPORTERS_ROOT.parent  # rhylthyme-split/
_WEB_SRC = _REPO_ROOT / "rhylthyme-server" / "src"
if _WEB_SRC.exists() and str(_WEB_SRC) not in sys.path:
    sys.path.insert(0, str(_WEB_SRC))

from rhylthyme_server._universal.program_builder import build_program  # noqa: E402
from rhylthyme_server._universal.types import (  # noqa: E402
    ExtractedRecipe,
    ExtractedStep,
    Ingredient,
)

from .cooklang_federation import (  # noqa: E402
    DEFAULT_RECIPES_USER_ID,
    _load_dotenv_if_present,
)


# ---------- Ingredient parsing -----------------------------------------------

# Map of short forms / pluralizations to a canonical unit. Items mapping to
# the empty string are recognized as a "unit" but produce no string in the
# measure (e.g. size adjectives like "small" / "medium" / "large").
_UNIT_MAP = {
    "c": "cup", "c.": "cup", "cup": "cup", "cups": "cup",
    "tbsp": "tbsp", "tbsp.": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "T.": "tbsp", "T": "tbsp",
    "tsp": "tsp", "tsp.": "tsp", "teaspoon": "tsp", "teaspoons": "tsp", "t.": "tsp",
    "oz": "oz", "oz.": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lb.": "lb", "lbs": "lb", "lbs.": "lb",
    "pound": "lb", "pounds": "lb",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "ml": "ml", "l": "l", "liter": "l", "liters": "l", "litre": "l",
    "pkg": "pkg", "pkg.": "pkg", "package": "pkg", "packages": "pkg",
    "can": "can", "cans": "can", "jar": "jar", "jars": "jar",
    "box": "box", "boxes": "box",
    "pinch": "pinch", "dash": "dash",
    "clove": "clove", "cloves": "clove",
    "slice": "slice", "slices": "slice", "piece": "piece", "pieces": "piece",
    "sprig": "sprig", "sprigs": "sprig",
    "bunch": "bunch", "bunches": "bunch",
    "head": "head", "heads": "head",
    "stalk": "stalk", "stalks": "stalk",
    # Size adjectives — recognized, then discarded so the measure stays
    # numeric ("1 small onion" → measure="1", name="onion").
    "small": "", "medium": "", "large": "",
}

# Matches a leading number, possibly a fraction or mixed number ("2 1/2").
_FRAC = r"\d+(?:/\d+)?(?:\s+\d+/\d+)?"
_QTY_RE = re.compile(r"^\s*(" + _FRAC + r")\s+(.*)", re.DOTALL)


def parse_ingredient(raw_line: str, canonical_name: str | None = None) -> tuple[Ingredient, str]:
    """Split a raw ingredient line into a (name, measure) pair.

    Returns:
        (Ingredient instance, measure string).

    Strategy:
      1. Strip a leading number ("1", "2 1/2", "3/4").
      2. If the following token matches a known unit alias, consume it.
      3. Use the RecipeNLG NER canonical name as the final name when
         present — it's already a clean noun phrase ("cream of mushroom
         soup") that strips brand names and parenthetical size notes
         from the original line.
    """
    s = (raw_line or "").strip()
    qty = ""
    unit = ""
    rest = s
    m = _QTY_RE.match(s)
    if m:
        qty = m.group(1).strip()
        rest = m.group(2).strip()
        parts = rest.split(None, 1)
        if parts:
            tok = parts[0].lower().rstrip(",")
            if tok in _UNIT_MAP:
                unit = _UNIT_MAP[tok]
                rest = parts[1] if len(parts) > 1 else ""
    name = (canonical_name or "").strip() or rest or s
    measure = (qty + (" " + unit if unit else "")).strip()
    return Ingredient(name=name, quantity=measure, unit=unit), measure


# ---------- Host parsing -----------------------------------------------------

def host_of(link: str) -> str:
    if not link:
        return ""
    if not link.startswith("http"):
        link = "http://" + link
    h = urlparse(link).netloc.lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def absolute_url(link: str) -> str:
    if not link:
        return ""
    return link if link.startswith("http") else "http://" + link


# ---------- Supabase helpers -------------------------------------------------

def _sb_headers(key: str, prefer: str | None = None) -> dict[str, str]:
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _normalize_title(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for dedup."""
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fetch_existing_meta(
    sb_url: str, sb_key: str, user_id: str, *, fetch_titles: bool
) -> tuple[set[str], set[str]]:
    """Return (sourceUrls, normalized_titles) already imported for user_id.

    ``fetch_titles`` is False by default since loading every program name
    (one network round trip per 1k rows) is wasted work if title-dedup
    isn't enabled. Source URLs come from program_json->metadata which we
    pull cheaply with a single PostgREST JSON-path projection."""
    sources: set[str] = set()
    titles: set[str] = set()
    offset = 0
    page_size = 1000
    select = "id,name,program_json->metadata->>sourceUrl" if fetch_titles \
        else "id,program_json->metadata->>sourceUrl"
    print(
        f"Fetching existing {'titles + ' if fetch_titles else ''}source URLs for dedup...",
        flush=True,
    )
    while True:
        params = {
            "user_id": f"eq.{user_id}",
            "select": select,
            "limit": str(page_size),
            "offset": str(offset),
        }
        url = f"{sb_url}/rest/v1/programs?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=_sb_headers(sb_key)),
                timeout=60,
            ) as resp:
                rows = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"  HTTPError pulling existing programs: {e}", flush=True)
            print(f"  body: {e.read().decode('utf-8', errors='replace')[:200]}", flush=True)
            raise
        if not rows:
            break
        for r in rows:
            v = r.get("sourceUrl") or r.get("program_json->metadata->>sourceUrl")
            if v:
                sources.add(v)
            if fetch_titles:
                t = _normalize_title(r.get("name") or "")
                if t:
                    titles.add(t)
        offset += len(rows)
        if len(rows) < page_size:
            break
        if offset % 10000 == 0:
            extra = f", {len(titles)} titles" if fetch_titles else ""
            print(
                f"  ...scanned {offset} rows, {len(sources)} URLs{extra}",
                flush=True,
            )
    extra = f", {len(titles)} normalized titles" if fetch_titles else ""
    print(f"  done: {len(sources)} source URLs{extra} already imported", flush=True)
    return sources, titles


# Quality thresholds — discovered by spot-checking CSV samples. Tuned to
# admit ~70% of food.com / allrecipes.com / foodnetwork.com rows and
# rejecting the obvious garbage (cookbook compilations with one-line
# directions, recipes with just a title and "stir together", etc.).
QUALITY_MIN_INGREDIENTS = 3
QUALITY_MAX_INGREDIENTS = 25
QUALITY_MIN_STEPS = 2
QUALITY_MAX_STEPS = 15
QUALITY_MIN_AVG_STEP_CHARS = 25
QUALITY_MIN_TOTAL_DIR_CHARS = 150
QUALITY_TITLE_BAD_RE = re.compile(
    r"^(recipe(\s*#?\s*\d+)?|untitled|no\s*name|tbd|test)\s*$",
    re.IGNORECASE,
)


def passes_quality(title: str, ings: list, dirs: list) -> bool:
    """Reject CSV rows that look like broken/stub recipes."""
    t = (title or "").strip()
    if not (3 <= len(t) <= 100):
        return False
    # All-caps or all-numeric titles read as junk in the UI.
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.9:
        return False
    if QUALITY_TITLE_BAD_RE.match(t):
        return False
    n_ings = len([i for i in ings if isinstance(i, str) and i.strip()])
    n_steps = len([d for d in dirs if isinstance(d, str) and d.strip()])
    if not (QUALITY_MIN_INGREDIENTS <= n_ings <= QUALITY_MAX_INGREDIENTS):
        return False
    if not (QUALITY_MIN_STEPS <= n_steps <= QUALITY_MAX_STEPS):
        return False
    total_chars = sum(len(d) for d in dirs if isinstance(d, str))
    if total_chars < QUALITY_MIN_TOTAL_DIR_CHARS:
        return False
    if total_chars / n_steps < QUALITY_MIN_AVG_STEP_CHARS:
        return False
    return True


def parse_quota_arg(spec: str) -> dict[str, int]:
    """Parse ``host=N,host=N`` into ``{host: N}``."""
    quotas: dict[str, int] = {}
    if not spec:
        return quotas
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        host, _, cap = piece.partition("=")
        try:
            quotas[host.strip().lower()] = int(cap.strip())
        except ValueError:
            continue
    return quotas


def _bulk_insert(sb_url: str, sb_key: str, batch: list[dict[str, Any]]) -> tuple[int, str]:
    """POST a batch of rows. Returns (inserted_count, error_message)."""
    if not batch:
        return 0, ""
    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        f"{sb_url}/rest/v1/programs",
        data=body,
        headers=_sb_headers(sb_key, prefer="return=minimal"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
        return len(batch), ""
    except urllib.error.HTTPError as e:
        return 0, f"HTTP {e.code}: " + e.read().decode("utf-8", errors="replace")[:240]
    except Exception as e:
        return 0, str(e)[:240]


# ---------- Row → program conversion -----------------------------------------

def _safe_json(s: str) -> list[Any]:
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def build_row_payload(
    csv_row: dict[str, str],
    user_id: str,
    default_step_seconds: int,
) -> dict[str, Any] | None:
    title = (csv_row.get("title") or "").strip()
    ings_raw = _safe_json(csv_row.get("ingredients") or "")
    dirs = _safe_json(csv_row.get("directions") or "")
    ner = _safe_json(csv_row.get("NER") or "")
    link = absolute_url(csv_row.get("link") or "")
    if not title or not ings_raw or not dirs:
        return None

    parsed = [
        parse_ingredient(raw, ner[i] if i < len(ner) else None)
        for i, raw in enumerate(ings_raw)
    ]
    ingredients = [p[0] for p in parsed]
    measures = [p[1] for p in parsed]
    steps = [
        ExtractedStep(text=d, duration_seconds=default_step_seconds, track_hint="main")
        for d in dirs
        if isinstance(d, str) and d.strip()
    ]
    if not steps:
        return None

    recipe = ExtractedRecipe(
        name=title,
        description=f"Recipe from {link}" if link else "",
        ingredients=ingredients,
        steps=steps,
        total_time_seconds=default_step_seconds * len(steps),
        language="en",
    )
    program = build_program(recipe, source_url=link)

    md = program.setdefault("metadata", {})
    md["importSource"] = "RecipeNLG"
    if link:
        md["sourceUrl"] = link
    # Renderer-friendly ingredient shape: {name, measure}.
    md["ingredients"] = [
        {"name": ing.name, "measure": measure}
        for ing, measure in zip(ingredients, measures)
    ]

    return {
        "user_id": user_id,
        "name": title,
        "program_json": program,
        "schema_version": program.get("schemaVersion", "0.2.0-alpha"),
        "environment": "kitchen",
        "is_public": True,
    }


# ---------- Streaming over the CSV -------------------------------------------

def stream_filtered_rows(
    csv_path: Path,
    hosts: set[str] | None,
    already_urls: set[str],
    already_titles: set[str],
    limit: int | None,
    *,
    quality: bool,
    host_quotas: dict[str, int],
) -> Iterator[dict[str, str]]:
    """Yield CSV rows that pass host, dedup, quality, and quota gates.
    Honors ``--limit`` so we cap at most N candidates overall."""
    csv.field_size_limit(10_000_000)
    emitted = 0
    per_host_seen: dict[str, int] = {}
    # Track normalized titles seen *within this run* to suppress
    # near-duplicate CSV rows ("Mom's Chocolate Chip Cookies" appearing
    # in twelve different cookbook compilations).
    seen_titles_this_run: set[str] = set()
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            link = absolute_url(row.get("link", ""))
            host = host_of(link)
            if hosts is not None and host not in hosts:
                continue
            # Per-host quota check (0 means "exclude this host").
            if host_quotas:
                cap = host_quotas.get(host)
                if cap is not None and per_host_seen.get(host, 0) >= cap:
                    continue
            if link and link in already_urls:
                continue
            title = (row.get("title") or "").strip()
            norm = _normalize_title(title)
            if not norm:
                continue
            if norm in already_titles or norm in seen_titles_this_run:
                continue
            if quality:
                ings = _safe_json(row.get("ingredients") or "")
                dirs = _safe_json(row.get("directions") or "")
                if not passes_quality(title, ings, dirs):
                    continue
            seen_titles_this_run.add(norm)
            per_host_seen[host] = per_host_seen.get(host, 0) + 1
            yield row
            emitted += 1
            if limit is not None and emitted >= limit:
                return


# ---------- CLI entry point --------------------------------------------------

# Green-tier defaults — the hosts our earlier probe confirmed return
# usable JSON-LD recipes. (For CSV-direct ingestion we don't refetch the
# source page, but green-tier hosts also tend to have the cleanest text
# in the CSV.)
DEFAULT_HOSTS = (
    "food.com,allrecipes.com,foodnetwork.com,cookpad.com,"
    "recipeland.com,cooking.nytimes.com,seriouseats.com"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-import recipes from RecipeNLG full_dataset.csv "
                    "into the Supabase programs table.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("/Volumes/My12tb/recipenlg/dataset/full_dataset.csv"),
        help="Path to the RecipeNLG full_dataset.csv",
    )
    parser.add_argument(
        "--hosts",
        default=DEFAULT_HOSTS,
        help="Comma-separated host allowlist. Use 'all' to skip filtering.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum candidate rows to consider (post-host-filter, pre-dedup).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Programs per Supabase POST batch.",
    )
    parser.add_argument(
        "--default-step-seconds",
        type=int,
        default=180,
        help="Per-step duration assigned when the CSV row has no timing info.",
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("RHYLTHYME_RECIPES_USER_ID", DEFAULT_RECIPES_USER_ID),
        help="UUID of the Supabase auth user that will own the inserted rows.",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Skip the existing-sourceUrl lookup (faster startup, may insert duplicates).",
    )
    parser.add_argument(
        "--dedup-titles",
        action="store_true",
        help="Also load every existing program's name and skip rows whose "
             "normalized title already exists. Adds ~30s of startup time on "
             "50k-row owners.",
    )
    parser.add_argument(
        "--quality",
        action="store_true",
        help="Reject rows whose ingredients/steps/title look like stubs or "
             "broken scrapes (see passes_quality() for thresholds).",
    )
    parser.add_argument(
        "--max-per-host",
        default="",
        help="Per-host cap, comma-separated. Example: "
             "'food.com=1000,foodnetwork.com=5000'. Use 0 to exclude.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payloads but do not write to Supabase.",
    )
    args = parser.parse_args(argv)

    _load_dotenv_if_present()
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not sb_url or not sb_key:
        sys.exit("SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required")

    if not args.csv.exists():
        sys.exit(f"CSV not found at {args.csv}")

    hosts_raw = (args.hosts or "").strip().lower()
    hosts: set[str] | None
    if hosts_raw == "all":
        hosts = None
        print("Host filter: ALL", flush=True)
    else:
        hosts = {h.strip() for h in hosts_raw.split(",") if h.strip()}
        print(f"Host filter: {sorted(hosts)}", flush=True)

    if args.no_dedup:
        already_urls: set[str] = set()
        already_titles: set[str] = set()
    else:
        already_urls, already_titles = _fetch_existing_meta(
            sb_url, sb_key, args.user_id, fetch_titles=args.dedup_titles
        )

    host_quotas = parse_quota_arg(args.max_per_host)
    if host_quotas:
        print(f"Host quotas: {host_quotas}", flush=True)

    t0 = time.time()
    inspected = 0
    converted = 0
    inserted = 0
    failed = 0
    batch: list[dict[str, Any]] = []
    last_err = ""

    print(f"Streaming CSV from {args.csv} ...", flush=True)
    for row in stream_filtered_rows(
        args.csv, hosts, already_urls, already_titles, args.limit,
        quality=args.quality, host_quotas=host_quotas,
    ):
        inspected += 1
        payload = build_row_payload(row, args.user_id, args.default_step_seconds)
        if payload is None:
            failed += 1
            continue
        converted += 1
        if args.dry_run:
            continue
        batch.append(payload)
        if len(batch) >= args.batch_size:
            count, err = _bulk_insert(sb_url, sb_key, batch)
            if err:
                last_err = err
                failed += len(batch) - count
                print(f"  batch error: {err}", flush=True)
            inserted += count
            batch = []
        if inspected % 500 == 0:
            elapsed = time.time() - t0
            rate = inserted / elapsed if elapsed else 0
            print(
                f"  {inspected:>7} candidates inspected · "
                f"{converted:>7} converted · "
                f"{inserted:>7} inserted · "
                f"{failed:>5} failed · "
                f"{rate:>5.1f} rows/s",
                flush=True,
            )

    # Flush remainder.
    if batch and not args.dry_run:
        count, err = _bulk_insert(sb_url, sb_key, batch)
        if err:
            last_err = err
            failed += len(batch) - count
            print(f"  final batch error: {err}", flush=True)
        inserted += count

    elapsed = time.time() - t0
    print(
        f"\nDone in {elapsed:.1f}s\n"
        f"  inspected: {inspected}\n"
        f"  converted: {converted}\n"
        f"  inserted:  {inserted}\n"
        f"  failed:    {failed}\n"
        f"  last err:  {last_err or 'none'}",
        flush=True,
    )
    if not args.dry_run and inserted:
        print("\nNext: generate cover photos for the newly-imported recipes:")
        print("  python -m rhylthyme_importers.generate_thumbnails --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
