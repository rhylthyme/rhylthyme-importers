"""
Bulk-import recipes from the Cooklang Federation (https://recipes.cooklang.org).

The federation aggregates ~76 GitHub repos via /feeds. Cloning them directly
is faster and politer than hitting the federation's rate-limited API
(~4s per request). This module:

  1. Discovers the source repos by paging /feeds.
  2. Downloads each repo as a tarball, walks `.cook` files.
  3. Parses each via :class:`rhylthyme_importers.cooklang.CooklangImporter`.
  4. Applies a name filter (rejects "recipe", "untitled", "test", …) and a
     structural quality filter (rejects zero-ingredient placeholders).
  5. Writes a staged JSON file for human review.
  6. Optionally uploads staged programs to Supabase.

Reproducible workflow
---------------------
::

    # 1. Discover source repos
    python -m rhylthyme_importers.cooklang_federation --discover

    # 2. Scrape + filter, write staged JSON
    python -m rhylthyme_importers.cooklang_federation --import-all \\
        --staged-out /tmp/staged.json

    # 3. Review the sample
    python -m rhylthyme_importers.cooklang_federation --review /tmp/staged.json

    # 4. Re-apply filters after editing rules (no re-download)
    python -m rhylthyme_importers.cooklang_federation --refilter /tmp/staged.json

    # 5. Upload to Supabase (idempotent — skips entries whose source_url
    #    already exists for the target user)
    python -m rhylthyme_importers.cooklang_federation --upload /tmp/staged.json \\
        --idempotent

The CLI is also exposed as ``rhylthyme-import-cooklang-federation`` after
installing the package.

Environment variables (for ``--upload``)
----------------------------------------
- ``SUPABASE_URL`` — e.g. ``https://xxx.supabase.co``
- ``SUPABASE_SERVICE_ROLE_KEY`` — service role key (bypasses RLS)
- ``RHYLTHYME_RECIPES_USER_ID`` (optional) — owner UID for uploaded rows;
  defaults to the rhylthyme-recipes content account.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from .cooklang import CooklangImporter


FEDERATION_BASE = 'https://recipes.cooklang.org'
DEFAULT_RECIPES_USER_ID = '80a129d0-2137-4cca-bf31-f21a41aee815'


# ---------------------------------------------------------------------------
# Optional .env loader — looks in cwd, repo root, and rhylthyme-server/.
# Mirrors the loader used by other repo scripts so reproductions don't need
# users to manually export env vars.
# ---------------------------------------------------------------------------

def _load_dotenv_if_present():
    here = Path.cwd()
    candidates = [
        here / '.env',
        here / 'rhylthyme-server' / '.env',
        Path(__file__).resolve().parents[3] / 'rhylthyme-server' / '.env',
        Path(__file__).resolve().parents[3] / '.env',
    ]
    for p in candidates:
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip())
        return


# ---------------------------------------------------------------------------
# Step 1 — discover the source repos
# ---------------------------------------------------------------------------

def fetch_feeds_page():
    """Walk every page of /feeds and return ``(github_repos, other_urls)``.

    The federation paginates 20 feeds per page. We follow pages until no new
    GitHub repos appear.
    """
    github_repos = set()
    other_feeds = set()
    for page in range(1, 20):  # safety cap
        url = f'{FEDERATION_BASE}/feeds?page={page}'
        req = urllib.request.Request(url, headers={'User-Agent': 'rhylthyme-importers/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode('utf-8')
        except Exception as e:
            print(f'  page {page} fetch failed: {e}', file=sys.stderr)
            break
        page_repos = set(re.findall(r'href="(https://github\.com/[^/"]+/[^/"]+)"', html))
        page_repos = {r for r in page_repos if not r.startswith('https://github.com/cooklang')}
        if not page_repos:
            break
        if not (page_repos - github_repos):
            break
        github_repos.update(page_repos)
        for m in re.finditer(r'href="(https?://[^"]+)"', html):
            u = m.group(1)
            if 'github.com' in u or 'cooklang.org' in u or 'plausible' in u:
                continue
            if u.startswith(FEDERATION_BASE):
                continue
            other_feeds.add(u)
    return sorted(github_repos), sorted(other_feeds)


# ---------------------------------------------------------------------------
# Step 2 — download and walk .cook files in each repo
# ---------------------------------------------------------------------------

def github_owner_repo(url):
    """``https://github.com/foo/bar`` -> ``('foo', 'bar')`` (handles ``.git`` suffix)."""
    m = re.match(r'https?://github\.com/([^/]+)/([^/?#]+)', url)
    if not m:
        return None, None
    repo = m.group(2)
    if repo.endswith('.git'):
        repo = repo[:-4]
    return m.group(1), repo


def fetch_repo_tarball(owner, repo):
    """Download a repo tarball, trying ``main`` then ``master``. Returns
    ``(bytes, branch)`` or ``(None, None)``."""
    for branch in ('main', 'master'):
        url = f'https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'rhylthyme-importers/1.0'})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(), branch
        except Exception:
            continue
    return None, None


def walk_cook_files(tarball_bytes):
    """Yield ``(member_path, content_str)`` for each ``.cook`` file in the tarball."""
    bio = io.BytesIO(tarball_bytes)
    with tarfile.open(fileobj=bio, mode='r:gz') as tar:
        for m in tar.getmembers():
            if not m.isreg():
                continue
            if not m.name.lower().endswith('.cook'):
                continue
            try:
                f = tar.extractfile(m)
                if f is None:
                    continue
                yield m.name, f.read().decode('utf-8', errors='replace')
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Step 3 — quality filters
# ---------------------------------------------------------------------------

# Reject when the candidate program name matches any of these patterns.
# Single-word real food names (Roux, Filo, Dahl, Toum, Ash, Dal, Poi, V60)
# pass — short doesn't mean low-quality.
_BAD_NAME_PATTERNS = [
    r'^\s*$',                                                       # empty
    r'^.{1}$',                                                      # 1 char only
    r'^(untitled|new recipe|recipe|recipes|test|draft|wip|todo|temp|tmp|example|examples|template|templates|placeholder|notes?|new|sample|stub|cfmt)$',
    r'^(smoke[\s_-]?test|smoketest)([\s_-]?(recipe|dish|food))?$',  # "Smoke Test Recipe"
    r'^(recipe|untitled|new|test|draft|wip|todo)[\s_\-]*\d*$',      # "recipe 1"
    r'^copy[\s_-]?of\b',                                            # "Copy of …"
    r'^.+\([\s\d]+\)$',                                             # "Foo (1)"
    r'^(asdf|qwer|qwerty|hello|foo|bar|baz|lorem)\b',
    r'^\d{4,}$',                                                    # all-digit IDs
    r'^[\W_]+$',                                                    # only punctuation
]
_BAD_NAME_RE = re.compile('|'.join(_BAD_NAME_PATTERNS), re.IGNORECASE)


def is_poorly_named(name: str) -> bool:
    if not name:
        return True
    s = re.sub(r'\.cook$', '', name.strip(), flags=re.IGNORECASE).strip()
    if not s:
        return True
    if _BAD_NAME_RE.match(s):
        return True
    if re.fullmatch(r'[0-9a-f]{8,}', s, re.IGNORECASE):
        return True  # hash-like
    if re.fullmatch(r'[bcdfghjklmnpqrstvwxyz]{5,}', s, re.IGNORECASE):
        return True  # consonant mash
    return False


def quality_issues(program: dict) -> list:
    """Return a list of human-readable quality issues, or ``[]`` if OK.

    Conservative: only rejects clear placeholders. Spice mixes and one-pot
    recipes can legitimately be short ("Garlic Salt": 2 ingredients, 1 step,
    30 chars prose) — those pass.
    """
    issues = []
    meta = program.get('metadata') or {}
    n_ing = len((meta.get('ingredients') or []))
    steps = [s for t in (program.get('tracks') or []) for s in (t.get('steps') or [])]
    n_steps = len(steps)
    total_prose = sum(len((s.get('description') or '')) for s in steps)

    if n_steps == 0:
        issues.append('zero steps')
    if n_ing == 0:
        issues.append('zero ingredients')
    if total_prose < 30 and n_ing == 0:
        issues.append(f'empty body (prose={total_prose}, ing=0)')
    return issues


def stem_to_title(stem: str) -> str:
    return stem.replace('-', ' ').replace('_', ' ').strip()


# ---------------------------------------------------------------------------
# Pipeline — discover → import → filter → write staged JSON
# ---------------------------------------------------------------------------

def import_all(staged_out: Path):
    print('Fetching feeds pages...')
    repos, other_feeds = fetch_feeds_page()
    print(f'  {len(repos)} GitHub repos, {len(other_feeds)} non-github feeds (skipped).\n')

    importer = CooklangImporter()
    staged, skipped_name, skipped_parse, skipped_quality, repo_stats = [], [], [], [], []

    for i, repo_url in enumerate(repos, 1):
        owner, repo = github_owner_repo(repo_url)
        if not owner:
            continue
        print(f'[{i}/{len(repos)}] {owner}/{repo} ...', end=' ', flush=True)
        tar_bytes, branch = fetch_repo_tarball(owner, repo)
        if not tar_bytes:
            print('FAILED to download')
            repo_stats.append({'repo': repo_url, 'cook_files': 0, 'imported': 0, 'error': 'download_failed'})
            continue

        n_cook, n_imported = 0, 0
        try:
            for member_path, content in walk_cook_files(tar_bytes):
                n_cook += 1
                stem = Path(member_path).stem
                title_from_filename = stem_to_title(stem)

                if is_poorly_named(title_from_filename):
                    skipped_name.append({
                        'repo': repo_url, 'path': member_path,
                        'reason': 'filename', 'name': title_from_filename,
                    })
                    continue

                source_url = (
                    f'https://github.com/{owner}/{repo}/blob/{branch}/'
                    f'{urllib.parse.quote(member_path.split("/", 1)[-1])}'
                )
                result = importer.import_from_content(
                    content, source_name=stem, source_url=source_url,
                )
                if not result.success:
                    skipped_parse.append({'repo': repo_url, 'path': member_path, 'error': result.error})
                    continue

                program = result.program
                if is_poorly_named(program.get('name', '')):
                    skipped_name.append({
                        'repo': repo_url, 'path': member_path,
                        'reason': 'program_name', 'name': program.get('name', ''),
                    })
                    continue

                issues = quality_issues(program)
                if issues:
                    skipped_quality.append({
                        'repo': repo_url, 'path': member_path,
                        'name': program.get('name', ''), 'issues': issues,
                    })
                    continue

                staged.append({
                    'repo': repo_url,
                    'path': member_path,
                    'source_url': source_url,
                    'name': program.get('name', ''),
                    'program': program,
                })
                n_imported += 1
        except Exception as e:
            print(f'(error: {e})', end=' ')

        print(f'{n_cook} .cook files, {n_imported} imported')
        repo_stats.append({'repo': repo_url, 'cook_files': n_cook, 'imported': n_imported})

    output = {
        'staged': staged,
        'skipped_name': skipped_name,
        'skipped_parse': skipped_parse,
        'skipped_quality': skipped_quality,
        'repo_stats': repo_stats,
        'totals': {
            'staged': len(staged),
            'skipped_name': len(skipped_name),
            'skipped_parse': len(skipped_parse),
            'skipped_quality': len(skipped_quality),
            'repos': len(repo_stats),
        },
    }
    staged_out.write_text(json.dumps(output, indent=2))
    print(
        f'\nWrote {staged_out} — '
        f'{len(staged)} staged, '
        f'{len(skipped_name)} name-skipped, '
        f'{len(skipped_quality)} quality-skipped, '
        f'{len(skipped_parse)} parse-skipped'
    )


def refilter(in_path: Path, out_path: Path):
    """Re-apply filters to an already-staged JSON file (no re-download)."""
    data = json.loads(in_path.read_text())
    pool = list(data.get('staged') or [])
    staged, skipped_name, skipped_quality = [], [], []
    for r in pool:
        name = r.get('name', '')
        if is_poorly_named(name):
            skipped_name.append({**r, 'reason': 'program_name'})
            continue
        issues = quality_issues(r.get('program') or {})
        if issues:
            skipped_quality.append({
                'repo': r.get('repo'), 'path': r.get('path'),
                'name': name, 'issues': issues,
            })
            continue
        staged.append(r)

    output = {
        **data,
        'staged': staged,
        'skipped_name': (data.get('skipped_name') or []) + skipped_name,
        'skipped_quality': (data.get('skipped_quality') or []) + skipped_quality,
        'totals': {
            'staged': len(staged),
            'skipped_name': len((data.get('skipped_name') or []) + skipped_name),
            'skipped_parse': len(data.get('skipped_parse') or []),
            'skipped_quality': len((data.get('skipped_quality') or []) + skipped_quality),
            'repos': len(data.get('repo_stats') or []),
        },
    }
    out_path.write_text(json.dumps(output, indent=2))
    print(
        f'Refiltered {in_path}: pool={len(pool)} -> '
        f'staged={len(staged)}, +{len(skipped_name)} name-skipped, '
        f'+{len(skipped_quality)} quality-skipped'
    )


def review(staged_path: Path, sample_n: int = 30):
    data = json.loads(staged_path.read_text())
    s = data.get('staged') or []
    sn = data.get('skipped_name') or []
    sp = data.get('skipped_parse') or []
    sq = data.get('skipped_quality') or []
    print(
        f'=== Totals: staged={len(s)} '
        f'name-skipped={len(sn)} '
        f'quality-skipped={len(sq)} '
        f'parse-skipped={len(sp)} ===\n'
    )

    print(f'--- Sample of {min(sample_n, len(s))} staged names ---')
    for r in s[:sample_n]:
        print(f'  {r["name"]:50}  ({r["repo"].split("/")[-1]})')

    print(f'\n--- Sample of {min(sample_n, len(sn))} name-skipped (rejected) ---')
    for r in sn[:sample_n]:
        print(f'  [{r.get("reason","-"):13}] {r["name"]:30}  {r["path"]}')

    if sq:
        print(f'\n--- Sample of {min(sample_n, len(sq))} quality-skipped ---')
        for r in sq[:sample_n]:
            issues = ', '.join(r.get('issues') or [])
            print(f'  {r.get("name",""):40}  [{issues}]  {r.get("path","")}')

    if sp:
        print(f'\n--- Sample of {min(10, len(sp))} parse-skipped ---')
        for r in sp[:10]:
            print(f'  {r["path"]} :: {r.get("error","")[:80]}')

    print(f'\n--- Per-repo: top 10 by imported count ---')
    for r in sorted(data.get('repo_stats') or [], key=lambda x: -x.get('imported', 0))[:10]:
        print(f'  {r.get("imported",0):4} / {r.get("cook_files",0):4}  {r["repo"]}')


# ---------------------------------------------------------------------------
# Step 4 — upload to Supabase
# ---------------------------------------------------------------------------

def _existing_source_urls(supabase_url: str, key: str, user_id: str) -> set:
    """Return the set of source URLs already present for ``user_id``.

    Used by ``--idempotent`` to skip rows that have been uploaded before.
    Walks the table in 1000-row pages.
    """
    seen = set()
    offset = 0
    while True:
        url = (
            f'{supabase_url}/rest/v1/programs?'
            f'user_id=eq.{user_id}&select=program_json&limit=1000&offset={offset}'
        )
        req = urllib.request.Request(url, headers={
            'apikey': key, 'Authorization': f'Bearer {key}',
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            rows = json.loads(resp.read().decode('utf-8'))
        if not rows:
            break
        for row in rows:
            pj = row.get('program_json') or {}
            if isinstance(pj, str):
                try:
                    pj = json.loads(pj)
                except Exception:
                    continue
            src_url = ((pj.get('metadata') or {}).get('source') or {}).get('url')
            if isinstance(src_url, str) and src_url:
                seen.add(src_url)
        if len(rows) < 1000:
            break
        offset += 1000
    return seen


def _unique_program_id(base_id: str, source_url: str) -> str:
    """Append a short stable hash of the source URL so program_ids stay unique
    even when multiple recipes share a name. The Supabase ``programs`` table
    has a UNIQUE(user_id, program_id) constraint and the federation has many
    same-named recipes across contributor repos (e.g. ~30 different "Pancakes",
    multiple "Lasagna"/"Crème Brûlée"/"Tiramisù").
    """
    import hashlib
    base = (base_id or 'recipe').strip('_-') or 'recipe'
    h = hashlib.sha1(source_url.encode('utf-8')).hexdigest()[:6]
    # Cap base length so the suffixed id fits within 50 chars (matches base.py).
    return f'{base[:43]}_{h}'


def upload(staged_path: Path, *, limit=None, start=0, idempotent=False, user_id=None):
    supabase_url = os.environ.get('SUPABASE_URL')
    supabase_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not supabase_url or not supabase_key:
        sys.exit('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set')

    target_user = user_id or os.environ.get('RHYLTHYME_RECIPES_USER_ID') or DEFAULT_RECIPES_USER_ID

    data = json.loads(staged_path.read_text())
    staged = data['staged'][start:]
    if limit:
        staged = staged[:limit]

    seen = set()
    if idempotent:
        print(f'Fetching existing source URLs for user {target_user} ...')
        seen = _existing_source_urls(supabase_url, supabase_key, target_user)
        print(f'  {len(seen)} programs already uploaded; will skip those.')

    print(f'Uploading {len(staged)} programs to Supabase as user {target_user} ...')
    n_ok = n_err = n_skip = 0
    for i, r in enumerate(staged, 1):
        if idempotent and r.get('source_url') in seen:
            n_skip += 1
            continue
        program = r['program']
        meta = program.setdefault('metadata', {})
        src = meta.setdefault('source', {})
        src.setdefault('url', r.get('source_url', ''))
        src.setdefault('type', 'cooklang-federation')

        # Make program_id unique-per-source so name collisions across repos
        # don't violate the UNIQUE(user_id, program_id) constraint.
        unique_pid = _unique_program_id(program.get('programId') or '', r.get('source_url', ''))
        program['programId'] = unique_pid

        # Populate the top-level ``environment`` column so the gallery
        # filter can use it without scanning JSONB on millions of rows.
        env = (program.get('environmentType') or '').strip().lower() or None
        row = {
            'user_id': target_user,
            'name': program.get('name') or 'Untitled',
            'program_id': unique_pid,
            'description': program.get('description') or '',
            'program_json': program,
            'schema_version': program.get('schemaVersion') or '0.2.0-alpha',
            'is_public': True,
            'environment': env,
        }
        try:
            req = urllib.request.Request(
                f'{supabase_url}/rest/v1/programs',
                data=json.dumps(row).encode('utf-8'),
                headers={
                    'apikey': supabase_key,
                    'Authorization': f'Bearer {supabase_key}',
                    'Content-Type': 'application/json',
                    'Prefer': 'return=minimal',
                },
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
                n_ok += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:200]
            n_err += 1
            if n_err <= 5:
                print(f'  [{i}] HTTP {e.code} for {row["name"]!r}: {body}')
        except Exception as e:
            n_err += 1
            if n_err <= 5:
                print(f'  [{i}] error for {row["name"]!r}: {e}')

        if i % 100 == 0:
            print(f'  ...{i}/{len(staged)} ({n_ok} ok, {n_skip} skip, {n_err} err)')

    print(f'\nDone: {n_ok} uploaded, {n_skip} skipped, {n_err} errors.')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    _load_dotenv_if_present()

    ap = argparse.ArgumentParser(
        prog='rhylthyme-import-cooklang-federation',
        description=__doc__.split('\n\n')[0] if __doc__ else None,
    )
    ap.add_argument('--discover', action='store_true', help='List source repos only')
    ap.add_argument('--import-all', action='store_true', help='Walk all repos and write staged JSON')
    ap.add_argument('--review', metavar='STAGED_JSON', help='Show a sample of staged + skipped')
    ap.add_argument('--refilter', metavar='STAGED_JSON', help='Re-apply filters to existing staged JSON')
    ap.add_argument('--upload', metavar='STAGED_JSON', help='Upload staged programs to Supabase')
    ap.add_argument('--staged-out', default='/tmp/cooklang_federation_staged.json',
                    help='Output path for --import-all (default: %(default)s)')
    ap.add_argument('--limit', type=int, default=None, help='Max programs for --upload (debug)')
    ap.add_argument('--start', type=int, default=0, help='Index to start --upload from')
    ap.add_argument('--idempotent', action='store_true',
                    help='Skip programs whose source_url is already in Supabase')
    ap.add_argument('--user-id', default=None,
                    help='Override target user_id for --upload (default: rhylthyme-recipes account)')
    args = ap.parse_args(argv)

    if args.discover:
        repos, other = fetch_feeds_page()
        print('--- GitHub repos ---')
        for r in repos:
            print(r)
        if other:
            print('\n--- Other (non-github) feeds, NOT scraped ---')
            for u in other:
                print(u)
        print(f'\nTotal: {len(repos)} GitHub repos + {len(other)} other feeds')
        return 0

    if args.import_all:
        import_all(Path(args.staged_out))
        return 0

    if args.refilter:
        in_path = Path(args.refilter)
        out_path = (
            Path(args.staged_out)
            if args.staged_out != '/tmp/cooklang_federation_staged.json'
            else in_path.with_suffix('.refiltered.json')
        )
        refilter(in_path, out_path)
        return 0

    if args.review:
        review(Path(args.review))
        return 0

    if args.upload:
        upload(
            Path(args.upload),
            limit=args.limit, start=args.start,
            idempotent=args.idempotent, user_id=args.user_id,
        )
        return 0

    ap.print_help()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
