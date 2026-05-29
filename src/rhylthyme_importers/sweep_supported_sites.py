"""
Sweep every domain supported by the ``recipe-scrapers`` library — auto-discover
recipe URLs from each site's sitemap, scrape them, upload to Supabase under the
canonical recipes account.

Resumable: writes a checkpoint JSON after each site so a restart picks up where
it left off. Uploads are idempotent (skip rows whose source_url is already in
Supabase for the target user).

Default flow per site:
  1. ``auto_sitemap.discover_for_domain`` → up to ``--max-per-site`` URLs
  2. Pre-filter against Supabase to drop URLs we already have
  3. Concurrent scrape (8 workers, 1s/host rate limit)
  4. Upload to Supabase

Per-site usage stays tiny by default (``--max-per-site 200``) so a 580-site
sweep finishes in a few hours, not days, and we don't take more than 200
recipes from any one site.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Iterable

from .auto_sitemap import discover_for_domain
from .cooklang_federation import (
    DEFAULT_RECIPES_USER_ID,
    _existing_source_urls,
    _load_dotenv_if_present,
    upload as _upload_staged,
)
from .recipe_scrapers_mine import import_urls


class _DiscoverTimeout(Exception):
    pass


def _discover_with_budget(domain: str, *, max_urls: int, budget_s: int):
    """Run discover_for_domain with a hard wall-clock cap so a single broken
    host (slow DNS, dropped SYNs, byte-by-byte sitemap) can't block the
    sweep. Uses SIGALRM so it kills any blocking syscall, not just Python.

    Caveat: SIGALRM is process-wide. We're single-threaded during discovery,
    so this is safe; threads only spin up later in import_urls.
    """
    def _alarm(_sig, _frm):
        raise _DiscoverTimeout(f'discovery timed out after {budget_s}s')
    prev = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(budget_s)
    try:
        return discover_for_domain(domain, max_urls=max_urls)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def _supported_domains() -> list[str]:
    """Return the sorted list of domains the recipe-scrapers library knows."""
    from recipe_scrapers import SCRAPERS  # type: ignore
    return sorted(SCRAPERS.keys())


def _read_checkpoint(path: Path) -> dict:
    if not path.is_file():
        return {'completed': {}, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    return json.loads(path.read_text())


def _write_checkpoint(path: Path, ckpt: dict) -> None:
    path.write_text(json.dumps(ckpt, indent=2))


def sweep(
    *,
    skip_curated: bool,
    max_per_site: int,
    workers: int,
    sleep: float,
    user_id: str,
    checkpoint_path: Path,
    staging_dir: Path,
    upload: bool,
    only_domains: list[str] | None = None,
    discover_only: bool = False,
) -> None:
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')

    domains = only_domains or _supported_domains()
    print(f'Sweep target: {len(domains)} domains', flush=True)

    # The 9 sites the curated sitemap_discovery already drained at depth.
    # Skip them on the broad sweep so we don't re-do the deep mining we just ran.
    CURATED = {
        'allrecipes.com', 'simplyrecipes.com', 'seriouseats.com',
        'bbcgoodfood.com', 'bbc.co.uk', 'smittenkitchen.com',
        'budgetbytes.com', 'cookieandkate.com', 'food.com',
    }

    ckpt = _read_checkpoint(checkpoint_path)
    completed: dict = ckpt.setdefault('completed', {})

    print('Fetching existing source-URL set from Supabase ...', flush=True)
    existing = _existing_source_urls(sb_url, sb_key, user_id)
    print(f'  {len(existing)} programs already uploaded', flush=True)

    staging_dir.mkdir(parents=True, exist_ok=True)
    n_sites_done = n_sites_skipped = 0
    total_staged = total_uploaded = 0

    for i, domain in enumerate(domains, 1):
        if domain in completed:
            n_sites_skipped += 1
            continue
        if skip_curated and domain in CURATED:
            completed[domain] = {'status': 'curated_skip', 'urls': 0}
            _write_checkpoint(checkpoint_path, ckpt)
            n_sites_skipped += 1
            continue

        print(f'\n[{i}/{len(domains)}] {domain} ', flush=True)
        try:
            # 90s ceiling per domain — ~12 candidates × 8s timeout × IPv4/v6
            # adds up; this caps the worst case so a SYN-blackholing host
            # can't stall the run.
            discovered, source = _discover_with_budget(
                domain, max_urls=max_per_site, budget_s=90,
            )
        except _DiscoverTimeout as e:
            print(f'  discovery timeout: {e}', flush=True)
            completed[domain] = {'status': 'discover_timeout', 'error': str(e)}
            _write_checkpoint(checkpoint_path, ckpt)
            continue
        except Exception as e:
            print(f'  discovery error: {e}', flush=True)
            completed[domain] = {'status': 'discover_error', 'error': str(e)[:200]}
            _write_checkpoint(checkpoint_path, ckpt)
            continue

        if not discovered:
            completed[domain] = {'status': 'no_urls', 'source': ''}
            _write_checkpoint(checkpoint_path, ckpt)
            print('  no URLs', flush=True)
            continue

        # Drop ones we already uploaded
        new_urls = [u for u in discovered if u not in existing]
        print(f'  discovered={len(discovered)} new={len(new_urls)} via {source}', flush=True)

        if discover_only or not new_urls:
            completed[domain] = {
                'status': 'discover_ok' if discover_only else 'all_existing',
                'source': source,
                'urls': len(discovered),
                'new': len(new_urls),
            }
            _write_checkpoint(checkpoint_path, ckpt)
            n_sites_done += 1
            continue

        # Scrape this site's batch, then optionally upload
        staged_path = staging_dir / f'{domain.replace("/", "_")}.json'
        try:
            import_urls(new_urls, staged_path, sleep=sleep, workers=workers)
        except Exception as e:
            print(f'  scrape error: {e}', flush=True)
            completed[domain] = {'status': 'scrape_error',
                                 'error': str(e)[:200],
                                 'source': source}
            _write_checkpoint(checkpoint_path, ckpt)
            continue

        # Count what landed in the staged JSON
        try:
            staged = json.loads(staged_path.read_text()).get('staged') or []
        except Exception:
            staged = []
        total_staged += len(staged)

        n_uploaded = 0
        if upload and staged:
            try:
                _upload_staged(staged_path, idempotent=True, user_id=user_id)
                # Mark the URLs as existing so a re-run within this sweep
                # doesn't re-upload them.
                for s in staged:
                    if s.get('source_url'):
                        existing.add(s['source_url'])
                n_uploaded = len(staged)
                total_uploaded += n_uploaded
            except SystemExit as e:
                # _upload_staged sys.exits if env is missing; we already checked.
                print(f'  upload aborted: {e}', flush=True)
            except Exception as e:
                print(f'  upload error: {e}', flush=True)

        completed[domain] = {
            'status': 'ok',
            'source': source,
            'urls': len(discovered),
            'new': len(new_urls),
            'staged': len(staged),
            'uploaded': n_uploaded,
        }
        _write_checkpoint(checkpoint_path, ckpt)
        n_sites_done += 1
        print(f'  -> staged={len(staged)} uploaded={n_uploaded}'
              f' (running totals: staged={total_staged} uploaded={total_uploaded})',
              flush=True)

    print('\n=== Sweep summary ===', flush=True)
    print(f'  sites visited: {n_sites_done}, skipped: {n_sites_skipped}', flush=True)
    print(f'  total programs staged: {total_staged}', flush=True)
    print(f'  total programs uploaded: {total_uploaded}', flush=True)


def main(argv: Iterable[str] | None = None) -> int:
    _load_dotenv_if_present()
    ap = argparse.ArgumentParser(
        prog='rhylthyme-sweep-supported-sites',
        description=__doc__.split('\n\n')[0] if __doc__ else None,
    )
    ap.add_argument('--max-per-site', type=int, default=200,
                    help='Per-site URL cap (default: %(default)s).')
    ap.add_argument('--workers', type=int, default=8,
                    help='Concurrent scraper threads per site batch (default: %(default)s).')
    ap.add_argument('--sleep', type=float, default=1.0,
                    help='Min seconds between requests to the same host (default: %(default)s).')
    ap.add_argument('--user-id', default=DEFAULT_RECIPES_USER_ID,
                    help=f'Owner user_id (default: {DEFAULT_RECIPES_USER_ID}).')
    ap.add_argument('--checkpoint', default='/tmp/rs_sweep.checkpoint.json',
                    help='Resumable checkpoint file (default: %(default)s).')
    ap.add_argument('--staging-dir', default='/tmp/rs_sweep',
                    help='Directory for per-site staged JSON files (default: %(default)s).')
    ap.add_argument('--no-skip-curated', action='store_true',
                    help='Do not skip the 9 sites already drained by curated sitemap-discovery.')
    ap.add_argument('--no-upload', action='store_true',
                    help='Scrape and stage only; skip Supabase upload.')
    ap.add_argument('--only', nargs='+', default=None,
                    help='Restrict the sweep to these domain(s).')
    ap.add_argument('--discover-only', action='store_true',
                    help='Walk sitemaps and record counts; do not scrape.')
    ap.add_argument('--reset', action='store_true',
                    help='Delete the checkpoint file and start fresh.')
    args = ap.parse_args(argv)

    ckpt_path = Path(args.checkpoint)
    if args.reset and ckpt_path.exists():
        ckpt_path.unlink()

    sweep(
        skip_curated=not args.no_skip_curated,
        max_per_site=args.max_per_site,
        workers=args.workers,
        sleep=args.sleep,
        user_id=args.user_id,
        checkpoint_path=ckpt_path,
        staging_dir=Path(args.staging_dir),
        upload=not args.no_upload,
        only_domains=args.only,
        discover_only=args.discover_only,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
