"""
Backfill the taxonomy columns and ``recipe_tags`` rows for every public
kitchen recipe that doesn't have a category yet.

Runs the :mod:`recipe_classifier` on each row's name + description +
ingredients (extracted from ``program_json``) with a deterministic
``time_bucket`` derived from ``metadata.total_time_minutes``. PATCHes the
6 new columns on the row and upserts the tag join-table rows.

Resumable, idempotent: rows where ``category IS NOT NULL`` are skipped
on subsequent runs. Existing tag rows are preserved across re-runs (we
delete the row's prior tags before inserting the new set, so the table
reflects the latest classification).

CLI: ``rhylthyme-backfill-taxonomy``.
- ``--limit N`` caps the number of rows processed (use 100 for a Phase 1.5
  spot-check before running on the full corpus).
- ``--workers N`` parallel classifier calls. The Anthropic SDK is fine at
  8-16; raise carefully so we don't burn through the rate limit.
- ``--max-cost USD`` aborts when projected spend crosses this threshold.
- ``--dry-run`` prints the result for each row without writing to Supabase.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from . import taxonomy_vocab as vocab
from .cooklang_federation import (
    DEFAULT_RECIPES_USER_ID,
    _load_dotenv_if_present,
)
from .recipe_classifier import (
    DEFAULT_MODEL,
    TaxonomyResult,
    _build_classifier_tool,
    _build_user_prompt,
    _coerce_list,
    _coerce_single,
    _parse_response,
    _SYSTEM_PROMPT,
    _TOOL_NAME,
    classify,
)


# ---------------------------------------------------------------------------
# Cost accounting — Haiku pricing as of 2026-Q2.
# ---------------------------------------------------------------------------
#
# Per-recipe cost is dominated by INPUT tokens (the long vocab listing in
# the prompt). Output is small (just the tool_use payload). These are
# rough constants used only for ``--max-cost`` projections; the bill of
# record is whatever Anthropic reports.

HAIKU_INPUT_PER_MTOK = 1.0   # USD / million input tokens
HAIKU_OUTPUT_PER_MTOK = 5.0  # USD / million output tokens
APPROX_INPUT_TOKENS_PER_REQ = 3500
APPROX_OUTPUT_TOKENS_PER_REQ = 200


def _approx_cost_per_recipe() -> float:
    return (
        APPROX_INPUT_TOKENS_PER_REQ * HAIKU_INPUT_PER_MTOK / 1_000_000
        + APPROX_OUTPUT_TOKENS_PER_REQ * HAIKU_OUTPUT_PER_MTOK / 1_000_000
    )


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _headers(sb_key: str) -> dict[str, str]:
    return {
        'apikey': sb_key,
        'Authorization': f'Bearer {sb_key}',
        'Content-Type': 'application/json',
    }


def _get(sb_url: str, sb_key: str, path: str, *, retries: int = 5) -> Any:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(f'{sb_url}/rest/v1/{path}', headers=_headers(sb_key))
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(min(60, 2 ** i))
    raise last  # type: ignore[misc]


def _patch(sb_url: str, sb_key: str, path: str, body: dict[str, Any]) -> tuple[bool, str]:
    req = urllib.request.Request(
        f'{sb_url}/rest/v1/{path}',
        data=json.dumps(body).encode('utf-8'),
        headers={**_headers(sb_key), 'Prefer': 'return=minimal'},
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True, ''
    except urllib.error.HTTPError as e:
        return False, e.read().decode('utf-8', 'replace')[:160]
    except Exception as e:
        return False, str(e)[:160]


def _delete(sb_url: str, sb_key: str, path: str) -> tuple[bool, str]:
    req = urllib.request.Request(
        f'{sb_url}/rest/v1/{path}',
        headers={**_headers(sb_key), 'Prefer': 'return=minimal'},
        method='DELETE',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True, ''
    except urllib.error.HTTPError as e:
        return False, e.read().decode('utf-8', 'replace')[:160]
    except Exception as e:
        return False, str(e)[:160]


def _post(sb_url: str, sb_key: str, path: str, body: list[dict] | dict) -> tuple[bool, str]:
    req = urllib.request.Request(
        f'{sb_url}/rest/v1/{path}',
        data=json.dumps(body).encode('utf-8'),
        headers={**_headers(sb_key), 'Prefer': 'return=minimal'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True, ''
    except urllib.error.HTTPError as e:
        return False, e.read().decode('utf-8', 'replace')[:160]
    except Exception as e:
        return False, str(e)[:160]


# ---------------------------------------------------------------------------
# Row → classifier-input adapter
# ---------------------------------------------------------------------------

def _extract_inputs(row: dict[str, Any]) -> tuple[str, str, list[str], int | None]:
    """Pull (name, description, ingredients, total_time_minutes) out of a
    program row."""
    pj = row.get('program_json') or {}
    if isinstance(pj, str):
        try:
            pj = json.loads(pj)
        except Exception:
            pj = {}
    name = (row.get('name') or pj.get('name') or '').strip()
    desc = (row.get('description') or pj.get('description') or '').strip()
    meta = pj.get('metadata') or {}

    raw_ings = meta.get('ingredients') or []
    ings: list[str] = []
    for ing in raw_ings[:25]:
        if isinstance(ing, str):
            ings.append(ing.strip())
        elif isinstance(ing, dict):
            measure = (ing.get('measure') or '').strip()
            n = (ing.get('name') or '').strip()
            if n:
                ings.append((f'{measure} {n}' if measure else n).strip())

    total_minutes_raw = meta.get('total_time_minutes')
    try:
        total_minutes: int | None = int(total_minutes_raw) if total_minutes_raw else None
    except (TypeError, ValueError):
        total_minutes = None
    return name, desc, ings, total_minutes


# ---------------------------------------------------------------------------
# Per-row processing
# ---------------------------------------------------------------------------

def _process_row(
    row: dict[str, Any],
    *,
    sb_url: str,
    sb_key: str,
    dry_run: bool,
    provider: str = 'anthropic',
) -> tuple[str, str, TaxonomyResult]:
    """Classify one row and (optionally) write to Supabase. Returns
    (program_id, status, result). Status values:
        ok           — wrote everything
        empty        — classifier returned no useful values; row left alone
        gen_fail     — classifier raised / returned only error
        patch_fail   — Supabase write failed
        dry          — dry-run; no write attempted
    """
    pid = row['id']
    name, desc, ings, minutes = _extract_inputs(row)
    if not name:
        return pid, 'empty', TaxonomyResult(error='no name')
    res = classify(
        name=name, description=desc, ingredients=ings,
        total_time_minutes=minutes, provider=provider,
    )
    if res.error or res.is_empty():
        # Don't overwrite NULL fields with NULL; just skip and report.
        return pid, ('gen_fail' if res.error else 'empty'), res
    if dry_run:
        return pid, 'dry', res

    body = {
        'category': res.category,
        'cuisine': res.cuisine,
        'time_bucket': res.time_bucket,
        'main_ingredient': res.main_ingredient,
        'diets': res.diets,
        'methods': res.methods,
    }
    ok, err = _patch(sb_url, sb_key, f'programs?id=eq.{pid}', body)
    if not ok:
        return pid, 'patch_fail', TaxonomyResult(error=err)

    # Reset & insert tag rows. We delete first so re-runs always reflect
    # the latest classifier output (including dropped tags).
    if res.tags:
        _delete(sb_url, sb_key, f'recipe_tags?program_id=eq.{pid}')
        ok, err = _post(
            sb_url, sb_key, 'recipe_tags',
            [{'program_id': pid, 'tag_slug': t} for t in res.tags],
        )
        if not ok:
            return pid, 'patch_fail', TaxonomyResult(error=f'tags: {err}')
    return pid, 'ok', res


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(
    *,
    user_id: str,
    limit: int | None,
    workers: int,
    dry_run: bool,
    max_cost: float | None,
    provider: str = 'anthropic',
) -> int:
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')
    if not os.environ.get('ANTHROPIC_API_KEY'):
        sys.exit('ANTHROPIC_API_KEY required for classifier')

    # Page through programs that still need classification. category IS
    # NULL is the resume signal: Phase 1.5's spot-check leaves the rest
    # untouched, Phase 2 picks up where it left off.
    PAGE_SIZE = 200
    rows_seen = 0
    queue: list[dict[str, Any]] = []
    print(f'Pulling unclassified rows (limit={limit}, dry_run={dry_run})...', flush=True)
    offset = 0
    while True:
        page = _get(
            sb_url, sb_key,
            f'programs?user_id=eq.{user_id}&is_public=eq.true'
            f'&environment=eq.kitchen&category=is.null'
            f'&select=id,name,description,program_json'
            f'&order=id&limit={PAGE_SIZE}&offset={offset}',
        )
        if not page:
            break
        for row in page:
            queue.append(row)
            rows_seen += 1
            if limit and rows_seen >= limit:
                break
        if limit and rows_seen >= limit:
            break
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    print(f'  queued {len(queue)} rows', flush=True)
    if not queue:
        print('Nothing to do.', flush=True)
        return 0

    if provider == 'gemini':
        # Gemini 2.5 Flash: $0.075 in / $0.30 out per Mtok.
        cost_per = (
            APPROX_INPUT_TOKENS_PER_REQ * 0.075 / 1_000_000
            + APPROX_OUTPUT_TOKENS_PER_REQ * 0.30 / 1_000_000
        )
    else:
        cost_per = _approx_cost_per_recipe()
    projected = cost_per * len(queue)
    print(f'  provider={provider}  approx cost: ${projected:.2f} '
          f'(@ ${cost_per * 1000:.2f} per 1000 rows)', flush=True)
    if max_cost is not None and projected > max_cost:
        sys.exit(f'projected ${projected:.2f} exceeds --max-cost ${max_cost:.2f}; aborting')

    n_ok = n_gen_fail = n_patch_fail = n_empty = 0
    done = 0
    cuisines: dict[str, int] = {}
    categories: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    diet_counts: dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                _process_row, row,
                sb_url=sb_url, sb_key=sb_key, dry_run=dry_run, provider=provider,
            ): row
            for row in queue
        }
        for fut in as_completed(futs):
            done += 1
            try:
                pid, status, res = fut.result()
            except Exception as e:
                n_gen_fail += 1
                print(f'  worker exc: {e}', flush=True)
                continue
            if status == 'ok' or status == 'dry':
                n_ok += 1
                if res.cuisine:
                    cuisines[res.cuisine] = cuisines.get(res.cuisine, 0) + 1
                if res.category:
                    categories[res.category] = categories.get(res.category, 0) + 1
                for t in res.tags:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
                for d in res.diets:
                    diet_counts[d] = diet_counts.get(d, 0) + 1
            elif status == 'empty':
                n_empty += 1
            elif status == 'gen_fail':
                n_gen_fail += 1
                if n_gen_fail <= 5:
                    print(f'  gen_fail {pid}: {res.error}', flush=True)
            elif status == 'patch_fail':
                n_patch_fail += 1
                if n_patch_fail <= 5:
                    print(f'  patch_fail {pid}: {res.error}', flush=True)
            if done % 10 == 0 or done == len(queue):
                print(
                    f'  ...{done}/{len(queue)} ok={n_ok} '
                    f'empty={n_empty} gen_fail={n_gen_fail} '
                    f'patch_fail={n_patch_fail}',
                    flush=True,
                )

    print(
        f'\nDone. ok={n_ok}  empty={n_empty}  gen_fail={n_gen_fail}  '
        f'patch_fail={n_patch_fail}',
        flush=True,
    )

    # Distribution dump — used to spot-check Phase 1.5 before running Phase 2.
    def _print_top(label: str, counts: dict[str, int], n: int = 10) -> None:
        if not counts:
            return
        print(f'\nTop {label} ({len(counts)} distinct values):', flush=True)
        for k, v in sorted(counts.items(), key=lambda t: -t[1])[:n]:
            print(f'  {v:5}  {k}', flush=True)

    _print_top('categories', categories)
    _print_top('cuisines', cuisines)
    _print_top('diets', diet_counts)
    _print_top('tags', tag_counts, n=15)
    return 0


# ---------------------------------------------------------------------------
# Anthropic Batch path — 50% discount on input + output tokens, async.
# ---------------------------------------------------------------------------
#
# Submit one batch with up to 100K classify-recipe requests (Anthropic's
# per-batch ceiling). For our 45K corpus that fits easily. The batch is
# returned via streaming JSONL once Anthropic finishes processing — we
# poll until done, then ingest each result and PATCH Supabase.
#
# Cost vs interactive: same prompt + tool schema, just the half-price
# billing tier. Wall time is "<24h" per the SLA, but in practice small
# batches typically come back within minutes to an hour.

BATCH_POLL_SECONDS = 30


def _build_batch_request(row: dict[str, Any], skip_time_default: bool = False) -> dict[str, Any] | None:
    """Build one item for messages.batches.create. Returns None if the row
    can't be classified (e.g. has no name)."""
    name, desc, ings, minutes = _extract_inputs(row)
    if not name:
        return None
    skip_time = minutes is not None
    user_prompt = _build_user_prompt(
        name=name, description=desc, ingredients=ings,
        skip_time_bucket=skip_time,
    )
    tool = _build_classifier_tool(skip_time)
    return {
        # custom_id = program UUID so we can map results back to rows.
        # Anthropic requires custom_id ≤64 chars; UUIDs are 36, fits fine.
        'custom_id': row['id'],
        'params': {
            'model': DEFAULT_MODEL,
            'max_tokens': 800,
            'system': _SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': user_prompt}],
            'tools': [tool],
            'tool_choice': {'type': 'tool', 'name': _TOOL_NAME},
        },
    }


def _parse_batch_message(message: Any) -> dict[str, Any] | None:
    """Pull the tool_use input out of an Anthropic Message, falling back
    to text-parsing if the model rolled back to plain text."""
    for block in (getattr(message, 'content', None) or []):
        if getattr(block, 'type', None) == 'tool_use' and getattr(block, 'name', None) == _TOOL_NAME:
            return getattr(block, 'input', None)
    raw = ''
    for block in (getattr(message, 'content', None) or []):
        if getattr(block, 'type', None) == 'text':
            raw += getattr(block, 'text', '') or ''
    return _parse_response(raw) if raw else None


def _result_to_taxonomy(parsed: dict[str, Any], total_minutes: int | None) -> TaxonomyResult:
    """Mirror of the post-processing in classify(): coerce values to
    vocab, expand diet implications, deterministically pick time_bucket."""
    diets = _coerce_list(parsed.get('diets'), vocab.DIETS_SET)
    diets = vocab.expand_diets(diets)
    skip_time = total_minutes is not None
    return TaxonomyResult(
        category=_coerce_single(parsed.get('category'), vocab.CATEGORIES_SET),
        cuisine=_coerce_single(parsed.get('cuisine'), vocab.CUISINES_SET),
        time_bucket=vocab.time_bucket_for_minutes(total_minutes) if skip_time
            else _coerce_single(parsed.get('time_bucket'), vocab.TIME_BUCKETS_SET),
        main_ingredient=_coerce_single(parsed.get('main_ingredient'), vocab.MAIN_INGREDIENTS_SET),
        diets=diets,
        methods=_coerce_list(parsed.get('methods'), vocab.METHODS_SET),
        tags=_coerce_list(parsed.get('tags'), vocab.TAGS_SET),
    )


def run_batch(
    *,
    user_id: str,
    limit: int | None,
    dry_run: bool,
    max_cost: float | None,
) -> int:
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')
    if not os.environ.get('ANTHROPIC_API_KEY'):
        sys.exit('ANTHROPIC_API_KEY required')
    try:
        import anthropic  # type: ignore
    except ImportError as e:
        sys.exit(f'anthropic SDK missing: {e}')
    client = anthropic.Anthropic()

    # Pull all unclassified rows up front. Batch can hold 100K so even
    # a full ~45K corpus fits in a single submission.
    PAGE_SIZE = 1000
    rows: list[dict[str, Any]] = []
    offset = 0
    print(f'Pulling unclassified rows (limit={limit})...', flush=True)
    while True:
        page = _get(
            sb_url, sb_key,
            f'programs?user_id=eq.{user_id}&is_public=eq.true'
            f'&environment=eq.kitchen&category=is.null'
            f'&select=id,name,description,program_json'
            f'&order=id&limit={PAGE_SIZE}&offset={offset}',
        )
        if not page:
            break
        rows.extend(page)
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    print(f'  {len(rows)} rows queued', flush=True)
    if not rows:
        print('Nothing to classify.', flush=True)
        return 0

    # Build batch requests + remember total_minutes per row for the
    # result-ingest step.
    minutes_by_pid: dict[str, int | None] = {}
    requests: list[dict[str, Any]] = []
    for row in rows:
        _, _, _, minutes = _extract_inputs(row)
        minutes_by_pid[row['id']] = minutes
        req = _build_batch_request(row)
        if req is not None:
            requests.append(req)
    print(f'  built {len(requests)} batch requests', flush=True)

    # Cost projection — batch is 50% off, applied to both input + output.
    cost_per = _approx_cost_per_recipe() * 0.5
    projected = cost_per * len(requests)
    print(f'  approx batch cost: ${projected:.2f} '
          f'(${cost_per * 1000:.2f} per 1000 rows; 50% off interactive)', flush=True)
    if max_cost is not None and projected > max_cost:
        sys.exit(f'projected ${projected:.2f} exceeds --max-cost ${max_cost:.2f}; aborting')
    if dry_run:
        print('DRY-RUN: not submitting batch.', flush=True)
        return 0

    # Submit
    print('Submitting batch to Anthropic...', flush=True)
    batch = client.messages.batches.create(requests=requests)
    print(f'  batch_id={batch.id}  status={batch.processing_status}', flush=True)

    # Poll
    started = time.time()
    while True:
        b = client.messages.batches.retrieve(batch.id)
        elapsed = int(time.time() - started)
        rc = b.request_counts
        print(
            f'  [{elapsed:>5}s] status={b.processing_status} '
            f'processing={rc.processing} succeeded={rc.succeeded} '
            f'errored={rc.errored} canceled={rc.canceled} '
            f'expired={rc.expired}',
            flush=True,
        )
        if b.processing_status == 'ended':
            break
        time.sleep(BATCH_POLL_SECONDS)

    # Stream results back. Each yielded item has .custom_id and .result.
    print(f'Batch finished in {int(time.time()-started)}s. Ingesting results...', flush=True)
    n_ok = n_gen_fail = n_patch_fail = n_empty = 0
    cuisines: dict[str, int] = {}
    categories: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    diet_counts: dict[str, int] = {}
    done = 0
    for item in client.messages.batches.results(batch.id):
        done += 1
        pid = item.custom_id
        result = item.result
        if getattr(result, 'type', None) != 'succeeded':
            n_gen_fail += 1
            if n_gen_fail <= 5:
                print(f'  result-fail {pid}: {getattr(result, "type", None)} {getattr(result, "error", None)}', flush=True)
            continue
        message = getattr(result, 'message', None)
        parsed = _parse_batch_message(message) if message is not None else None
        if not parsed:
            n_gen_fail += 1
            continue
        res = _result_to_taxonomy(parsed, minutes_by_pid.get(pid))
        if res.is_empty():
            n_empty += 1
            continue
        body = {
            'category': res.category,
            'cuisine': res.cuisine,
            'time_bucket': res.time_bucket,
            'main_ingredient': res.main_ingredient,
            'diets': res.diets,
            'methods': res.methods,
        }
        ok, err = _patch(sb_url, sb_key, f'programs?id=eq.{pid}', body)
        if not ok:
            n_patch_fail += 1
            if n_patch_fail <= 5:
                print(f'  patch_fail {pid}: {err}', flush=True)
            continue
        if res.tags:
            _delete(sb_url, sb_key, f'recipe_tags?program_id=eq.{pid}')
            ok, err = _post(
                sb_url, sb_key, 'recipe_tags',
                [{'program_id': pid, 'tag_slug': t} for t in res.tags],
            )
            if not ok:
                n_patch_fail += 1
                continue
        n_ok += 1
        if res.cuisine:
            cuisines[res.cuisine] = cuisines.get(res.cuisine, 0) + 1
        if res.category:
            categories[res.category] = categories.get(res.category, 0) + 1
        for t in res.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        for d in res.diets:
            diet_counts[d] = diet_counts.get(d, 0) + 1
        if done % 100 == 0:
            print(f'  ...ingest {done} ok={n_ok} empty={n_empty} '
                  f'gen_fail={n_gen_fail} patch_fail={n_patch_fail}',
                  flush=True)

    print(
        f'\nDone. ok={n_ok}  empty={n_empty}  gen_fail={n_gen_fail}  '
        f'patch_fail={n_patch_fail}',
        flush=True,
    )

    def _print_top(label: str, counts: dict[str, int], n: int = 10) -> None:
        if not counts:
            return
        print(f'\nTop {label} ({len(counts)} distinct values):', flush=True)
        for k, v in sorted(counts.items(), key=lambda t: -t[1])[:n]:
            print(f'  {v:5}  {k}', flush=True)

    _print_top('categories', categories)
    _print_top('cuisines', cuisines)
    _print_top('diets', diet_counts)
    _print_top('tags', tag_counts, n=15)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    _load_dotenv_if_present()
    ap = argparse.ArgumentParser(
        prog='rhylthyme-backfill-taxonomy',
        description='Classify unclassified kitchen recipes via Claude Haiku.',
    )
    ap.add_argument('--user-id', default=DEFAULT_RECIPES_USER_ID)
    ap.add_argument('--limit', type=int, default=None,
                    help='Cap the number of rows processed (e.g. 100 for a spot-check).')
    ap.add_argument('--workers', type=int, default=8,
                    help='Concurrent classifier calls (default: 8).')
    ap.add_argument('--dry-run', action='store_true',
                    help='Classify but do not write to Supabase.')
    ap.add_argument('--max-cost', type=float, default=None,
                    help='Abort if approx projected USD spend exceeds this.')
    ap.add_argument('--batch', action='store_true',
                    help='Use Anthropic Batch API (50%% discount, async). '
                         'Anthropic provider only.')
    ap.add_argument('--provider', choices=['anthropic', 'gemini'], default='anthropic',
                    help='Classifier provider. anthropic = Haiku 4.5; '
                         'gemini = 2.5 Flash (~7x cheaper).')
    args = ap.parse_args(argv)
    if args.batch:
        if args.provider != 'anthropic':
            sys.exit('--batch is only supported for the anthropic provider')
        return run_batch(
            user_id=args.user_id,
            limit=args.limit,
            dry_run=args.dry_run,
            max_cost=args.max_cost,
        )
    return run(
        user_id=args.user_id,
        limit=args.limit,
        workers=args.workers,
        dry_run=args.dry_run,
        max_cost=args.max_cost,
        provider=args.provider,
    )


if __name__ == '__main__':
    raise SystemExit(main())
