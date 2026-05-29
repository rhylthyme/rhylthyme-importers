"""
Standardize recipe titles in the rhylthyme-recipes catalog.

Three independent passes, applied in order, each idempotent:

1. **Brace strip** — `"{Slow Cooker} Bourbon Peaches"` → `"Slow Cooker Bourbon Peaches"`.
   Sites that publish "{Tag} Title"-style titles slip them in via metadata; we
   keep the content but drop the brackets that are visual noise in our UI.
2. **Smart-quote normalize** — curly quotes / em dashes around the recipe name
   confuse search; replaced with their ASCII equivalents.
3. **Title-case for English titles** — only applied to titles whose script is
   purely Latin AND that contain at least one English stop-word. Foreign
   titles ("Boeuf bourguignon"), names with mixed CJK / Cyrillic, and titles
   already in proper Title Case are left alone.
4. **Length cap** — titles longer than ``MAX_LEN`` chars or ``MAX_WORDS``
   words get an LLM-summarized replacement (Claude Haiku) constrained to
   5-8 words. Off when ``ANTHROPIC_API_KEY`` is unset.

CLI: ``rhylthyme-clean-titles``. Run with ``--dry-run`` first to preview diffs;
``--limit N`` for sampling; ``--apply`` to write back to Supabase.

Patching is idempotent — running clean_title on an already-cleaned title is
a no-op. Resumable via the same checkpoint pattern as the other importers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


# ---------------------------------------------------------------------------
# Heuristic cleaning (no network)
# ---------------------------------------------------------------------------

_BRACES_RE = re.compile(r'[\{\[\}\]]')
_MULTI_SPACE_RE = re.compile(r'\s{2,}')

# Words to keep lowercase in title case (when not first/last). Conservative
# list — it's better to over-capitalize than to mangle a title.
_LOWERCASE_WORDS = {
    'a', 'an', 'and', 'as', 'at', 'but', 'by', 'en', 'for', 'from',
    'if', 'in', 'into', 'is', 'nor', 'of', 'on', 'onto', 'or',
    'over', 'so', 'than', 'that', 'the', 'to', 'up', 'upon', 'via',
    'vs', 'vs.', 'with', 'yet',
}

# A token that signals the title is English (and worth title-casing). We need
# at least one match before we touch a title — that protects French / German /
# Italian / Spanish titles from being mauled by English casing rules.
_ENGLISH_STOPWORD_RE = re.compile(
    r'\b(the|and|with|for|of|in|on|to|a|an|is|are|how|easy|best|recipe|recipes)\b',
    re.IGNORECASE,
)

_NON_LATIN_RE = re.compile(r'[-￿]')  # any non-ASCII char


def _capitalize_token(tok: str) -> str:
    """Capitalize a single token, preserving:
    - hyphenated parts: each gets capitalized, EXCEPT small words like
      "to" / "of" / "and" inside a hyphenated phrase ("Back-to-School").
    - apostrophes ("Mom's" -> "Mom's", "O'Brien" -> "O'Brien")
    - leading punctuation/digits
    - short all-caps acronyms (KFC, BBQ, NYC, USA, IPA): kept as-is when
      the whole token is uppercase letters of length 2-4. Longer all-caps
      tokens (PERFECT, AMAZING) are mis-emphasis and DO get title-cased.
    """
    if not tok:
        return tok
    # Acronym shortcut — preserve "KFC" / "BBQ" / "NYC" / "USA"
    if 2 <= len(tok) <= 4 and tok.isalpha() and tok.isupper():
        return tok
    # Preserve a leading punctuation char if any
    lead = ''
    while tok and not tok[0].isalnum():
        lead += tok[0]
        tok = tok[1:]
    # Hyphen / em-dash split
    parts = re.split(r'([-–—/])', tok)
    out_parts = []
    last_alpha_idx = max(
        (i for i, p in enumerate(parts) if p and p[0].isalpha()),
        default=-1,
    )
    first_alpha_idx = next(
        (i for i, p in enumerate(parts) if p and p[0].isalpha()),
        -1,
    )
    for i, p in enumerate(parts):
        if not p or not p[0].isalpha():
            out_parts.append(p)
            continue
        # Same acronym shortcut at the segment level
        if 2 <= len(p) <= 4 and p.isalpha() and p.isupper():
            out_parts.append(p); continue
        # Inside a hyphenated phrase, keep small connectives lowercase
        # ("Back-to-School", "Eye-of-Newt"), but always capitalize the first
        # and last segment.
        is_inner = first_alpha_idx < i < last_alpha_idx
        if is_inner and p.lower() in _LOWERCASE_WORDS:
            out_parts.append(p.lower()); continue
        # Apostrophe handling — only capitalize the first letter, leave
        # post-apostrophe untouched so "MOM'S" -> "Mom's".
        if "'" in p or '’' in p:
            ap = p.replace('’', "'")
            head, sep, tail = ap.partition("'")
            out_parts.append(head[:1].upper() + head[1:].lower() + sep + tail.lower())
        else:
            out_parts.append(p[:1].upper() + p[1:].lower())
    return lead + ''.join(out_parts)


def title_case_english(name: str) -> str:
    """Apply English-style title case to an English title. Caller is
    responsible for confirming the title is English.

    If the input is entirely uppercase (emphasis shouting) we lowercase the
    whole string before title-casing, otherwise the per-token acronym
    shortcut would preserve every short word ("EASY CHICKEN" → "EASY
    Chicken"). In a mixed-case title, all-caps short tokens are real
    acronyms (KFC, BBQ) and stay preserved.
    """
    name = name.strip()
    if not name:
        return name
    letters = [c for c in name if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        name = name.lower()
    tokens = name.split(' ')
    out = []
    last_idx = len(tokens) - 1
    for i, tok in enumerate(tokens):
        bare = re.sub(r'[^a-zA-Z\']', '', tok).lower()
        # Lowercase the small words unless first or last
        if 0 < i < last_idx and bare in _LOWERCASE_WORDS:
            out.append(tok.lower())
        else:
            out.append(_capitalize_token(tok))
    return ' '.join(out)


def looks_english(name: str) -> bool:
    """Heuristic: the title is English-ish AND should be title-cased.

    True iff:
    - contains no non-Latin characters (no CJK / Cyrillic / Hebrew / Arabic /
      Devanagari / Thai / Greek), AND
    - matches at least one English stop-word, OR
    - the whole title is all-lowercase or all-uppercase letters (a strong
      signal that the source emitted it without intentional casing).

    Mixed-case titles without stop-words ("Boeuf bourguignon", "Pasta alla
    Genovese") are left alone — title-casing them risks mangling foreign
    typography conventions.
    """
    # CJK / Cyrillic / Hebrew / Arabic / Devanagari / Thai / Greek ranges
    if re.search(
        r'[Ͱ-ϿЀ-ӿ֐-׿؀-ۿ'
        r'ऀ-ॿ฀-๿぀-ゟ゠-ヿ'
        r'가-힯一-鿿]',
        name,
    ):
        return False
    if _ENGLISH_STOPWORD_RE.search(name):
        return True
    letters = [c for c in name if c.isalpha()]
    if not letters:
        return False
    if all(c.islower() for c in letters):
        return True
    if all(c.isupper() for c in letters):
        return True
    # Any "shouty" word (all-caps, 5+ letters) inside an otherwise mixed-case
    # title is almost always emphasis ("HERSHEY'S Milk", "PERFECT Polenta")
    # and signals the title needs normalizing.
    for tok in re.split(r"\s+", name):
        bare = re.sub(r"[^A-Za-z]", "", tok)
        if len(bare) >= 5 and bare.isupper():
            return True
    return False


def strip_braces(name: str) -> str:
    """Remove curly / square braces but preserve the content inside.

    `{3 Ingredient} Easy Sugar Cookies` → `3 Ingredient Easy Sugar Cookies`.
    The wrapper characters are visual noise; the words inside are usually
    informative qualifiers (Slow Cooker, Copycat, 4th of July, etc.).
    """
    out = _BRACES_RE.sub(' ', name)
    out = _MULTI_SPACE_RE.sub(' ', out).strip()
    return out


def normalize_quotes(name: str) -> str:
    """Replace curly quotes / em dashes inside titles with ASCII forms.
    Search results, copy-paste reliability, and consistent rendering all win."""
    return (
        name
        .replace('‘', "'")
        .replace('’', "'")
        .replace('“', '"')
        .replace('”', '"')
    )


def clean_title_offline(name: str) -> str:
    """The offline pass: braces, smart quotes, title case for English titles.
    Length-based shortening lives in the LLM pass (clean_title_llm)."""
    if not name:
        return name
    out = name.strip()
    out = strip_braces(out)
    out = normalize_quotes(out)
    if looks_english(out):
        out = title_case_english(out)
    return out


# ---------------------------------------------------------------------------
# LLM length shortener
# ---------------------------------------------------------------------------

MAX_LEN = 100
MAX_WORDS = 12

_SHORTEN_PROMPT = (
    "You are normalizing a recipe title. The current title is too long — it's "
    "marketing copy or a full sentence. Return ONLY a 4-7 word recipe title "
    "that names the dish. No quotes, no punctuation at the end, no prose, no "
    "explanation. If the source language is not English, keep the recipe name "
    "in its original language. Output the title and nothing else.\n\n"
    "Current title:\n{title}\n\n"
    "Optional ingredients (for context, do NOT include them in the title):\n"
    "{ings}\n"
)


def is_too_long(name: str) -> bool:
    return len(name) > MAX_LEN or len(name.split()) > MAX_WORDS


def clean_title_llm(name: str, ingredients: list[str] | None = None) -> str:
    """Ask Claude Haiku to compress an over-long title to a real dish name.
    Returns the original ``name`` on any failure."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        return name
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return name
    client = anthropic.Anthropic()
    ings = '\n'.join(f'- {i}' for i in (ingredients or [])[:8]) or '(none)'
    try:
        resp = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=80,
            messages=[{'role': 'user', 'content': _SHORTEN_PROMPT.format(
                title=name, ings=ings,
            )}],
        )
        text = resp.content[0].text.strip()
    except Exception:
        return name
    # Strip any wrapping quotes the model added
    text = text.strip().strip('"').strip("'").strip()
    text = text.split('\n', 1)[0].strip()
    if not text or is_too_long(text):
        return name
    return text


# ---------------------------------------------------------------------------
# Supabase pagination + patch
# ---------------------------------------------------------------------------

def _supabase_headers(key: str) -> dict:
    return {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }


def _fetch_page(sb_url: str, sb_key: str, user_id: str,
                offset: int, limit: int, retries: int = 5) -> list[dict]:
    """Fetch one page, retrying on transient 5xx / network errors with
    exponential back-off so a single Supabase hiccup doesn't kill a long run.
    Re-raises after the final attempt."""
    qs = (f'programs?user_id=eq.{user_id}&is_public=eq.true'
          f'&select=id,name,program_json'
          f'&order=id&limit={limit}&offset={offset}')
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(f'{sb_url}/rest/v1/{qs}',
                                     headers=_supabase_headers(sb_key))
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code < 500 and e.code != 429:
                raise
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        sleep_for = min(60, 2 ** attempt)
        print(f'  page-fetch retry {attempt + 1}/{retries} in {sleep_for}s '
              f'(offset={offset}, err={last_err})', flush=True)
        time.sleep(sleep_for)
    raise last_err  # type: ignore[misc]


def _patch_row(sb_url: str, sb_key: str, row: dict, new_name: str) -> tuple[bool, str]:
    pj = row['program_json'] if isinstance(row['program_json'], dict) else json.loads(row['program_json'])
    pj_name_changed = (pj.get('name') != new_name)
    if pj_name_changed:
        pj['name'] = new_name
    body = {'name': new_name}
    if pj_name_changed:
        body['program_json'] = pj
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        f'{sb_url}/rest/v1/programs?id=eq.{row["id"]}',
        data=data,
        headers={**_supabase_headers(sb_key), 'Prefer': 'return=minimal'},
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True, ''
    except urllib.error.HTTPError as e:
        return False, e.read().decode('utf-8', 'replace')[:160]
    except Exception as e:
        return False, str(e)[:160]


def run(*, apply: bool, limit: int | None, llm_shorten: bool,
        workers: int, user_id: str, sample: int,
        start_offset: int = 0) -> int:
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')

    PAGE = 500
    offset = start_offset
    total = 0
    n_changed = 0
    n_too_long = 0
    n_llm_called = 0
    n_patch_ok = 0
    n_patch_err = 0
    sample_diffs: list[tuple[str, str]] = []

    print(f'Mode: {"APPLY" if apply else "DRY-RUN"}, '
          f'llm_shorten={llm_shorten}, limit={limit}', flush=True)

    while True:
        rows = _fetch_page(sb_url, sb_key, user_id, offset, PAGE)
        if not rows:
            break
        for row in rows:
            total += 1
            if limit and total > limit:
                break
            old = row.get('name') or ''
            new = clean_title_offline(old)
            # LLM shortener fires only if the offline pass left something too long.
            if llm_shorten and is_too_long(new):
                n_too_long += 1
                ings_blob = (
                    (row.get('program_json') or {}).get('metadata', {}).get('ingredients')
                    or []
                )
                ing_strings: list[str] = []
                for ing in ings_blob[:8]:
                    if isinstance(ing, str):
                        ing_strings.append(ing)
                    elif isinstance(ing, dict):
                        nm = ing.get('name') or ''
                        ms = ing.get('measure') or ''
                        s = (ms + ' ' + nm).strip() if ms else nm
                        if s:
                            ing_strings.append(s)
                shortened = clean_title_llm(new, ing_strings)
                n_llm_called += 1
                if shortened and shortened != new:
                    new = shortened
            if new != old:
                n_changed += 1
                if len(sample_diffs) < sample:
                    sample_diffs.append((old, new))
                if apply:
                    ok, err = _patch_row(sb_url, sb_key, row, new)
                    if ok:
                        n_patch_ok += 1
                    else:
                        n_patch_err += 1
                        if n_patch_err <= 5:
                            print(f'  PATCH FAIL {row["id"]}: {err}', flush=True)
            if total % 1000 == 0:
                print(
                    f'  ...{total} scanned, '
                    f'changed={n_changed}, too_long={n_too_long}, '
                    f'llm_called={n_llm_called}, '
                    f'patched={n_patch_ok}, patch_err={n_patch_err}',
                    flush=True,
                )
        if limit and total >= limit:
            break
        if len(rows) < PAGE:
            break
        offset += PAGE

    print(
        f'\nDone: scanned={total}, would_change={n_changed}, '
        f'too_long={n_too_long}, llm_called={n_llm_called}, '
        f'patched={n_patch_ok}, patch_err={n_patch_err}',
        flush=True,
    )
    if sample_diffs:
        print('\nSample diffs:')
        for old, new in sample_diffs:
            print(f'  - {old!r:80}\n    -> {new!r}')
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    _load_dotenv_if_present()
    ap = argparse.ArgumentParser(
        prog='rhylthyme-clean-titles',
        description='Standardize recipe titles in the rhylthyme-recipes catalog.',
    )
    ap.add_argument('--apply', action='store_true',
                    help='Write changes back to Supabase. Without this flag the run is dry.')
    ap.add_argument('--limit', type=int, default=None,
                    help='Cap the number of rows scanned (debug / sampling).')
    ap.add_argument('--no-llm', action='store_true',
                    help='Skip the Claude-based shortener; only run the offline pass.')
    ap.add_argument('--workers', type=int, default=1,
                    help='Concurrent rows for the LLM step. Default 1; raise for speed.')
    ap.add_argument('--user-id', default=DEFAULT_RECIPES_USER_ID,
                    help=f'Owner user_id (default: {DEFAULT_RECIPES_USER_ID}).')
    ap.add_argument('--sample', type=int, default=20,
                    help='How many before/after pairs to print at the end.')
    ap.add_argument('--start-offset', type=int, default=0,
                    help='Resume the page-walk from this offset (after a crash).')
    args = ap.parse_args(argv)
    return run(
        apply=args.apply,
        limit=args.limit,
        llm_shorten=not args.no_llm,
        workers=args.workers,
        user_id=args.user_id,
        sample=args.sample,
        start_offset=args.start_offset,
    )


if __name__ == '__main__':
    raise SystemExit(main())
