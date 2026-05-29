"""
Find and replace recipe thumbnails that are broken or hot-link-blocked.

Phase 1 (detection) — HEAD-check every public kitchen recipe's
``program_json.metadata.thumbnail`` URL. A URL is "broken" if any of:
- HTTP status is 4xx or 5xx
- Response content-type isn't an image/*
- Request times out / raises
- URL ends with ``/__hp-blocked`` (a sentinel some sites append)

FAL-generated URLs (``v3b.fal.media`` etc.) are skipped — they're already
ours and never expire / referer-protected. So are local Supabase Storage
URLs.

Phase 2 (regeneration) — for each broken row, regenerate via fal.ai
FLUX.1 [schnell] (~ $0.003/image, 1s wall clock) and PATCH the new
``fal.media`` URL into ``program_json.metadata.thumbnail``.

Resumable: re-running skips rows that were already replaced this cycle
(thumbnails that already point to ``fal.media`` or ``supabase``).

Usage:
    rhylthyme-replace-broken-thumbs --dry-run              # detect only
    rhylthyme-replace-broken-thumbs --limit 100            # sample run
    rhylthyme-replace-broken-thumbs --workers 30           # full run
    rhylthyme-replace-broken-thumbs --report /tmp/r.jsonl  # save detail to file

Required env: ``SUPABASE_URL``, ``SUPABASE_SERVICE_ROLE_KEY``, ``FAL_KEY``.
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
from typing import Iterable

from .cooklang_federation import (
    DEFAULT_RECIPES_USER_ID,
    _load_dotenv_if_present,
)
from .generate_thumbnails import (
    FAL_MODEL,
    PROMPT_TEMPLATE,
    _has_thumbnail,
    _meta_extra,
    _supabase_headers,
    _patch_thumbnail,
)


# Domains we KNOW are ours (never replace, never re-check).
SAFE_DOMAINS = (
    'fal.media',
    'supabase.co',
    'supabase.in',
)

# Headers we send when probing third-party images. A referer that
# matches our site is the same one a real browser would use, so any
# server that allows hotlinking from rhylthyme.com will return 200.
PROBE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (rhylthyme-thumb-probe)',
    'Referer': 'https://kitchen.rhylthyme.com/',
    'Accept': 'image/*,*/*;q=0.8',
}


def _thumbnail_url(meta: dict) -> str:
    """Pull the thumbnail URL out of metadata, handling the dict/list
    variants that appear in older imports."""
    t = meta.get('thumbnail')
    if isinstance(t, str):
        return t.strip()
    if isinstance(t, dict):
        for k in ('url', 'src', 'href'):
            v = t.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(t, list) and t:
        return _thumbnail_url({'thumbnail': t[0]})
    return ''


def _is_safe_url(url: str) -> bool:
    return any(d in url for d in SAFE_DOMAINS)


def _probe_status(url: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Return ``(is_broken, reason)``. ``is_broken=False`` means the URL
    looks like a live image."""
    if not url:
        return True, 'empty'
    if url.endswith('/__hp-blocked'):
        return True, 'hp-blocked-suffix'
    if not (url.startswith('http://') or url.startswith('https://')):
        return True, 'not-http'
    # Most servers respect HEAD; some quirky CDNs return 405 on HEAD but
    # respond fine to GET. Treat 405 as "indeterminate" and fall back
    # to a tiny GET via Range to keep bytes-downloaded low.
    try:
        req = urllib.request.Request(url, method='HEAD', headers=PROBE_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            ctype = resp.headers.get('Content-Type', '')
    except urllib.error.HTTPError as e:
        status, ctype = e.code, e.headers.get('Content-Type', '') if e.headers else ''
    except Exception as e:
        return True, f'head-error: {type(e).__name__}'

    if status == 405:
        # Fall back to a 1-byte GET to settle ambiguity.
        try:
            req = urllib.request.Request(
                url, method='GET',
                headers={**PROBE_HEADERS, 'Range': 'bytes=0-0'},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, ctype = resp.status, resp.headers.get('Content-Type', '')
        except urllib.error.HTTPError as e:
            status, ctype = e.code, e.headers.get('Content-Type', '') if e.headers else ''
        except Exception as e:
            return True, f'get-error: {type(e).__name__}'

    if 400 <= status < 600:
        return True, f'http-{status}'
    if ctype and not ctype.lower().startswith('image/'):
        return True, f'wrong-content-type: {ctype[:40]}'
    return False, 'ok'


def _fetch_all_rows(sb_url: str, sb_key: str, user_id: str) -> list[dict]:
    """Page through the entire public-kitchen corpus owned by ``user_id``."""
    rows: list[dict] = []
    offset = 0
    H = _supabase_headers(sb_key)
    while True:
        url = (
            f'{sb_url}/rest/v1/programs?user_id=eq.{user_id}'
            f'&select=id,name,program_json&limit=1000&offset={offset}'
        )
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=120) as resp:
            page = json.loads(resp.read())
        if not page:
            break
        for r in page:
            pj = r['program_json'] if isinstance(r['program_json'], dict) else json.loads(r['program_json'])
            meta = pj.get('metadata') or {}
            url = _thumbnail_url(meta)
            if not url:
                continue  # no thumbnail at all → handled by generate_thumbnails
            if _is_safe_url(url):
                continue  # our own URLs are never broken
            rows.append({
                'id': r['id'], 'name': r['name'],
                'program_json': pj, 'thumb_url': url,
            })
        if len(page) < 1000:
            break
        offset += 1000
    return rows


def _generate_replacement(row: dict) -> dict:
    """Run FAL to generate a replacement image. Returns {ok, url, error}."""
    import fal_client
    pj = row['program_json']
    meta = pj.get('metadata') or {}
    name = pj.get('name') or row.get('name') or 'a dish'
    prompt = PROMPT_TEMPLATE.format(name=name, extra=_meta_extra(name, meta))
    try:
        result = fal_client.subscribe(
            FAL_MODEL,
            arguments={
                'prompt': prompt,
                'image_size': 'square_hd',
                'num_inference_steps': 4,
                'enable_safety_checker': False,
            },
            with_logs=False,
        )
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}
    images = result.get('images') or []
    if not images or not images[0].get('url'):
        return {'ok': False, 'error': 'fal returned no image'}
    return {'ok': True, 'url': images[0]['url']}


def run(*, limit: int | None, workers: int, dry_run: bool,
        user_id: str, report_path: Path | None) -> int:
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    fal_key = os.environ.get('FAL_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')
    if not dry_run and not fal_key:
        sys.exit('FAL_KEY required when not in --dry-run mode')

    print(f'Fetching public-kitchen rows for user_id={user_id} ...', flush=True)
    rows = _fetch_all_rows(sb_url, sb_key, user_id)
    if limit:
        rows = rows[:limit]
    print(f'  → {len(rows)} rows with third-party thumbnail URLs', flush=True)

    # ----- Detection phase -----
    print(f'Probing {len(rows)} URLs with {workers} workers ...', flush=True)
    broken: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_row = {
            ex.submit(_probe_status, r['thumb_url']): r for r in rows
        }
        for i, fut in enumerate(as_completed(future_to_row), 1):
            r = future_to_row[fut]
            try:
                is_broken, reason = fut.result()
            except Exception as e:
                is_broken, reason = True, f'exception: {type(e).__name__}'
            if is_broken:
                broken.append({**r, 'reason': reason})
            if i % 500 == 0:
                rate = i / max(1, time.time() - t0)
                print(f'  ...{i}/{len(rows)} probed  broken={len(broken)}  ({rate:.0f}/s)', flush=True)
    print(f'\nDetection done. {len(broken)}/{len(rows)} broken in {time.time()-t0:.0f}s', flush=True)

    # Print top reasons
    from collections import Counter
    reasons = Counter(r['reason'] for r in broken)
    for reason, n in reasons.most_common():
        print(f'  {n:6d}  {reason}')

    if report_path:
        with report_path.open('w') as f:
            for r in broken:
                f.write(json.dumps({
                    'id': r['id'], 'name': r['name'][:80],
                    'url': r['thumb_url'][:120], 'reason': r['reason'],
                }) + '\n')
        print(f'  wrote {len(broken)} report rows to {report_path}', flush=True)

    if dry_run or not broken:
        print('\nDry-run — no regenerations queued.' if dry_run else 'Nothing to do.')
        return 0

    # Cost guard
    est_cost = 0.003 * len(broken)
    print(f'\nWill regenerate {len(broken)} thumbnails (~${est_cost:.2f}). Proceeding ...', flush=True)

    # ----- Regeneration phase -----
    ok = err = 0
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=min(workers, 16)) as ex:
        future_to_row = {ex.submit(_generate_replacement, r): r for r in broken}
        for i, fut in enumerate(as_completed(future_to_row), 1):
            r = future_to_row[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {'ok': False, 'error': str(e)[:200]}
            if res.get('ok'):
                wrote_ok, msg = _patch_thumbnail(sb_url, sb_key, r, res['url'])
                if wrote_ok:
                    ok += 1
                else:
                    err += 1
                    print(f'  patch_fail {r["id"]}: {msg[:120]}', flush=True)
            else:
                err += 1
                print(f'  gen_fail {r["id"]}: {res.get("error","")[:120]}', flush=True)
            if i % 50 == 0:
                rate = i / max(1, time.time() - t1)
                print(f'  ...{i}/{len(broken)} regenerated  ok={ok} err={err}  ({rate:.0f}/s)', flush=True)

    print(f'\nDone. regenerated={ok} failed={err} in {time.time()-t1:.0f}s')
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--limit', type=int, default=None,
                   help='stop after N rows (detection phase)')
    p.add_argument('--workers', type=int, default=20,
                   help='concurrent HEAD probes (default 20)')
    p.add_argument('--dry-run', action='store_true',
                   help='detect only — do not call FAL or write to Supabase')
    p.add_argument('--user-id', default=DEFAULT_RECIPES_USER_ID,
                   help='canonical recipes user_id (default = catalog owner)')
    p.add_argument('--report', type=Path, default=None,
                   help='write per-row diagnostic JSONL here')
    args = p.parse_args()

    _load_dotenv_if_present()
    sys.exit(run(
        limit=args.limit,
        workers=args.workers,
        dry_run=args.dry_run,
        user_id=args.user_id,
        report_path=args.report,
    ))


if __name__ == '__main__':
    main()
