"""
Bulk-mine recipes via the ``recipe-scrapers`` library and upload them to
Supabase, owned by the canonical "rhylthyme-recipes" user account.

Default target user: ``80a129d0-2137-4cca-bf31-f21a41aee815``
Override with env ``RHYLTHYME_RECIPES_USER_ID`` or ``--user-id``.

Reproducible workflow
---------------------
::

    # 1. Scrape a list of recipe URLs (one per line in a file, or piped via stdin)
    rhylthyme-import-recipe-scrapers --import-urls urls.txt \\
        --staged-out /tmp/recipe_scrapers_staged.json

    # 2. Review what was staged / skipped
    rhylthyme-import-recipe-scrapers --review /tmp/recipe_scrapers_staged.json

    # 3. Upload (idempotent: skips entries whose source_url is already there)
    rhylthyme-import-recipe-scrapers --upload /tmp/recipe_scrapers_staged.json \\
        --idempotent

Environment variables (for ``--upload``)
- ``SUPABASE_URL``
- ``SUPABASE_SERVICE_ROLE_KEY``
- ``RHYLTHYME_RECIPES_USER_ID`` (optional)

We deliberately reuse the upload helpers from
``cooklang_federation`` so the row layout and idempotency behaviour are
identical for every bulk-import path that targets the recipes account.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlparse

from .recipe_scrapers_importer import RecipeScrapersImporter
# Reuse the well-tested upload pipeline rather than reimplementing it.
from .cooklang_federation import (
    DEFAULT_RECIPES_USER_ID,
    _existing_source_urls,
    _load_dotenv_if_present,
    upload as _upload_staged,
)
from .sitemap_discovery import discover as discover_sitemaps


# A small starter seed so users can dry-run the pipeline without a URL file.
# Each URL is on a recipe-scrapers-supported domain (allrecipes is parser #1
# in the library, BBC Good Food is one of the most reliable feeds).
DEFAULT_SEEDS: List[str] = [
    'https://www.bbcgoodfood.com/recipes/best-spaghetti-bolognese-recipe',
    'https://www.bbcgoodfood.com/recipes/easy-chicken-curry',
    'https://www.bbcgoodfood.com/recipes/banana-bread-recipe',
    'https://www.allrecipes.com/recipe/213742/cheesy-chicken-broccoli-rice-casserole/',
    'https://www.allrecipes.com/recipe/24074/alysons-broccoli-salad/',
    'https://www.simplyrecipes.com/recipes/homemade_pizza/',
    'https://www.simplyrecipes.com/recipes/rotisserie_chicken/',
    'https://www.seriouseats.com/the-best-chocolate-chip-cookies-recipe',
]


# ---------------------------------------------------------------------------
# Quality filter — same intent as cooklang_federation: drop placeholder,
# unparseable, or near-empty recipes before they reach Supabase.
# ---------------------------------------------------------------------------

BAD_NAME_TOKENS = {
    'recipe', 'untitled', 'test', 'example', 'todo', 'tbd', 'wip',
}


def _passes_quality(program: dict) -> tuple[bool, str]:
    name = (program.get('name') or '').strip()
    if not name:
        return False, 'empty name'
    low = name.lower()
    if low in BAD_NAME_TOKENS or low.startswith('untitled'):
        return False, f'placeholder name: {name!r}'
    ingredients = (program.get('metadata') or {}).get('ingredients') or []
    if not ingredients:
        return False, 'no ingredients'
    tracks = program.get('tracks') or []
    n_steps = sum(len(t.get('steps') or []) for t in tracks)
    # Skip recipes that only have the auto-generated single placeholder step
    if n_steps <= 1:
        return False, 'no instruction steps'
    return True, ''


# ---------------------------------------------------------------------------
# Step 1 — scrape a list of URLs into a staged JSON
# ---------------------------------------------------------------------------

def _read_url_list(path: Path | None) -> List[str]:
    if path is None:
        text = sys.stdin.read()
    else:
        text = path.read_text(encoding='utf-8')
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        urls.append(line)
    return urls


class _PerHostRateLimiter:
    """Enforce a minimum interval between requests *to the same host*.

    Lock contention is per-host so workers hitting different sites don't
    serialise on each other.
    """

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._next_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            lk = self._host_locks.get(host)
            if lk is None:
                lk = threading.Lock()
                self._host_locks[host] = lk
            return lk

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._host_lock(host):
            now = time.monotonic()
            wait_until = self._next_at.get(host, 0)
            if wait_until > now:
                time.sleep(wait_until - now)
            self._next_at[host] = time.monotonic() + self.min_interval


def _scrape_one(url: str, importer: RecipeScrapersImporter, limiter: _PerHostRateLimiter):
    if not importer.can_import(url):
        return ('skip', url, 'unsupported domain', None)
    limiter.wait(url)
    result = importer.import_from_url(url)
    if not result.success or not result.program:
        return ('fail', url, result.error or 'scrape failed', None)
    ok, reason = _passes_quality(result.program)
    if not ok:
        return ('filter', url, reason, None)
    return ('ok', url, None, result.program)


def import_urls(urls: Iterable[str], staged_out: Path, *,
                sleep: float = 1.0, workers: int = 8) -> None:
    """Scrape URLs (concurrently if ``workers > 1``) and write a staged JSON file.

    ``sleep`` is the minimum interval *between requests to the same host* —
    workers hitting different hosts run truly in parallel.
    """
    importer = RecipeScrapersImporter()
    limiter = _PerHostRateLimiter(min_interval=sleep)
    urls = list(urls)
    # Shuffle so consecutive workers tend to hit different hosts.
    random.shuffle(urls)

    staged: list[dict] = []
    skipped: list[dict] = []
    n_total = len(urls)
    n_done = 0
    print(f'Scraping {n_total} URLs (workers={workers}, per-host interval={sleep}s) ...')

    if workers <= 1:
        # Serial path — useful for debugging
        for url in urls:
            kind, u, info, prog = _scrape_one(url, importer, limiter)
            n_done += 1
            if kind == 'ok':
                staged.append({'source_url': u, 'program': prog})
                if n_done % 25 == 0:
                    print(f'  [{n_done}/{n_total}] ok={len(staged)} skip={len(skipped)} (last: {prog.get("name","?")})')
            else:
                skipped.append({'url': u, 'reason': info})
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_scrape_one, u, importer, limiter) for u in urls]
            for fut in as_completed(futs):
                try:
                    kind, u, info, prog = fut.result()
                except Exception as e:
                    n_done += 1
                    skipped.append({'url': '<unknown>', 'reason': f'worker exception: {e}'})
                    continue
                n_done += 1
                if kind == 'ok':
                    staged.append({'source_url': u, 'program': prog})
                else:
                    skipped.append({'url': u, 'reason': info})
                if n_done % 10 == 0 or n_done == n_total:
                    last = prog.get('name', '?') if prog else (info or '?')
                    print(f'  [{n_done}/{n_total}] ok={len(staged)} skip={len(skipped)} (last: {last})', flush=True)

    out = {
        'importer': 'recipe-scrapers',
        'staged': staged,
        'skipped': skipped,
        'source_count': n_total,
    }
    staged_out.parent.mkdir(parents=True, exist_ok=True)
    staged_out.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f'\nWrote {len(staged)} staged programs ({len(skipped)} skipped) -> {staged_out}')


# ---------------------------------------------------------------------------
# Step 2 — quick review
# ---------------------------------------------------------------------------

def review(staged_path: Path, *, n: int = 5) -> None:
    data = json.loads(staged_path.read_text())
    staged = data.get('staged') or []
    skipped = data.get('skipped') or []
    print(f'{staged_path}: {len(staged)} staged, {len(skipped)} skipped\n')
    for r in staged[:n]:
        prog = r['program']
        ingredients = (prog.get('metadata') or {}).get('ingredients') or []
        n_steps = sum(len(t.get('steps') or []) for t in prog.get('tracks') or [])
        print(f"  - {prog.get('name')!r}")
        print(f"      url: {r['source_url']}")
        print(f"      ingredients: {len(ingredients)}, steps: {n_steps}")
    if len(staged) > n:
        print(f'  ... ({len(staged) - n} more)')
    if skipped:
        print('\nSample skipped:')
        for r in skipped[:n]:
            print(f"  - {r['reason']}: {r['url']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    _load_dotenv_if_present()

    ap = argparse.ArgumentParser(
        prog='rhylthyme-import-recipe-scrapers',
        description='Mine recipes via recipe-scrapers and upload to Supabase.',
    )
    ap.add_argument('--import-urls', metavar='URLS_FILE', nargs='?', const='-',
                    help='Scrape URLs from this file (one per line). Use "-" or omit value to read stdin.')
    ap.add_argument('--seed', action='store_true',
                    help='Use the built-in starter URL list (smoke test).')
    ap.add_argument('--from-sitemaps', action='store_true',
                    help='Discover URLs from curated recipe-site sitemaps and scrape them.')
    ap.add_argument('--max-per-site', type=int, default=500,
                    help='Per-site URL cap when using --from-sitemaps (default: %(default)s).')
    ap.add_argument('--max-total', type=int, default=5000,
                    help='Global URL cap when using --from-sitemaps (default: %(default)s).')
    ap.add_argument('--review', metavar='STAGED_JSON', help='Show a sample of a staged JSON file.')
    ap.add_argument('--upload', metavar='STAGED_JSON', help='Upload staged programs to Supabase.')
    ap.add_argument('--staged-out', default='/tmp/recipe_scrapers_staged.json',
                    help='Output path for --import-urls / --seed / --from-sitemaps (default: %(default)s).')
    ap.add_argument('--sleep', type=float, default=1.0,
                    help='Min seconds between requests to the same host (default: %(default)s).')
    ap.add_argument('--workers', type=int, default=8,
                    help='Concurrent scraper threads (default: %(default)s).')
    ap.add_argument('--limit', type=int, default=None, help='Max programs for --upload (debug).')
    ap.add_argument('--start', type=int, default=0, help='Index to start --upload from.')
    ap.add_argument('--idempotent', action='store_true',
                    help='Skip programs whose source_url is already in Supabase for the target user.')
    ap.add_argument('--prefilter-supabase', action='store_true',
                    help='Before scraping --from-sitemaps URLs, drop those already in Supabase for the target user.')
    ap.add_argument('--user-id', default=None,
                    help=f'Override target user_id for --upload (default: {DEFAULT_RECIPES_USER_ID}).')
    args = ap.parse_args(argv)

    if args.seed:
        import_urls(DEFAULT_SEEDS, Path(args.staged_out),
                    sleep=args.sleep, workers=args.workers)
        return 0

    if args.from_sitemaps:
        urls = discover_sitemaps(
            max_per_site=args.max_per_site, max_total=args.max_total
        )
        # Dedupe (sitemaps occasionally repeat URLs across files)
        urls = list(dict.fromkeys(urls))
        if not urls:
            sys.exit('Sitemap discovery returned 0 URLs.')

        if args.prefilter_supabase:
            import os
            sb_url = os.environ.get('SUPABASE_URL')
            sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
            target = args.user_id or os.environ.get('RHYLTHYME_RECIPES_USER_ID') or DEFAULT_RECIPES_USER_ID
            if not sb_url or not sb_key:
                sys.exit('--prefilter-supabase requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY')
            print(f'Pre-filtering against existing source URLs for user {target} ...')
            existing = _existing_source_urls(sb_url, sb_key, target)
            print(f'  {len(existing)} existing source URLs in Supabase')
            before = len(urls)
            urls = [u for u in urls if u not in existing]
            print(f'  {before - len(urls)} URLs already uploaded; {len(urls)} new to scrape')

        if not urls:
            print('Nothing new to scrape after pre-filter.')
            return 0
        print(f'\nDiscovered {len(urls)} URLs from sitemaps. Starting scrape ...')
        import_urls(urls, Path(args.staged_out),
                    sleep=args.sleep, workers=args.workers)
        return 0

    if args.import_urls is not None:
        path = None if args.import_urls == '-' else Path(args.import_urls)
        urls = _read_url_list(path)
        if not urls:
            sys.exit('No URLs to scrape (file/stdin was empty).')
        import_urls(urls, Path(args.staged_out),
                    sleep=args.sleep, workers=args.workers)
        return 0

    if args.review:
        review(Path(args.review))
        return 0

    if args.upload:
        _upload_staged(
            Path(args.upload),
            limit=args.limit, start=args.start,
            idempotent=args.idempotent, user_id=args.user_id,
        )
        return 0

    ap.print_help()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
