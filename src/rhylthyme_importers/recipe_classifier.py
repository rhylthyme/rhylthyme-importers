"""
Recipe taxonomy classifier — pure function around Claude Haiku.

Public surface is the single function ``classify(name, description,
ingredients, total_time_minutes=None) -> TaxonomyResult``.

The classifier asks Claude Haiku to map a recipe to the controlled
vocabulary in :mod:`taxonomy_vocab`. The prompt enumerates each facet's
valid slugs and instructs the model to:

  - pick exactly one slug for the single-valued facets
    (``category``, ``cuisine``, ``main_ingredient``);
  - return a (possibly empty) array for the multi-valued facets
    (``diets``, ``methods``, ``tags``);
  - return ``null``/empty when uncertain — never invent a value.

Output is parsed back into a TaxonomyResult dataclass. Anything outside
the vocab is silently dropped (defense-in-depth against the LLM
hallucinating a slug). Network / parse failures yield an empty result —
the caller is expected to either retry or leave the row's taxonomy
columns NULL.

Time bucket is derived deterministically from ``total_time_minutes`` if
the caller supplies it; otherwise we let the LLM pick. Deterministic
mapping is preferred (no LLM cost, no inconsistency).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import taxonomy_vocab as vocab


@dataclass
class TaxonomyResult:
    """Mirrors the column set added by the schema migration. Each field
    is independently optional — the classifier may confidently fill some
    and leave others ``None``."""
    category: str | None = None
    cuisine: str | None = None
    time_bucket: str | None = None
    main_ingredient: str | None = None
    diets: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # Diagnostic — surfaced for tests and CLI logging, never persisted.
    error: str | None = None

    def is_empty(self) -> bool:
        return (
            self.category is None
            and self.cuisine is None
            and self.time_bucket is None
            and self.main_ingredient is None
            and not self.diets
            and not self.methods
            and not self.tags
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'category': self.category,
            'cuisine': self.cuisine,
            'time_bucket': self.time_bucket,
            'main_ingredient': self.main_ingredient,
            'diets': sorted(set(self.diets)),
            'methods': sorted(set(self.methods)),
            'tags': sorted(set(self.tags)),
        }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You classify recipes into a fixed controlled vocabulary. Output STRICT "
    "JSON matching the schema described in the user message. Choose values "
    "ONLY from the lists provided. If you are not confident about a single-"
    "valued field, return null for it. If you are not confident about a "
    "multi-valued field, return an empty array. Never invent slugs."
)


def _build_user_prompt(
    name: str,
    description: str,
    ingredients: list[str],
    *,
    skip_time_bucket: bool,
) -> str:
    ing_block = '\n'.join(f'- {i}' for i in ingredients[:25]) or '(none provided)'
    facets = [
        ('category', 'pick exactly one', vocab.CATEGORIES),
        ('cuisine', 'pick exactly one', vocab.CUISINES),
        ('main_ingredient', 'pick exactly one (the most central ingredient)', vocab.MAIN_INGREDIENTS),
        ('diets', 'array of AT MOST 4 entries. Only include diets the recipe was clearly designed for. Do NOT pile on flags that happen to be technically true (e.g. a cocktail being incidentally vegan/keto/gluten_free). Empty array is the right answer when no diet was the explicit point of the dish.', vocab.DIETS),
        ('methods', 'array of AT MOST 3 entries — only the cooking methods / equipment actually used.', vocab.METHODS),
        ('tags', 'array of AT MOST 6 entries. Only emit a tag when there is a clear textual or contextual signal. Prefer fewer, high-signal tags over piling on adjectives.', vocab.TAGS),
    ]
    if not skip_time_bucket:
        facets.insert(2, ('time_bucket', 'pick exactly one', vocab.TIME_BUCKETS))

    facet_blocks = []
    for slug, instr, choices in facets:
        bullet = '\n'.join(f'    - {c}' for c in choices)
        facet_blocks.append(
            f'  "{slug}": {instr}.\n  Allowed values:\n{bullet}'
        )
    facets_md = '\n\n'.join(facet_blocks)

    return (
        "Classify this recipe into the controlled vocabulary below.\n\n"
        f"=== RECIPE ===\n"
        f"Name: {name}\n"
        f"Description: {description or '(none)'}\n"
        f"Ingredients:\n{ing_block}\n\n"
        f"=== VOCABULARY ===\n{facets_md}\n\n"
        "=== OUTPUT FORMAT ===\n"
        "Return JSON only, no prose, with this shape (use null or [] for "
        "fields you can't confidently fill):\n"
        "{\n"
        '  "category": "<slug or null>",\n'
        '  "cuisine": "<slug or null>",\n'
        + ('' if skip_time_bucket else '  "time_bucket": "<slug or null>",\n')
        + '  "main_ingredient": "<slug or null>",\n'
        '  "diets": ["<slug>", ...],\n'
        '  "methods": ["<slug>", ...],\n'
        '  "tags": ["<slug>", ...]\n'
        "}"
    )


# ---------------------------------------------------------------------------
# Response parsing — defensive against the LLM bending the rules
# ---------------------------------------------------------------------------

_JSON_RE = re.compile(r'\{[\s\S]*\}')


def _parse_response(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from the model's response. Returns
    ``None`` if no parseable JSON is present."""
    if not raw:
        return None
    raw = raw.strip()
    # Some models like to wrap in ```json fences — strip them.
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1] if '\n' in raw else raw
        raw = raw.rstrip('`').rstrip()
        if raw.endswith('```'):
            raw = raw[:-3].rstrip()
    m = _JSON_RE.search(raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


def _coerce_single(value: Any, allowed: frozenset[str]) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in allowed else None


def _coerce_list(value: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        v = item.strip().lower()
        if v in allowed and v not in seen:
            out.append(v)
            seen.add(v)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

DEFAULT_MODEL = 'claude-haiku-4-5'
# Allow runtime override so we can swap to flash-lite (cheaper) or pro
# (better quality) without code changes.
GEMINI_DEFAULT_MODEL = os.environ.get('RHYLTHYME_GEMINI_MODEL') or 'gemini-2.5-flash'


# ---------------------------------------------------------------------------
# Gemini provider — equivalent shape, ~10× cheaper than Haiku.
# ---------------------------------------------------------------------------

def _gemini_classify(
    *,
    name: str,
    description: str,
    ingredients: list[str],
    skip_time: bool,
    user_prompt: str,
    model: str = GEMINI_DEFAULT_MODEL,
) -> dict[str, Any] | None:
    """Call Gemini 2.5 Flash with a JSON-schema-constrained response.
    Returns the parsed dict or None on failure. The schema mirrors the
    Anthropic tool-use schema so post-processing is shared."""
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except ImportError:
        return None

    api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        return None

    # Gemini's responseSchema doesn't accept Python None in enums (the OpenAPI
    # schema flavor it uses requires nullable instead). Emit single-valued
    # facets as nullable strings; the post-processor's _coerce_single
    # already drops anything outside the vocab.
    properties: dict[str, Any] = {
        'category': {'type': 'STRING', 'enum': list(vocab.CATEGORIES), 'nullable': True},
        'cuisine': {'type': 'STRING', 'enum': list(vocab.CUISINES), 'nullable': True},
        'main_ingredient': {'type': 'STRING', 'enum': list(vocab.MAIN_INGREDIENTS), 'nullable': True},
        'diets': {'type': 'ARRAY', 'items': {'type': 'STRING', 'enum': list(vocab.DIETS)}, 'maxItems': 4},
        'methods': {'type': 'ARRAY', 'items': {'type': 'STRING', 'enum': list(vocab.METHODS)}, 'maxItems': 3},
        'tags': {'type': 'ARRAY', 'items': {'type': 'STRING', 'enum': list(vocab.TAGS)}, 'maxItems': 6},
    }
    required = ['category', 'cuisine', 'main_ingredient', 'diets', 'methods', 'tags']
    if not skip_time:
        properties['time_bucket'] = {
            'type': 'STRING', 'enum': list(vocab.TIME_BUCKETS), 'nullable': True,
        }
        required.append('time_bucket')

    schema = {
        'type': 'OBJECT',
        'properties': properties,
        'required': required,
    }
    client = genai.Client(api_key=api_key)
    # Gemini 2.5 Flash uses "thinking" tokens by default which consume
    # max_output_tokens before the actual JSON response is emitted —
    # truncates ~50% of recipe classifications mid-object. Setting
    # thinking_budget=0 disables it for our tightly-structured task.
    config_kwargs: dict[str, Any] = dict(
        response_mime_type='application/json',
        response_schema=schema,
        max_output_tokens=1024,
        temperature=0.1,
    )
    try:
        thinking_cfg = genai_types.ThinkingConfig(thinking_budget=0)
        config_kwargs['thinking_config'] = thinking_cfg
    except Exception:
        # Older google-genai versions don't expose ThinkingConfig — let the
        # request go through with the bumped output cap as a fallback.
        pass

    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                genai_types.Content(role='user', parts=[genai_types.Part(text=_SYSTEM_PROMPT + '\n\n' + user_prompt)]),
            ],
            config=genai_types.GenerateContentConfig(**config_kwargs),
        )
    except Exception as e:
        return {'__error__': f'gemini-error: {e}'[:200]}
    text = (resp.text or '').strip()
    return _parse_response(text)


_TOOL_NAME = 'classify_recipe'


def _build_classifier_tool(skip_time: bool) -> dict[str, Any]:
    """JSON schema for the structured-output tool.

    The Anthropic SDK enforces this schema when the model is told it
    MUST use this tool: each enum constrains the model to a specific
    vocab list, and ``maxItems`` guards against runaway tag dumps that
    blow the response token budget.
    """
    properties: dict[str, Any] = {
        'category': {
            'type': ['string', 'null'],
            'enum': [*vocab.CATEGORIES, None],
            'description': 'Top-level meal category. Use null only if truly ambiguous.',
        },
        'cuisine': {
            'type': ['string', 'null'],
            'enum': [*vocab.CUISINES, None],
            'description': 'Regional cuisine. Use null if the recipe is not strongly associated with one.',
        },
        'main_ingredient': {
            'type': ['string', 'null'],
            'enum': [*vocab.MAIN_INGREDIENTS, None],
            'description': 'The single most central ingredient.',
        },
        'diets': {
            'type': 'array',
            'items': {'type': 'string', 'enum': list(vocab.DIETS)},
            'maxItems': 4,
            'uniqueItems': True,
            'description': (
                'Diets the recipe was clearly designed for. Do NOT pile on '
                'flags just because they are technically true (a cocktail is '
                'not a vegan recipe in any useful sense). Empty is fine.'
            ),
        },
        'methods': {
            'type': 'array',
            'items': {'type': 'string', 'enum': list(vocab.METHODS)},
            'maxItems': 3,
            'uniqueItems': True,
            'description': 'Cooking methods / equipment actually used.',
        },
        'tags': {
            'type': 'array',
            'items': {'type': 'string', 'enum': list(vocab.TAGS)},
            'maxItems': 6,
            'uniqueItems': True,
            'description': 'High-signal editorial labels. Prefer fewer over more.',
        },
    }
    required = ['category', 'cuisine', 'main_ingredient', 'diets', 'methods', 'tags']
    if not skip_time:
        properties['time_bucket'] = {
            'type': ['string', 'null'],
            'enum': [*vocab.TIME_BUCKETS, None],
            'description': 'Total-time bucket; null if uncertain.',
        }
        required.append('time_bucket')
    return {
        'name': _TOOL_NAME,
        'description': 'Classify a recipe into the rhylthyme controlled vocabulary.',
        'input_schema': {
            'type': 'object',
            'properties': properties,
            'required': required,
            'additionalProperties': False,
        },
    }


def classify(
    name: str,
    description: str = '',
    ingredients: list[str] | None = None,
    total_time_minutes: int | None = None,
    *,
    provider: str = 'anthropic',
    model: str | None = None,
    client: Any | None = None,
) -> TaxonomyResult:
    """Map a recipe to the controlled vocabulary.

    Two providers supported:
      - ``anthropic`` (default): Claude Haiku via tool-use. Highest
        quality, ~$1.55/1000 rows.
      - ``gemini``: Gemini 2.5 Flash via response_schema. ~10× cheaper.

    Args:
      provider: 'anthropic' or 'gemini'.
      model: override the model id (defaults to provider-specific default).
      client: optional anthropic.Anthropic instance for tests / DI.
        Ignored when provider != 'anthropic'.

    Returns: TaxonomyResult. On any failure returns an empty result with
    .error set.
    """
    ings = list(ingredients or [])
    skip_time = total_time_minutes is not None
    time_bucket: str | None = None
    if skip_time:
        time_bucket = vocab.time_bucket_for_minutes(total_time_minutes)

    user_prompt = _build_user_prompt(
        name=name, description=description, ingredients=ings,
        skip_time_bucket=skip_time,
    )

    parsed: dict[str, Any] | None = None
    if provider == 'gemini':
        # Distinguish "SDK / key missing" (return value never set, so a
        # follow-up sentinel is needed) from "API call returned but parse
        # failed" (the function returns None for parse failures too).
        try:
            from google import genai  # type: ignore  # noqa: F401
        except ImportError as e:
            return TaxonomyResult(error=f'gemini SDK missing: {e}')
        if not (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')):
            return TaxonomyResult(error='GEMINI_API_KEY not set')
        result = _gemini_classify(
            name=name, description=description, ingredients=ings,
            skip_time=skip_time, user_prompt=user_prompt,
            model=model or GEMINI_DEFAULT_MODEL,
        )
        if result is None:
            return TaxonomyResult(error='gemini: empty/unparseable response')
        if '__error__' in result:
            return TaxonomyResult(error=result['__error__'])
        parsed = result
    elif provider == 'anthropic':
        if client is None:
            if not os.environ.get('ANTHROPIC_API_KEY'):
                return TaxonomyResult(error='ANTHROPIC_API_KEY not set')
            try:
                import anthropic  # type: ignore
            except ImportError as e:
                return TaxonomyResult(error=f'anthropic SDK missing: {e}')
            client = anthropic.Anthropic()
        tool = _build_classifier_tool(skip_time)
        try:
            resp = client.messages.create(
                model=model or DEFAULT_MODEL,
                max_tokens=800,
                system=_SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': user_prompt}],
                tools=[tool],
                tool_choice={'type': 'tool', 'name': _TOOL_NAME},
            )
        except Exception as e:
            return TaxonomyResult(error=f'api-error: {e}'[:200])

        # Find the tool_use block.
        for block in (resp.content or []):
            if getattr(block, 'type', None) == 'tool_use' and getattr(block, 'name', None) == _TOOL_NAME:
                parsed = getattr(block, 'input', None)
                break
        if parsed is None:
            # Server-side rollback fallback: parse text.
            raw = ''
            for block in (resp.content or []):
                if getattr(block, 'type', None) == 'text':
                    raw += block.text or ''
            parsed = _parse_response(raw)
    else:
        return TaxonomyResult(error=f'unknown provider: {provider!r}')

    if parsed is None:
        return TaxonomyResult(error='no tool_use in response')

    diets = _coerce_list(parsed.get('diets'), vocab.DIETS_SET)
    # Apply implication graph: vegan → also tag vegetarian + dairy_free +
    # egg_free, etc. Real-world filter expectation, not LLM guesswork.
    diets = vocab.expand_diets(diets)

    out = TaxonomyResult(
        category=_coerce_single(parsed.get('category'), vocab.CATEGORIES_SET),
        cuisine=_coerce_single(parsed.get('cuisine'), vocab.CUISINES_SET),
        time_bucket=time_bucket if skip_time
            else _coerce_single(parsed.get('time_bucket'), vocab.TIME_BUCKETS_SET),
        main_ingredient=_coerce_single(parsed.get('main_ingredient'), vocab.MAIN_INGREDIENTS_SET),
        diets=diets,
        methods=_coerce_list(parsed.get('methods'), vocab.METHODS_SET),
        tags=_coerce_list(parsed.get('tags'), vocab.TAGS_SET),
    )
    return out
