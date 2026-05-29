"""
Backfill ``programs.language`` using langdetect on name + description.

Runs entirely locally — no LLM, no external API. Safe to re-run; only
touches rows where ``language IS NULL``.

Usage:
    rhylthyme-backfill-language [--limit N] [--dry-run]

The script reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from env. It
pages through public kitchen-vertical programs missing a language tag,
runs the detector on each row's name + description, and PATCHes the
``language`` column. Progress reports every 500 rows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional
from urllib.parse import urlencode

import requests

from .language_detect import detect_language


def _env() -> tuple[str, str]:
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        print('ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set', file=sys.stderr)
        sys.exit(2)
    return url, key


def _page(url: str, key: str, *, limit: int, offset: int) -> list[dict]:
    """Pull a page of public kitchen rows that haven't been classified yet."""
    qs = urlencode([
        ('select', 'id,name,description'),
        ('environment', 'eq.kitchen'),
        ('is_public', 'is.true'),
        ('language', 'is.null'),
        ('order', 'updated_at.desc'),
        ('limit', str(limit)),
        ('offset', str(offset)),
    ])
    r = requests.get(
        f'{url}/rest/v1/programs?{qs}',
        headers={'apikey': key, 'Authorization': f'Bearer {key}'},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _patch(url: str, key: str, row_id: str, language: str) -> bool:
    r = requests.patch(
        f'{url}/rest/v1/programs?id=eq.{row_id}',
        headers={
            'apikey': key, 'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'Prefer': 'return=minimal',
        },
        data=json.dumps({'language': language}),
        timeout=20,
    )
    return r.ok


def run(*, limit: Optional[int], dry_run: bool) -> int:
    url, key = _env()
    PAGE = 500
    seen = ok = empty = err = 0
    t0 = time.time()
    while True:
        rows = _page(url, key, limit=PAGE, offset=0)
        if not rows:
            break
        for r in rows:
            seen += 1
            lang = detect_language(r.get('name'), r.get('description'))
            if not lang:
                empty += 1
                # Stamp 'und' so we don't re-process the same row every
                # backfill run. Treated as null by the UI.
                if not dry_run:
                    _patch(url, key, r['id'], 'und')
                continue
            if dry_run:
                ok += 1
            else:
                if _patch(url, key, r['id'], lang):
                    ok += 1
                else:
                    err += 1
            if seen % 200 == 0:
                rate = seen / max(1, time.time() - t0)
                print(f'  ...{seen} seen  ok={ok} empty={empty} err={err}  ({rate:.0f}/s)')
            if limit and seen >= limit:
                break
        if limit and seen >= limit:
            break
    print(f'\nDone. seen={seen} ok={ok} empty={empty} err={err} in {time.time()-t0:.1f}s')
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--limit', type=int, default=None, help='stop after N rows')
    p.add_argument('--dry-run', action='store_true', help='detect but do not write')
    args = p.parse_args()
    sys.exit(run(limit=args.limit, dry_run=args.dry_run))


if __name__ == '__main__':
    main()
