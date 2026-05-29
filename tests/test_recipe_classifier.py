"""
Golden-fixture tests for ``recipe_classifier.classify``.

Each fixture is a (name, description, ingredients, expected) tuple.
``expected`` is a dict whose keys we want the classifier to land on; we
assert *containment* (expected ⊆ actual) rather than exact equality, so
the LLM is allowed to confidently emit *additional* tags beyond what we
specified — but it must never miss the central facets we hand-labeled.

Two parsing-layer tests run without the LLM (mocked); the rest hit the
real API and are skipped when ``ANTHROPIC_API_KEY`` is absent (so
contributors and CI without API access can still run the suite).

Each fixture is a lightweight integration test; running all of them on
every CI commit is overkill. Cost-conscious CI can use ``-k "not haiku"``
to skip the live calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from rhylthyme_importers import taxonomy_vocab as vocab
from rhylthyme_importers.recipe_classifier import (
    TaxonomyResult,
    _coerce_list,
    _coerce_single,
    _parse_response,
    classify,
)


# ===========================================================================
# Pure-function tests — no LLM, no network
# ===========================================================================


class TestParseResponse:
    def test_extracts_clean_json(self):
        out = _parse_response('{"category": "dinner", "cuisine": "italian"}')
        assert out == {'category': 'dinner', 'cuisine': 'italian'}

    def test_strips_markdown_fence(self):
        out = _parse_response('```json\n{"category": "dinner"}\n```')
        assert out == {'category': 'dinner'}

    def test_extracts_first_object_when_prose_surrounds(self):
        text = 'Here is the result:\n{"category": "snacks"}\nHope that helps!'
        assert _parse_response(text) == {'category': 'snacks'}

    def test_returns_none_on_garbage(self):
        assert _parse_response('') is None
        assert _parse_response('not json at all') is None
        assert _parse_response('{not closed') is None


class TestCoercion:
    def test_single_value_in_vocab_passes(self):
        assert _coerce_single('italian', vocab.CUISINES_SET) == 'italian'

    def test_single_value_outside_vocab_dropped(self):
        assert _coerce_single('martian', vocab.CUISINES_SET) is None

    def test_single_value_normalized_to_lowercase(self):
        assert _coerce_single('  Italian ', vocab.CUISINES_SET) == 'italian'

    def test_single_value_non_string_returns_none(self):
        assert _coerce_single(42, vocab.CUISINES_SET) is None
        assert _coerce_single(None, vocab.CUISINES_SET) is None

    def test_list_filters_to_vocab_only(self):
        assert _coerce_list(
            ['vegan', 'martian', 'gluten_free', 'made_up'],
            vocab.DIETS_SET,
        ) == ['vegan', 'gluten_free']

    def test_list_dedupes_preserving_first_seen(self):
        assert _coerce_list(
            ['vegan', 'vegan', 'gluten_free', 'vegan'],
            vocab.DIETS_SET,
        ) == ['vegan', 'gluten_free']

    def test_list_non_list_returns_empty(self):
        assert _coerce_list(None, vocab.DIETS_SET) == []
        assert _coerce_list('vegan', vocab.DIETS_SET) == []


# ===========================================================================
# Classifier tests with a mocked Anthropic client (no network)
# ===========================================================================


def _mock_tool_use(payload: dict):
    """Mock returns a tool_use content block carrying ``payload`` as input.
    Mirrors how Claude's structured-output path responds when forced to
    call a specific tool."""
    block = MagicMock()
    block.type = 'tool_use'
    block.name = 'classify_recipe'
    block.input = payload
    msg = MagicMock()
    msg.content = [block]
    client = MagicMock()
    client.messages.create.return_value = msg
    return client


def _mock_text(text: str):
    """Mock returns a plain text block — used to test the legacy fallback
    path when no tool_use block is present (server-side rollback)."""
    block = MagicMock()
    block.type = 'text'
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    client = MagicMock()
    client.messages.create.return_value = msg
    return client


# Back-compat alias so existing tests keep working
_mock_client = _mock_text


class TestClassifyMocked:
    def test_happy_path(self):
        client = _mock_tool_use({
            'category': 'dinner', 'cuisine': 'italian',
            'main_ingredient': 'pasta',
            'diets': ['vegetarian'], 'methods': ['stovetop'],
            'tags': ['weeknight', 'one_pot'],
        })
        r = classify(
            name='Spaghetti Aglio e Olio',
            ingredients=['1 lb spaghetti', '6 cloves garlic', '1/2 cup olive oil'],
            total_time_minutes=20,
            client=client,
        )
        assert r.category == 'dinner'
        assert r.cuisine == 'italian'
        assert r.main_ingredient == 'pasta'
        assert r.diets == ['vegetarian']
        assert r.methods == ['stovetop']
        assert 'weeknight' in r.tags
        # time_bucket comes from the deterministic helper, not the LLM
        assert r.time_bucket == 'under_30'
        assert r.error is None

    def test_drops_invented_slugs(self):
        # Even if the model somehow returns a non-vocab value (it shouldn't,
        # given the tool's enum constraints), the classifier silently
        # drops it rather than persist garbage.
        client = _mock_tool_use({
            'cuisine': 'atlantean', 'category': 'dinner',
            'main_ingredient': None, 'diets': [], 'methods': [], 'tags': [],
        })
        r = classify('test', total_time_minutes=20, client=client)
        assert r.cuisine is None
        assert r.category == 'dinner'

    def test_no_tool_use_block(self):
        # If the model rolls back to plain text (e.g. tool_choice was
        # ignored), classifier falls back to text-parsing and surfaces an
        # error if the text is unparseable too.
        client = _mock_text('I will not comply with this request.')
        r = classify('test', client=client)
        assert r.is_empty()
        assert r.error is not None
        assert 'no tool_use' in r.error or 'unparseable' in r.error or 'response' in r.error

    def test_text_fallback_with_valid_json(self):
        # If the only block is text but the text contains valid JSON, the
        # legacy fallback parses it.
        client = _mock_text('{"category": "snacks", "cuisine": "american", '
                            '"main_ingredient": "nuts", '
                            '"diets": [], "methods": [], "tags": []}')
        r = classify('test', total_time_minutes=10, client=client)
        assert r.category == 'snacks'
        assert r.cuisine == 'american'

    def test_api_error_surfaces(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError('429 rate limit')
        r = classify('test', client=client)
        assert r.is_empty()
        assert 'api-error' in (r.error or '')

    def test_time_bucket_deterministic_overrides_llm(self):
        # Even if the LLM somehow emits time_bucket in the tool input, the
        # deterministic helper wins when total_time_minutes is supplied.
        client = _mock_tool_use({
            'category': 'dinner', 'cuisine': 'american',
            'main_ingredient': 'beef', 'time_bucket': 'two_plus_hr',
            'diets': [], 'methods': [], 'tags': [],
        })
        r = classify('test', total_time_minutes=10, client=client)
        assert r.time_bucket == 'under_15'

    def test_time_bucket_from_tool_when_minutes_absent(self):
        client = _mock_tool_use({
            'category': 'dinner', 'cuisine': 'american',
            'main_ingredient': 'beef', 'time_bucket': 'under_30',
            'diets': [], 'methods': [], 'tags': [],
        })
        r = classify('test', total_time_minutes=None, client=client)
        assert r.time_bucket == 'under_30'


class TestTaxonomyResult:
    def test_to_dict_sorted_dedup(self):
        r = TaxonomyResult(
            category='dinner',
            diets=['vegetarian', 'vegan', 'vegetarian'],
            tags=['weeknight', 'budget', 'weeknight'],
        )
        d = r.to_dict()
        assert d['diets'] == ['vegan', 'vegetarian']
        assert d['tags'] == ['budget', 'weeknight']

    def test_is_empty(self):
        assert TaxonomyResult().is_empty()
        assert not TaxonomyResult(category='dinner').is_empty()
        assert not TaxonomyResult(tags=['weeknight']).is_empty()


# ===========================================================================
# Live LLM golden fixtures — skipped when no API key
# ===========================================================================


needs_api = pytest.mark.skipif(
    not os.environ.get('ANTHROPIC_API_KEY'),
    reason='ANTHROPIC_API_KEY required for live classifier fixtures',
)


@dataclass
class Fixture:
    name: str
    description: str
    ingredients: list[str]
    minutes: int | None
    expect: dict[str, Any]


# Hand-labeled examples. Each fixture asserts the *minimum* the classifier
# must hit: the central facets where there's a clear correct answer. The
# LLM is allowed to add more tags / methods than we specify.
FIXTURES: list[Fixture] = [
    # --- Cuisine coverage ------------------------------------------------
    Fixture(
        # Aglio e olio is traditionally finished with parmesan, so "vegan"
        # is genuinely debatable depending on whether the model assumes
        # the cheese. We only assert vegetarian here.
        name='Spaghetti Aglio e Olio',
        description='Pasta tossed with golden garlic, chili flakes, and good olive oil.',
        ingredients=['1 lb spaghetti', '6 cloves garlic', '1/2 cup olive oil', 'red pepper flakes', 'parsley'],
        minutes=20,
        expect={
            'category': 'dinner', 'cuisine': 'italian',
            'main_ingredient': 'pasta', 'time_bucket': 'under_30',
            'diets_superset': {'vegetarian'},
        },
    ),
    Fixture(
        name='Carne Asada Tacos',
        description='Marinated grilled flank steak in warm corn tortillas.',
        ingredients=['1 lb flank steak', 'lime juice', 'cilantro', '2 garlic cloves', 'cumin', '8 corn tortillas'],
        minutes=45,
        expect={'cuisine': 'mexican', 'main_ingredient': 'beef', 'time_bucket': 'under_60'},
    ),
    Fixture(
        name='Chicken Tikka Masala',
        description='Yogurt-marinated chicken simmered in creamy tomato curry.',
        ingredients=['1.5 lb chicken thighs', '1 cup yogurt', '2 tbsp garam masala', '1 onion', 'tomato sauce', 'heavy cream'],
        minutes=40,
        expect={'cuisine': 'indian', 'main_ingredient': 'chicken'},
    ),
    Fixture(
        name='Pad Thai',
        description='Stir-fried rice noodles with shrimp, peanuts, lime, and tamarind.',
        ingredients=['8 oz rice noodles', '1/2 lb shrimp', 'tamarind paste', 'fish sauce', 'peanuts', '2 eggs', 'bean sprouts'],
        minutes=25,
        expect={'cuisine': 'thai', 'main_ingredient': 'shrimp'},
    ),
    Fixture(
        name='Boeuf Bourguignon',
        description='French braise of beef in red wine with mushrooms and pearl onions.',
        ingredients=['3 lb beef chuck', '1 bottle red wine', '4 oz bacon', '8 oz mushrooms', 'pearl onions', 'thyme'],
        minutes=240,
        expect={'cuisine': 'french', 'main_ingredient': 'beef', 'time_bucket': 'two_plus_hr'},
    ),
    Fixture(
        # gluten_free isn't an *intent* of Greek salad — it just happens
        # to contain no grains. The prompt instructs the model not to
        # pile on incidental diet flags, so we don't assert it here.
        name='Greek Salad',
        description='Tomato, cucumber, red onion, olives, feta with olive oil and oregano.',
        ingredients=['4 tomatoes', '1 cucumber', '1 red onion', '1/2 cup kalamata olives', '4 oz feta', 'olive oil', 'oregano'],
        minutes=10,
        expect={
            'cuisine': 'greek', 'time_bucket': 'under_15',
            'diets_superset': {'vegetarian'},
            'methods_superset': {'no_cook'},
        },
    ),
    # --- Diet edge cases -------------------------------------------------
    Fixture(
        name='Vegan Lentil Bolognese',
        description='Hearty plant-based pasta sauce with red lentils and walnuts.',
        ingredients=['1 cup red lentils', '1/2 cup walnuts', '1 onion', '4 cloves garlic', '28 oz canned tomatoes', '1 lb pasta', 'olive oil'],
        minutes=45,
        expect={
            'main_ingredient': 'lentils',
            'diets_superset': {'vegan', 'vegetarian'},
        },
    ),
    Fixture(
        # We assert the diets the recipe was DESIGNED for (keto, low_carb)
        # and the implied vegetarian (no animal flesh). gluten_free is
        # incidental, not an intent — model is allowed to skip it.
        name='Keto Cauliflower Mac & Cheese',
        description='Low-carb riff on the classic — riced cauliflower in cheddar sauce.',
        ingredients=['1 head cauliflower', '2 cups shredded cheddar', '1/2 cup heavy cream', '4 oz cream cheese', 'butter'],
        minutes=30,
        expect={
            'main_ingredient': 'cauliflower',
            'diets_superset': {'keto', 'low_carb', 'vegetarian'},
        },
    ),
    # --- Method / equipment ---------------------------------------------
    Fixture(
        name='Slow-Cooker Pulled Pork',
        description='Pork shoulder with bbq spice rub, slow-cooked 8 hours.',
        ingredients=['4 lb pork shoulder', 'paprika', 'brown sugar', 'cumin', 'garlic powder', '1 cup bbq sauce'],
        minutes=480,
        expect={
            'main_ingredient': 'pork', 'time_bucket': 'two_plus_hr',
            'methods_superset': {'slow_cooker'},
        },
    ),
    Fixture(
        name='Air Fryer Crispy Brussels Sprouts',
        description='Halved sprouts tossed with olive oil and balsamic, air-fried until crisp.',
        ingredients=['1 lb brussels sprouts', '2 tbsp olive oil', 'salt', 'balsamic glaze'],
        minutes=20,
        expect={'methods_superset': {'air_fryer'}, 'time_bucket': 'under_30'},
    ),
    Fixture(
        name='Sheet-Pan Salmon and Asparagus',
        description='Salmon fillets and asparagus roasted on one pan with lemon.',
        ingredients=['4 salmon fillets', '1 lb asparagus', 'olive oil', 'lemon', 'garlic'],
        minutes=25,
        expect={
            'main_ingredient': 'salmon', 'category': 'dinner',
            'methods_superset': {'sheet_pan', 'oven'},
            'tags_superset': {'weeknight'},
        },
    ),
    Fixture(
        name='No-Cook Caprese Skewers',
        description='Cherry tomato, basil, and mozzarella on toothpicks; balsamic drizzle.',
        ingredients=['1 pint cherry tomatoes', '8 oz mozzarella balls', 'fresh basil', 'balsamic glaze'],
        minutes=10,
        expect={
            'category': 'appetizers', 'time_bucket': 'under_15',
            'methods_superset': {'no_cook'},
        },
    ),
    # --- Categories other than dinner -----------------------------------
    Fixture(
        name='Classic Pancakes',
        description='Fluffy buttermilk pancakes for a weekend breakfast.',
        ingredients=['2 cups flour', '2 eggs', '1.5 cups buttermilk', '2 tbsp sugar', '1 tsp baking powder', 'butter'],
        minutes=25,
        expect={'category': 'breakfast'},
    ),
    Fixture(
        name='Chocolate Chip Cookies',
        description='Soft, chewy classic cookies with semisweet chips.',
        ingredients=['2 cups flour', '1 cup butter', '3/4 cup brown sugar', '2 eggs', '2 cups chocolate chips'],
        minutes=30,
        expect={'category': 'desserts', 'main_ingredient': 'chocolate'},
    ),
    Fixture(
        name='Old Fashioned',
        description='Classic whiskey cocktail with sugar, bitters, and orange peel.',
        ingredients=['2 oz bourbon', 'sugar cube', '2 dashes angostura bitters', 'orange peel'],
        minutes=5,
        expect={'category': 'drinks', 'time_bucket': 'under_15'},
    ),
    Fixture(
        name='Honey Roasted Carrots',
        description='Side dish — carrots roasted with honey, butter, and thyme.',
        ingredients=['2 lb carrots', '3 tbsp butter', '2 tbsp honey', 'thyme'],
        minutes=40,
        expect={
            'category': 'sides', 'main_ingredient': 'vegetables',
            'methods_superset': {'oven'},
        },
    ),
    Fixture(
        name='Hummus',
        description='Smooth chickpea dip with tahini, lemon, and garlic.',
        ingredients=['1 can chickpeas', '1/4 cup tahini', '2 cloves garlic', '1 lemon', 'olive oil', 'cumin'],
        minutes=10,
        expect={
            'main_ingredient': 'chickpeas',
            'cuisine': 'middle_eastern',
            'diets_superset': {'vegan', 'vegetarian', 'gluten_free'},
        },
    ),
    Fixture(
        # Banana bread sits between breakfast and dessert in real menus;
        # don't penalize either reading.
        name='Banana Bread',
        description='Single-loaf banana bread with walnuts.',
        ingredients=['3 ripe bananas', '2 cups flour', '1/2 cup sugar', '1/3 cup melted butter', '2 eggs', '1/2 cup walnuts'],
        minutes=75,
        expect={
            'category_oneof': ['desserts', 'breakfast', 'snacks'],
            'methods_superset': {'oven'},
            'time_bucket': 'one_to_two_hr',
        },
    ),
    # --- Tag detection --------------------------------------------------
    Fixture(
        name='30-Minute Weeknight Chicken Stir-Fry',
        description='Quick weeknight dinner: chicken, broccoli, soy-ginger sauce, jasmine rice.',
        ingredients=['1 lb chicken breast', '1 head broccoli', 'soy sauce', 'ginger', 'garlic', '1 cup jasmine rice'],
        minutes=30,
        expect={
            'cuisine': 'chinese', 'main_ingredient': 'chicken',
            'tags_superset': {'weeknight'},
        },
    ),
    Fixture(
        name='Holiday Beef Tenderloin Roast',
        description='Christmas-dinner centerpiece — whole tenderloin with herb butter.',
        ingredients=['4 lb beef tenderloin', '4 tbsp butter', 'rosemary', 'thyme', 'garlic'],
        minutes=90,
        expect={
            'main_ingredient': 'beef', 'category': 'dinner',
            'tags_superset': {'holiday'},
            'methods_superset': {'oven'},
        },
    ),
    Fixture(
        name='Make-Ahead Freezer Burritos',
        description='Batch-cook breakfast burritos that freeze well for 3 months.',
        ingredients=['12 flour tortillas', '8 eggs', '1 lb breakfast sausage', '2 cups shredded cheese', '1 cup salsa', '4 cups potatoes'],
        minutes=60,
        expect={
            'category': 'breakfast',
            'tags_superset': {'meal_prep', 'freezer_friendly'},
        },
    ),
]


@needs_api
@pytest.mark.parametrize('fx', FIXTURES, ids=lambda fx: fx.name)
def test_haiku_classifier_golden(fx: Fixture):
    """Each fixture asserts the central facets the classifier must land on.

    Multi-valued expects use ``<facet>_superset``: actual must contain
    every value, may contain more. Single-valued expects must match
    exactly.
    """
    r = classify(
        name=fx.name,
        description=fx.description,
        ingredients=fx.ingredients,
        total_time_minutes=fx.minutes,
    )
    assert r.error is None, f'unexpected error: {r.error}'

    for key in ('category', 'cuisine', 'main_ingredient', 'time_bucket'):
        if key in fx.expect:
            assert getattr(r, key) == fx.expect[key], (
                f'{key}: expected {fx.expect[key]!r}, got {getattr(r, key)!r} '
                f'(full: {r.to_dict()})'
            )
        # _oneof: accept any value from the listed options. Used for
        # genuinely ambiguous single-valued facets (banana bread is
        # legitimately breakfast OR dessert, etc.).
        oneof_key = f'{key}_oneof'
        if oneof_key in fx.expect:
            allowed = set(fx.expect[oneof_key])
            actual = getattr(r, key)
            assert actual in allowed, (
                f'{key}: expected one of {allowed!r}, got {actual!r} '
                f'(full: {r.to_dict()})'
            )

    for plural, attr in [
        ('diets_superset', 'diets'),
        ('methods_superset', 'methods'),
        ('tags_superset', 'tags'),
    ]:
        if plural in fx.expect:
            actual = set(getattr(r, attr))
            need = set(fx.expect[plural])
            missing = need - actual
            assert not missing, (
                f'{attr}: missing {missing} from actual {actual} '
                f'(full: {r.to_dict()})'
            )
