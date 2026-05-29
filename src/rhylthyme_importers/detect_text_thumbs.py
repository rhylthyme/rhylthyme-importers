"""
Find recipe thumbnails that contain overlay text (recipe titles, brand
watermarks, "Pin it" cards) and rewrite them with a clean FLUX-generated
image instead.

Two phases:

1. **Scan**: page through programs with a thumbnail, call Claude Haiku
   vision on each image with a YES/NO + 1-line reason prompt. Records
   results (and any image-fetch errors) to a JSON file. Resumable via
   ``--start-offset`` and a checkpoint that the scanner writes after each
   page.

2. **Regenerate**: read the scan output, take rows flagged YES, and call
   the existing ``generate_thumbnails._generate_one`` machinery to replace
   each one's thumbnail with a fresh FAL image. Idempotent — already-clean
   rows (those generated previously) are not touched.

Cost ballpark:
- Haiku vision @ ~$0.0009/image × 40K rows ≈ $36 for a full scan.
- FLUX schnell regenerate @ $0.003/image × N flagged ≈ small.

Use ``--sample 200`` first to size the problem before paying for the
whole corpus.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .cooklang_federation import (
    DEFAULT_RECIPES_USER_ID,
    _load_dotenv_if_present,
)


# ---------------------------------------------------------------------------
# Vision check (Claude Haiku)
# ---------------------------------------------------------------------------

_VISION_PROMPT = (
    'Does this image contain readable overlay text (recipe title, "pin me", '
    'brand card, watermark with words, etc.) beyond a tiny logo? Reply with '
    'exactly one word YES or NO on the first line, then a short description '
    'of any text you see on a second line.'
)


def _vision_check(client, url: str) -> tuple[bool, str, str | None]:
    """Return (has_text, description, error). If error is set, the call
    failed; has_text is False in that case."""
    try:
        resp = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=120,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'url', 'url': url}},
                    {'type': 'text', 'text': _VISION_PROMPT},
                ],
            }],
        )
    except Exception as e:
        return False, '', f'vision-error: {e}'[:200]
    text = (resp.content[0].text or '').strip()
    first = text.splitlines()[0].strip().upper() if text else ''
    has_text = first.startswith('YES')
    desc = text.splitlines()[1].strip() if '\n' in text else ''
    return has_text, desc, None


# ---------------------------------------------------------------------------
# macOS Vision OCR (free, Mac-only). VNRecognizeTextRequest is far better
# than Tesseract on stylized / small / colored overlay text.
# ---------------------------------------------------------------------------

# Confidence floor — Vision's "accurate" recognition level returns
# confidence 0.5 for any recognized word. Lower bar exists but is rarely
# meaningful, so we just count any returned word that survives the letter
# filter as a hit.
MAC_MIN_LETTERS = 4


def _macos_vision_check(url: str, ua: str = 'rhylthyme-importers/1.0') -> tuple[bool, str, str | None]:
    """Local OCR via Apple's VNRecognizeTextRequest. Returns
    (has_text, description, error). Importable only on macOS.
    """
    try:
        from Foundation import NSData  # type: ignore
        from Vision import VNImageRequestHandler, VNRecognizeTextRequest  # type: ignore
    except ImportError as e:
        return False, '', f'missing-dep (mac-only): {e}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': ua})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except Exception as e:
        return False, '', f'fetch-error: {e}'[:200]
    try:
        nsdata = NSData.dataWithBytes_length_(data, len(data))
        request = VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(1)  # 1 = accurate, 0 = fast
        request.setUsesLanguageCorrection_(False)
        handler = VNImageRequestHandler.alloc().initWithData_options_(nsdata, None)
        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            return False, '', f'ocr-error: {err}'[:200]
    except Exception as e:
        return False, '', f'ocr-error: {e}'[:200]
    words: list[str] = []
    for obs in (request.results() or []):
        cands = obs.topCandidates_(1)
        if not cands or len(cands) == 0:
            continue
        text = (cands[0].string() or '').strip()
        if not text:
            continue
        # Letters-only filter rejects mostly-punctuation / decorative noise
        # like ">*-" or "II" that Vision sometimes emits over textured food.
        letters = ''.join(c for c in text if c.isalpha()).lower()
        if len(letters) < MAC_MIN_LETTERS:
            continue
        # Reject consonant-cluster noise like "JfJl" / "rYtr" — these come
        # from Vision hallucinating text on crumb / herb / batter textures.
        # Real watermarks ("budgetbytes", "carlsbadcravings", "morning")
        # always have vowels in roughly the standard English ratio.
        vowel_count = sum(1 for c in letters if c in 'aeiouy')
        if vowel_count == 0:
            continue
        # Vowel-to-letter ratio: english is ~38%; cap at >=15% to retain
        # cases like "BBQ" while rejecting random capitalised junk.
        if vowel_count / len(letters) < 0.15:
            continue
        words.append(text)
    has = len(words) >= 1
    desc = ' | '.join(words[:6])[:200] if words else ''
    return has, desc, None


# ---------------------------------------------------------------------------
# Local OCR (free) — Tesseract via pytesseract.
# ---------------------------------------------------------------------------
#
# Calibration tunables:
# - MIN_LETTERS: shorter strings than this are noise (random punctuation,
#   half-glimpses of dish edges that look like letters).
# - MIN_CONF: Tesseract reports per-word confidence 0-100; below this we
#   ignore the word. Watermarks on real photos tend to score high; noise
#   patterns on textured food (e.g. crispy bread) score low.
# - MIN_LONG_WORDS: require at least one word of >=this many letters that
#   passed conf+letter filters. Single-letter / 2-letter detections are
#   almost always false positives over crumb / herb backgrounds.
MIN_LETTERS = 3
MIN_CONF = 55
# A single 3-letter blip on a textured surface is too weak; require either
# one robust word (>=5 letters at high conf) OR several lower-bar words
# (e.g. "the", "by", "com" patterns common in watermarks).
MIN_LONG_WORDS = 2
MIN_HIGH_CONF_WORD_LETTERS = 5
MIN_HIGH_CONF = 75


def _tesseract_check(url: str, ua: str = 'rhylthyme-importers/1.0') -> tuple[bool, str, str | None]:
    """Local OCR. Returns (has_text, description, error).

    The description aggregates the high-confidence words Tesseract found so
    a human can spot-check the report later.
    """
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
        import io
    except ImportError as e:
        return False, '', f'missing-dep: {e}'
    # Fetch image bytes
    try:
        req = urllib.request.Request(url, headers={'User-Agent': ua})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except Exception as e:
        return False, '', f'fetch-error: {e}'[:200]
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode not in ('L', 'RGB'):
            img = img.convert('RGB')
        # Tesseract really wants ~30+px tall glyphs. Recipe-site hero images
        # are usually 480-720 px on the SHORT edge with title-overlay text
        # around 50px tall — right at the recognition edge. The relevant
        # axis is the short edge (a 480x1200 Pinterest pin still has narrow
        # text), so we scale based on that. Probed against real watermark /
        # title-card samples; 2x consistently surfaces text that the native
        # pass misses entirely.
        short_edge = min(img.size)
        if short_edge < 700:
            scale = 2
        else:
            scale = 1
        if scale > 1:
            img = img.resize(
                (img.size[0] * scale, img.size[1] * scale),
                Image.LANCZOS,
            )
        info = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as e:
        return False, '', f'ocr-error: {e}'[:200]
    words = []
    confident_words = 0  # passed the standard bar
    big_word_hit = False  # ≥5 letters at conf ≥75 — alone, that's a flag
    for i, w in enumerate(info.get('text') or []):
        w = (w or '').strip()
        if not w:
            continue
        try:
            conf = float(info['conf'][i])
        except (KeyError, IndexError, TypeError, ValueError):
            conf = -1
        # Letters only (drops punctuation / digits-only fragments)
        letters = ''.join(ch for ch in w if ch.isalpha())
        if conf >= MIN_HIGH_CONF and len(letters) >= MIN_HIGH_CONF_WORD_LETTERS:
            big_word_hit = True
            words.append(w)
            continue
        if conf < MIN_CONF:
            continue
        if len(letters) < MIN_LETTERS:
            continue
        words.append(w)
        confident_words += 1
    has = big_word_hit or confident_words >= MIN_LONG_WORDS
    desc = ' '.join(words[:8])[:200] if words else ''
    return has, desc, None


# ---------------------------------------------------------------------------
# Supabase iteration
# ---------------------------------------------------------------------------

def _supa_get(sb_url: str, sb_key: str, qs: str, retries: int = 5) -> list:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                f'{sb_url}/rest/v1/{qs}',
                headers={'apikey': sb_key, 'Authorization': f'Bearer {sb_key}'},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(min(30, 2 ** i))
    raise last  # type: ignore[misc]


def _iter_programs(sb_url: str, sb_key: str, user_id: str,
                   start_offset: int, page_size: int = 300):
    """Yield (id, name, thumbnail) for every public program owned by user_id
    that has a non-empty thumbnail. Skips empty-thumbnail rows so we don't
    spend vision calls on them."""
    offset = start_offset
    while True:
        rows = _supa_get(sb_url, sb_key, (
            f'programs?user_id=eq.{user_id}&is_public=eq.true'
            f'&select=id,name,thumb:program_json->metadata->>thumbnail'
            f'&order=id&limit={page_size}&offset={offset}'
        ))
        if not rows:
            return
        for row in rows:
            t = row.get('thumb')
            if isinstance(t, str) and t.strip():
                yield row['id'], row.get('name') or '', t.strip(), offset
            offset += 1
        if len(rows) < page_size:
            return


# ---------------------------------------------------------------------------
# Scan command
# ---------------------------------------------------------------------------

def _ckpt_path(report_path: Path) -> Path:
    return report_path.with_suffix(report_path.suffix + '.ckpt')


def cmd_scan(*, report_path: Path, user_id: str, sample: int | None,
             start_offset: int, workers: int, skip_fal: bool,
             engine: str) -> int:
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')
    client = None
    if engine == 'vision':
        if not os.environ.get('ANTHROPIC_API_KEY'):
            sys.exit('ANTHROPIC_API_KEY required for engine=vision')
        import anthropic
        client = anthropic.Anthropic()
    elif engine == 'tesseract':
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as e:
            sys.exit(f'engine=tesseract requires pytesseract + Pillow ({e})')
    elif engine == 'macos':
        try:
            from Vision import VNRecognizeTextRequest  # noqa: F401
        except ImportError as e:
            sys.exit(f'engine=macos requires pyobjc-framework-Vision (mac-only): {e}')
    else:
        sys.exit(f'unknown engine: {engine!r}')

    # Resume support — load existing report and pick up where we left off.
    flagged: list[dict] = []
    clean_count = 0
    error_count = 0
    seen_ids: set[str] = set()
    if report_path.exists():
        try:
            existing = json.loads(report_path.read_text())
            flagged = existing.get('flagged') or []
            clean_count = existing.get('clean_count', 0)
            error_count = existing.get('error_count', 0)
            seen_ids = set(existing.get('seen_ids') or [])
            print(f'Resuming from {report_path}: '
                  f'{len(flagged)} flagged, {clean_count} clean, '
                  f'{error_count} errors, {len(seen_ids)} seen.', flush=True)
        except Exception as e:
            print(f'Could not read existing report ({e}); starting fresh.', flush=True)

    iterator = _iter_programs(sb_url, sb_key, user_id, start_offset)
    if sample:
        iterator = (t for i, t in enumerate(iterator) if i < sample * 4)  # 4x oversample, see below

    queue: list[tuple] = []
    for prog_id, name, thumb, page_off in iterator:
        if prog_id in seen_ids:
            continue
        if skip_fal and 'fal.media' in thumb:
            continue
        queue.append((prog_id, name, thumb))
        if sample and len(queue) >= sample:
            break

    print(f'Vision-checking {len(queue)} thumbnails (workers={workers}) ...',
          flush=True)

    def _check_one(args):
        pid, nm, url = args
        if engine == 'vision':
            has, desc, err = _vision_check(client, url)
        elif engine == 'macos':
            has, desc, err = _macos_vision_check(url)
        else:
            has, desc, err = _tesseract_check(url)
        return pid, nm, url, has, desc, err

    n_done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_check_one, item): item for item in queue}
        for fut in as_completed(futs):
            try:
                pid, nm, url, has, desc, err = fut.result()
            except Exception as e:
                error_count += 1
                continue
            seen_ids.add(pid)
            n_done += 1
            if err:
                error_count += 1
            elif has:
                flagged.append({
                    'id': pid, 'name': nm, 'thumbnail': url, 'description': desc,
                })
            else:
                clean_count += 1

            if n_done % 50 == 0 or n_done == len(queue):
                _write_report(report_path, flagged, clean_count, error_count, seen_ids)
                print(f'  ...{n_done}/{len(queue)} flagged={len(flagged)} '
                      f'clean={clean_count} err={error_count}', flush=True)

    _write_report(report_path, flagged, clean_count, error_count, seen_ids)
    pct = 100 * len(flagged) / max(1, n_done)
    print(f'\nDone scan: flagged={len(flagged)} ({pct:.1f}% of {n_done}), '
          f'clean={clean_count}, errors={error_count}', flush=True)
    if flagged[:5]:
        print('\nSample flagged:')
        for f in flagged[:5]:
            print(f'  - {f["name"][:50]}  ->  {f["thumbnail"][:80]}\n      "{f["description"][:120]}"')
    return 0


def _write_report(path, flagged, clean, errors, seen):
    path.write_text(json.dumps({
        'flagged': flagged,
        'clean_count': clean,
        'error_count': errors,
        'seen_ids': sorted(seen),
    }, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Regenerate command
# ---------------------------------------------------------------------------

def cmd_regenerate(*, report_path: Path, user_id: str, limit: int | None,
                   workers: int, dry_run: bool) -> int:
    if not os.environ.get('FAL_KEY'):
        sys.exit('FAL_KEY required for regeneration')
    sb_url = os.environ.get('SUPABASE_URL')
    sb_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not sb_url or not sb_key:
        sys.exit('SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required')
    data = json.loads(report_path.read_text())
    flagged = data.get('flagged') or []
    if limit:
        flagged = flagged[:limit]
    print(f'Will regenerate {len(flagged)} thumbnails (dry_run={dry_run})', flush=True)

    # Reuse the existing image-generation pipeline so we get the same prompt
    # template, fal.media-URL behaviour, and Supabase patch logic.
    from .generate_thumbnails import _generate_one, _patch_thumbnail, _supabase_headers
    H = _supabase_headers(sb_key)

    def _process_one(idx_item):
        i, item = idx_item
        pid = item['id']
        # Refetch row so we have the current program_json for prompt context
        # and so the patch overwrites the latest version.
        try:
            with urllib.request.urlopen(urllib.request.Request(
                f'{sb_url}/rest/v1/programs?id=eq.{pid}&select=id,name,program_json',
                headers=H,
            ), timeout=30) as r:
                rows = json.loads(r.read())
        except Exception as e:
            return i, pid, 'fetch_fail', str(e)[:160], None
        if not rows:
            return i, pid, 'missing', '', None
        row = rows[0]
        if isinstance(row.get('program_json'), str):
            row['program_json'] = json.loads(row['program_json'])
        res = _generate_one(row)
        if res.get('status') != 'ok':
            return i, pid, 'gen_fail', res.get('error', '') or '', row
        if dry_run:
            return i, pid, 'dry', res['url'], row
        ok, err = _patch_thumbnail(sb_url, sb_key, row, res['url'])
        if ok:
            return i, pid, 'ok', res['url'], row
        return i, pid, 'patch_fail', err, row

    n_ok = n_fail = 0
    n_done = 0
    queue = list(enumerate(flagged, 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_process_one, it) for it in queue]
        for fut in as_completed(futs):
            n_done += 1
            try:
                i, pid, status, info, row = fut.result()
            except Exception as e:
                n_fail += 1
                continue
            if status == 'ok' or status == 'dry':
                n_ok += 1
            else:
                n_fail += 1
                if n_fail <= 5:
                    print(f'  [{i}] {status} {pid}: {info}', flush=True)
            if n_done % 25 == 0 or n_done == len(queue):
                print(f'  ...{n_done}/{len(queue)} ok={n_ok} fail={n_fail}', flush=True)

    print(f'\nDone regenerate: ok={n_ok} fail={n_fail}', flush=True)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    _load_dotenv_if_present()
    ap = argparse.ArgumentParser(
        prog='rhylthyme-detect-text-thumbs',
        description='Find thumbnails with overlay text and rewrite them via FAL.',
    )
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('scan', help='Vision-check thumbnails for overlay text')
    s.add_argument('--report', type=Path, default=Path('/tmp/text_thumbs_report.json'))
    s.add_argument('--user-id', default=DEFAULT_RECIPES_USER_ID)
    s.add_argument('--sample', type=int, default=None,
                   help='Limit to N thumbnails (debug / sizing).')
    s.add_argument('--start-offset', type=int, default=0)
    s.add_argument('--workers', type=int, default=8)
    s.add_argument('--skip-fal', action='store_true', default=True,
                   help='Skip thumbnails already on fal.media (default true).')
    s.add_argument('--no-skip-fal', dest='skip_fal', action='store_false')
    s.add_argument('--engine', choices=['macos', 'tesseract', 'vision'], default='macos',
                   help='OCR engine: macos (Apple VNRecognizeTextRequest, free local; default), '
                        'tesseract (free local, weaker), vision (paid Claude Haiku via API).')

    r = sub.add_parser('regenerate', help='Re-render flagged thumbnails via FAL')
    r.add_argument('--report', type=Path, default=Path('/tmp/text_thumbs_report.json'))
    r.add_argument('--user-id', default=DEFAULT_RECIPES_USER_ID)
    r.add_argument('--limit', type=int, default=None)
    r.add_argument('--workers', type=int, default=8)
    r.add_argument('--dry-run', action='store_true')

    args = ap.parse_args(argv)
    if args.cmd == 'scan':
        return cmd_scan(
            report_path=args.report, user_id=args.user_id,
            sample=args.sample, start_offset=args.start_offset,
            workers=args.workers, skip_fal=args.skip_fal,
            engine=args.engine,
        )
    if args.cmd == 'regenerate':
        return cmd_regenerate(
            report_path=args.report, user_id=args.user_id,
            limit=args.limit, workers=args.workers, dry_run=args.dry_run,
        )
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
