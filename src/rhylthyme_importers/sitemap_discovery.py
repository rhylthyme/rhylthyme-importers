"""
Sitemap-based recipe URL discovery.

Walks a curated list of recipe-site sitemap (or sitemap-index) URLs, follows
nested sitemaps, and returns the subset of URLs that look like recipe pages.

Designed to feed ``recipe_scrapers_mine`` — only emits URLs whose domain is
parseable by the ``recipe-scrapers`` library.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.request
from typing import Iterable
from urllib.parse import urlparse


USER_AGENT = 'Mozilla/5.0 rhylthyme-importers/1.0 (+https://rhylthyme.com)'

# Curated list of sitemap entry points covering the largest, well-behaved
# recipe sites supported by recipe-scrapers. Each entry is a domain → list of
# sitemap URLs (often a single index file). Heuristic url-path filters per
# domain narrow sitemaps that mix recipes with editorial articles.
SITEMAPS: dict[str, dict] = {
    'allrecipes.com': {
        'sitemaps': ['https://www.allrecipes.com/sitemap_1.xml'],
        'path_re': re.compile(r'/recipe/\d+/'),
    },
    'simplyrecipes.com': {
        'sitemaps': [
            'https://www.simplyrecipes.com/sitemap_1.xml',
            'https://www.simplyrecipes.com/sitemap_2.xml',
        ],
        'path_re': re.compile(r'/recipes/'),
    },
    'seriouseats.com': {
        'sitemaps': [
            'https://www.seriouseats.com/sitemap_1.xml',
            'https://www.seriouseats.com/sitemap_2.xml',
        ],
        'path_re': re.compile(r'-recipe(-\d+)?$|/recipes/'),
    },
    'bbcgoodfood.com': {
        # bbcgoodfood's sitemap is a sitemap-index — the walker will follow.
        'sitemaps': ['https://www.bbcgoodfood.com/sitemap.xml'],
        'path_re': re.compile(r'/recipes/'),
    },
    'bbc.co.uk': {
        'sitemaps': ['https://www.bbc.co.uk/food/sitemap.xml'],
        'path_re': re.compile(r'/food/recipes/'),
    },
    'smittenkitchen.com': {
        'sitemaps': ['https://smittenkitchen.com/sitemap.xml'],
        # SK posts are at /YYYY/MM/slug/
        'path_re': re.compile(r'/\d{4}/\d{2}/'),
    },
    'budgetbytes.com': {
        'sitemaps': ['https://www.budgetbytes.com/post-sitemap.xml'],
        'path_re': re.compile(r'/[^/]+/?$'),
    },
    'cookieandkate.com': {
        'sitemaps': ['https://cookieandkate.com/post-sitemap.xml'],
        'path_re': re.compile(r'/[^/]+/?$'),
    },
    'food.com': {
        'sitemaps': ['https://www.food.com/sitemap.xml'],
        'path_re': re.compile(r'/recipe/'),
    },
}


def _fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    if url.endswith('.gz') or body[:2] == b'\x1f\x8b':
        body = gzip.decompress(body)
    return body.decode('utf-8', errors='replace')


_LOC_RE = re.compile(r'<loc>\s*([^<\s]+)\s*</loc>', re.I)


def _walk_sitemap(url: str, *, depth: int = 0, max_depth: int = 4) -> Iterable[str]:
    """Yield page URLs from a sitemap, recursively following sitemap-index files."""
    if depth > max_depth:
        return
    try:
        body = _fetch(url)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        print(f'  [sitemap] fetch failed {url}: {e}')
        return
    locs = _LOC_RE.findall(body)
    is_index = '<sitemapindex' in body[:500].lower()
    if is_index:
        for sub in locs:
            yield from _walk_sitemap(sub, depth=depth + 1, max_depth=max_depth)
    else:
        for loc in locs:
            yield loc


def discover(domains: Iterable[str] | None = None,
             *, max_per_site: int = 1000,
             max_total: int = 10000,
             verbose: bool = True) -> list[str]:
    """Walk sitemaps and return up to ``max_total`` recipe URLs.

    Caps per-site at ``max_per_site`` so a single huge feed doesn't dominate.
    Filters by per-domain regex to drop obvious non-recipe URLs (category
    pages, tag indexes, articles).
    """
    domain_filter = set(domains) if domains else None
    urls: list[str] = []
    for domain, config in SITEMAPS.items():
        if domain_filter and domain not in domain_filter:
            continue
        if len(urls) >= max_total:
            break
        path_re: re.Pattern = config['path_re']
        seen_for_site = 0
        if verbose:
            print(f'\n== {domain} ==')
        for sm in config['sitemaps']:
            if seen_for_site >= max_per_site or len(urls) >= max_total:
                break
            for u in _walk_sitemap(sm):
                if seen_for_site >= max_per_site or len(urls) >= max_total:
                    break
                # Quick host check — sitemaps occasionally cross-link.
                host = urlparse(u).netloc.lower()
                if not (host == domain or host.endswith('.' + domain)):
                    continue
                if not path_re.search(urlparse(u).path):
                    continue
                urls.append(u)
                seen_for_site += 1
        if verbose:
            print(f'  collected {seen_for_site} URLs (running total: {len(urls)})')
    return urls
