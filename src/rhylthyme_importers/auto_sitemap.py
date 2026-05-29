"""
Generic sitemap auto-discovery.

Given any domain (e.g. ``allrecipes.com``), tries the conventional sitemap
entry points and walks them recursively, returning recipe-shaped page URLs.

Strategy (in order, stops at the first non-empty result):
  1. ``/robots.txt`` ``Sitemap:`` directives
  2. ``/sitemap.xml`` (most common)
  3. ``/sitemap_index.xml``        (WordPress / Yoast)
  4. ``/wp-sitemap.xml``           (WordPress core)
  5. ``/sitemap-index.xml``        (some custom CMSes)
  6. ``/post-sitemap.xml``         (Yoast post sitemap)

Index files (``<sitemapindex>``) are followed recursively up to ``max_depth``.

URL filtering:
  - Host must match the requested domain (drops cross-links)
  - Path heuristic identifies likely recipe pages (default: contains
    ``/recipe`` or ``/recipes/`` or ``-recipe-`` etc.)
  - Falls back to "any URL on the same host" if the recipe heuristic
    matches nothing — many small food blogs put recipes at ``/{slug}/``
    with no ``recipe`` token in the path.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.request
from typing import Iterable
from urllib.parse import urlparse


USER_AGENT = (
    'Mozilla/5.0 rhylthyme-importers/1.0 '
    '(+https://rhylthyme.com; contact: leipzig@gmail.com)'
)

# Conservative path filter — most recipe-scrapers-supported sites put
# recipes either under ``/recipe(s)/`` or use a ``-recipe-`` slug suffix.
DEFAULT_RECIPE_PATH_RE = re.compile(
    r'/recipe(s)?/'              # /recipe/ or /recipes/
    r'|-recipe(-\d+)?(/|$)'      # foo-recipe-123 or foo-recipe/
    r'|/recipe-[^/]+/?$',        # /recipe-foo-bar/
    re.I,
)


def _fetch(url: str, timeout: int = 8) -> bytes | None:
    """Fetch a URL with a short timeout. Default 8s — sweep mode tries up to
    ~24 candidate URLs per domain, so a slow host shouldn't be allowed to
    block for more than a couple of minutes total."""
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None
    if url.endswith('.gz') or data[:2] == b'\x1f\x8b':
        try:
            data = gzip.decompress(data)
        except OSError:
            return None
    return data


def _decode(data: bytes) -> str:
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('utf-8', errors='replace')


def _sitemap_urls_from_robots(domain: str) -> list[str]:
    body = _fetch(f'https://{domain}/robots.txt', timeout=6)
    if not body:
        body = _fetch(f'https://www.{domain}/robots.txt', timeout=6)
    if not body:
        return []
    text = _decode(body)
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith('sitemap:'):
            urls.append(line.split(':', 1)[1].strip())
    return urls


_LOC_RE = re.compile(r'<loc>\s*([^<\s]+)\s*</loc>', re.I)


def _walk(url: str, depth: int = 0, max_depth: int = 4) -> Iterable[str]:
    if depth > max_depth:
        return
    body = _fetch(url, timeout=30)
    if not body:
        return
    text = _decode(body)
    is_index = '<sitemapindex' in text[:600].lower()
    locs = _LOC_RE.findall(text)
    if is_index:
        for sub in locs:
            yield from _walk(sub, depth + 1, max_depth)
    else:
        for loc in locs:
            yield loc


def discover_for_domain(
    domain: str,
    *,
    max_urls: int = 1000,
    path_re: re.Pattern[str] | None = DEFAULT_RECIPE_PATH_RE,
    fallback_to_any: bool = True,
) -> tuple[list[str], str]:
    """Auto-discover recipe-page URLs for one domain.

    Returns ``(urls, source)`` where ``source`` is the sitemap entry URL
    that worked (or ``''`` if none). ``urls`` is deduped, capped at
    ``max_urls``, and filtered by ``path_re`` if provided.
    """
    domain = domain.lower().lstrip('.')
    candidates: list[str] = []

    # Order: robots, then conventional paths
    candidates.extend(_sitemap_urls_from_robots(domain))
    for suffix in (
        'sitemap.xml',
        'sitemap_index.xml',
        'wp-sitemap.xml',
        'sitemap-index.xml',
        'post-sitemap.xml',
        'sitemap_1.xml',
    ):
        candidates.append(f'https://{domain}/{suffix}')
        candidates.append(f'https://www.{domain}/{suffix}')

    seen_sm: set[str] = set()
    for sm in candidates:
        if sm in seen_sm:
            continue
        seen_sm.add(sm)
        urls = list(_collect(sm, domain, max_urls, path_re))
        if urls:
            return urls, sm

    # Fallback: try once more with no path filter (some food blogs have
    # recipes at /{slug}/ and the default regex would skip them).
    if fallback_to_any and path_re is not None:
        for sm in candidates:
            urls = list(_collect(sm, domain, max_urls, path_re=None))
            if urls:
                return urls, sm + ' (no-path-filter)'

    return [], ''


def _collect(sitemap_url: str, domain: str, cap: int,
             path_re: re.Pattern[str] | None) -> Iterable[str]:
    """Walk a sitemap and yield up to ``cap`` URLs that match the host
    + path filter. Skips known non-recipe sitemap names like
    'image' / 'video' / 'category' / 'tag'."""
    if any(skip in sitemap_url.lower() for skip in (
        'image-sitemap', 'video-sitemap', 'category-sitemap',
        'tag-sitemap', 'author-sitemap',
    )):
        return
    seen: set[str] = set()
    n = 0
    for u in _walk(sitemap_url):
        if n >= cap:
            return
        try:
            p = urlparse(u)
        except Exception:
            continue
        host = p.netloc.lower()
        if not host:
            continue
        if not (host == domain or host.endswith('.' + domain)
                or host == 'www.' + domain):
            continue
        if path_re is not None and not path_re.search(p.path):
            continue
        if u in seen:
            continue
        seen.add(u)
        n += 1
        yield u
