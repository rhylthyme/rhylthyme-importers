"""
Recipe language detection.

Run on recipe ``name`` + ``description`` to assign an ISO 639-1 language
tag. Used inline by import paths (so newly-saved recipes carry a
``language`` column from the start) and by a one-shot backfill for the
existing corpus.

The deep module here is :func:`detect_language` — pure function, no I/O,
deterministic seed. Callers don't need to know we use ``langdetect``;
swapping in lingua-py or a different library would be a one-file change.
"""

from __future__ import annotations

from typing import Optional

try:
    from langdetect import DetectorFactory, detect, LangDetectException
    DetectorFactory.seed = 0  # deterministic across runs
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False


# ISO 639-1 codes we explicitly support across the rest of the app
# (matches the i18n locale set: en, es, fr, de, ja, zh + the long tail of
# recipe-source languages we expect to see in the corpus).
SUPPORTED_LANGUAGES: tuple[str, ...] = (
    'en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'sv', 'da', 'no', 'fi',
    'pl', 'cs', 'hu', 'ro', 'el', 'tr', 'ru', 'uk', 'bg', 'ja', 'ko',
    'zh-cn', 'zh-tw', 'th', 'vi', 'id', 'ms', 'hi', 'ar', 'he', 'fa',
)

# Minimum characters of content before we trust a language detection.
# Below this, recipes get ``language=None`` rather than a guess — we'd
# rather show no flag than the wrong one.
_MIN_CONTENT_CHARS = 30


def _normalize(code: str) -> str:
    """Map langdetect's output to the codes we emit.

    Most langdetect codes already match ISO 639-1; the exceptions are
    Chinese variants (we keep zh-cn / zh-tw) and a few legacy aliases.
    """
    if not code:
        return ''
    code = code.lower().strip()
    if code == 'zh':
        return 'zh-cn'
    return code


def detect_language(
    name: Optional[str],
    description: Optional[str] = None,
    extra: Optional[str] = None,
) -> Optional[str]:
    """Detect the language of a recipe.

    Args:
        name: recipe name
        description: optional description (gives the detector more signal)
        extra: any additional text to include (e.g. step text)

    Returns:
        An ISO 639-1 language code, or ``None`` if detection failed or
        the input was too short to be reliable.
    """
    if not _AVAILABLE:
        return None
    parts = [s for s in (name, description, extra) if s and s.strip()]
    if not parts:
        return None
    text = ' '.join(parts).strip()
    if len(text) < _MIN_CONTENT_CHARS:
        return None
    try:
        code = detect(text)
    except LangDetectException:
        return None
    return _normalize(code) or None
