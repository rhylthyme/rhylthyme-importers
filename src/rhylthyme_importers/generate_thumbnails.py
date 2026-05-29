"""
Backfill missing recipe thumbnails using fal.ai FLUX.1 [schnell].

Finds programs with no ``metadata.thumbnail``, generates a food-photography
prompt from the recipe name + cuisine + ingredient list, calls fal, and
writes the returned ``fal.media`` URL back into ``program_json.metadata.thumbnail``.

Costs: FLUX.1 [schnell] is $0.003/image (≈ $15 for 5K images at the time of
writing). Verify on https://fal.ai/pricing before large runs.

Env vars (loaded from rhylthyme-web/.env):
- ``FAL_KEY`` — fal.ai API key
- ``SUPABASE_URL`` / ``SUPABASE_SERVICE_ROLE_KEY``
- ``RHYLTHYME_RECIPES_USER_ID`` (optional, defaults to the canonical recipes UID)

Reproducible workflow
---------------------
::

    # 1. Quality test on a small random sample
    python -m rhylthyme_importers.generate_thumbnails --limit 10

    # 2. Full backfill
    python -m rhylthyme_importers.generate_thumbnails --all

    # 3. Re-run only programs missing thumbnails (skips anything already filled)
    python -m rhylthyme_importers.generate_thumbnails --all --skip-existing
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from .cooklang_federation import (
    DEFAULT_RECIPES_USER_ID,
    _load_dotenv_if_present,
)


FAL_MODEL = 'fal-ai/flux/schnell'
PROMPT_TEMPLATE = (
    'Professional overhead food photography of {name}{extra}. '
    'On a clean white plate or rustic wooden surface, soft natural daylight, '
    'shallow depth of field, magazine-quality, appetising, hyperreal.'
)


def _meta_extra(name: str, meta: dict) -> str:
    """Return a comma-prefixed hint string built from cuisine / category / a few
    ingredients — gives FLUX more to work with than the recipe name alone."""
    bits = []
    cuisine = (meta.get('area') or meta.get('cuisine') or '').strip()
    if cuisine:
        bits.append(f'{cuisine} cuisine')
    category = (meta.get('category') or '').strip()
    if category and category.lower() not in name.lower():
        bits.append(category.lower())
    ings = meta.get('ingredients') or []
    ing_names = []
    for ing in ings[:5]:
        if isinstance(ing, dict):
            n = (ing.get('name') or '').strip()
            if n:
                ing_names.append(n)
        elif isinstance(ing, str) and ing.strip():
            ing_names.append(ing.strip())
    if ing_names:
        bits.append('with ' + ', '.join(ing_names))
    if not bits:
        return ''
    return ', ' + ', '.join(bits)


def _has_thumbnail(meta: dict) -> bool:
    t = meta.get('thumbnail')
    if isinstance(t, str) and t.strip():
        return True
    if isinstance(t, dict):
        for k in ('url', 'src', 'href'):
            v = t.get(k)
            if isinstance(v, str) and v.strip():
                return True
    if isinstance(t, list) and t:
        return _has_thumbnail({'thumbnail': t[0]})
    return False


def _supabase_headers(sb_key: str) -> dict:
    return {
        'apikey': sb_key,
        'Authorization': f'Bearer {sb_key}',
        'Content-Type': 'application/json',
    }


def _fetch_missing(sb_url: str, sb_key: str, user_id: str,
                   limit: int | None = None,
                   shuffle: bool = False) -> list[dict]:
    """Page through every program for ``user_id`` and return rows whose
    ``metadata.thumbnail`` is empty/missing. We do the filter client-side so
    structurally-funny thumbnail values (dict/list/whitespace) are caught
    consistently."""
    rows = []
    offset = 0
    H = _supabase_headers(sb_key)
    while True:
        url = (f'{sb_url}/rest/v1/programs?user_id=eq.{user_id}'
               f'&select=id,name,program_json&limit=1000&offset={offset}')
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=120) as resp:
            page = json.loads(resp.read())
        if not page:
            break
        for r in page:
            pj = r['program_json'] if isinstance(r['program_json'], dict) else json.loads(r['program_json'])
            meta = pj.get('metadata') or {}
            if not _has_thumbnail(meta):
                rows.append({'id': r['id'], 'name': r['name'], 'program_json': pj})
        if len(page) < 1000:
            break
        offset += 1000
    if shuffle:
        random.shuffle(rows)
    if limit is not None:
        rows = rows[:limit]
    return rows


def _generate_one(row: dict) -> dict:
    """Generate a single image. Returns dict with status + url + error."""
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
        return {'id': row['id'], 'status': 'gen_fail', 'error': str(e)[:200]}
    images = result.get('images') or []
    if not images:
        return {'id': row['id'], 'status': 'no_image', 'error': 'fal returned no images'}
    img_url = images[0].get('url')
    if not img_url:
        return {'id': row['id'], 'status': 'no_url', 'error': 'fal image has no url'}
    return {'id': row['id'], 'status': 'ok', 'url': img_url, 'prompt': prompt, 'name': name}


def _patch_thumbnail(sb_url: str, sb_key: str, row: dict, image_url: str) -> tuple[bool, str]:
    pj = row['program_json']
    meta = pj.setdefault('metadata', {})
    meta['thumbnail'] = image_url
    body = json.dumps({'program_json': pj}).encode('utf-8')
    req = urllib.request.Request(
        f'{sb_url}/rest/v1/programs?id=eq.{row["id"]}',
        data=body,
        headers={**_supabase_headers(sb_key), 'Prefer': 'return=minimal'},
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True, ''
    except urllib.error.HTTPError as e:
        return False, e.read().decode('utf-8', errors='replace')[:160]
    except Exception as e:
        return False, str(e)[:160]


def run(*, limit: int | None, all_: bool, shuffle: bool,
        workers: int, dry_run: bool, user_id: str,
        report_path: Path | None) -> int:
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    fal_key = os.environ.get('FAL_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')
    if not fal_key:
        sys.exit('FAL_KEY required')

    print(f'Fetching programs missing thumbnails for {user_id} ...', flush=True)
    rows = _fetch_missing(sb_url, sb_key, user_id, limit=None, shuffle=shuffle)
    print(f'  {len(rows)} programs are missing thumbnails', flush=True)

    if not all_:
        rows = rows[:limit if limit is not None else 10]
    print(f'Will generate for {len(rows)} programs '
          f'(model={FAL_MODEL}, workers={workers}, dry_run={dry_run})', flush=True)

    results = []
    n_ok = n_fail = n_patch_fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_generate_one, r): r for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            row = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {'id': row['id'], 'status': 'worker_exc', 'error': str(e)[:200]}
            res['name'] = row['name']
            results.append(res)
            if res['status'] != 'ok':
                n_fail += 1
                print(f'  [{i}/{len(rows)}] FAIL {row["name"][:40]!r}: {res.get("error","?")}', flush=True)
                continue
            if dry_run:
                n_ok += 1
                print(f'  [{i}/{len(rows)}] OK (dry) {row["name"][:40]!r} -> {res["url"]}', flush=True)
                continue
            ok, err = _patch_thumbnail(sb_url, sb_key, row, res['url'])
            if ok:
                n_ok += 1
                if i % 25 == 0 or i <= 10 or i == len(rows):
                    print(f'  [{i}/{len(rows)}] ok={n_ok} fail={n_fail} patch_fail={n_patch_fail} '
                          f'(last: {row["name"][:40]!r})', flush=True)
            else:
                n_patch_fail += 1
                print(f'  [{i}/{len(rows)}] PATCH FAIL {row["name"][:40]!r}: {err}', flush=True)

    print(f'\nDone: gen_ok={n_ok}  gen_fail={n_fail}  patch_fail={n_patch_fail}', flush=True)
    if report_path:
        report_path.write_text(json.dumps(results, indent=2))
        print(f'Wrote per-row report to {report_path}', flush=True)
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    _load_dotenv_if_present()
    ap = argparse.ArgumentParser(
        prog='rhylthyme-generate-thumbnails',
        description='Backfill missing recipe thumbnails via fal FLUX.1 [schnell].',
    )
    ap.add_argument('--all', action='store_true',
                    help='Process every missing-thumbnail program (otherwise --limit).')
    ap.add_argument('--limit', type=int, default=10,
                    help='Max programs to process when --all is not set (default: 10).')
    ap.add_argument('--shuffle', action='store_true', default=True,
                    help='Shuffle the missing-thumbnail set so test batches are random (default).')
    ap.add_argument('--no-shuffle', dest='shuffle', action='store_false',
                    help='Process in DB order instead.')
    ap.add_argument('--workers', type=int, default=4,
                    help='Concurrent fal calls (default: 4). fal handles parallel requests fine.')
    ap.add_argument('--dry-run', action='store_true',
                    help='Generate images but do not write them back to Supabase.')
    ap.add_argument('--user-id', default=DEFAULT_RECIPES_USER_ID,
                    help='Owner user_id (default: canonical recipes account).')
    ap.add_argument('--report', metavar='PATH', default=None,
                    help='Write per-row results JSON to this path.')
    args = ap.parse_args(argv)
    return run(
        limit=args.limit,
        all_=args.all,
        shuffle=args.shuffle,
        workers=args.workers,
        dry_run=args.dry_run,
        user_id=args.user_id,
        report_path=Path(args.report) if args.report else None,
    )


if __name__ == '__main__':
    raise SystemExit(main())
