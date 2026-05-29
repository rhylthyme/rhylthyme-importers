"""
Controlled vocabulary for the recipe taxonomy / faceted-search system.

This module is the single source of truth. All three downstream consumers
import from here so the LLM classifier, the API query-validator, and the
frontend filter UI agree on what's a valid value:

  - ``recipe_classifier`` constrains Claude Haiku output to these slugs.
  - ``/api/public/search`` validates query params against these sets and
    rejects unknowns with HTTP 400.
  - The gallery UI builds its filter sidebar from these lists, mapping
    each slug to a translated display label via ``I18N`` keys.

Adding a new value is a one-line change here, and it propagates everywhere.
Removing or renaming a value MUST be paired with a one-shot data migration
script that updates existing rows.

Conventions:
  - Slugs are English-locale, lowercase, snake_case, no spaces.
  - Display labels are NOT defined here — they live in the frontend's
    ``I18N`` strings under ``taxonomy.<facet>.<slug>``.

Total scope (matches PRD): 8 categories, ~25 cuisines, ~12 diets, 5 time
buckets, ~30 main ingredients, ~10 methods, ~40 starter tags.
"""

from __future__ import annotations

# --- Categories (single-select, top-level browse axis) ----------------------

CATEGORIES: tuple[str, ...] = (
    'breakfast',
    'lunch',
    'dinner',
    'appetizers',
    'desserts',
    'drinks',
    'snacks',
    'sides',
)


# --- Cuisine / region (single-select facet) --------------------------------

CUISINES: tuple[str, ...] = (
    'american',
    'british',
    'cajun',
    'caribbean',
    'chinese',
    'eastern_european',
    'french',
    'german',
    'greek',
    'indian',
    'italian',
    'japanese',
    'korean',
    'mediterranean',
    'mexican',
    'middle_eastern',
    'nordic',
    'southern_us',
    'soul_food',
    'spanish',
    'thai',
    'tex_mex',
    'turkish',
    'vietnamese',
)


# --- Diet (multi-select; a recipe can satisfy multiple) --------------------

DIETS: tuple[str, ...] = (
    'vegan',
    'vegetarian',
    'gluten_free',
    'dairy_free',
    'keto',
    'low_carb',
    'paleo',
    'pescatarian',
    'halal',
    'kosher',
    'nut_free',
    'egg_free',
)


# --- Time bucket (single-select; derived from program's total minutes) ----
#
# Bucket → max minutes. The classifier picks the smallest bucket whose
# ceiling is >= the recipe's total time. ``two_plus_hr`` has no upper bound.
TIME_BUCKETS: tuple[str, ...] = (
    'under_15',
    'under_30',
    'under_60',
    'one_to_two_hr',
    'two_plus_hr',
)

TIME_BUCKET_MAX_MINUTES: dict[str, int | None] = {
    'under_15': 15,
    'under_30': 30,
    'under_60': 60,
    'one_to_two_hr': 120,
    'two_plus_hr': None,
}


def time_bucket_for_minutes(minutes: int | None) -> str | None:
    """Map a total-time-in-minutes value to the matching bucket slug.
    Returns ``None`` if minutes is missing or non-positive."""
    if minutes is None or minutes <= 0:
        return None
    for slug in TIME_BUCKETS:
        ceiling = TIME_BUCKET_MAX_MINUTES[slug]
        if ceiling is None or minutes <= ceiling:
            return slug
    return TIME_BUCKETS[-1]


# --- Main ingredient (single-select facet) ---------------------------------

MAIN_INGREDIENTS: tuple[str, ...] = (
    'beans',
    'beef',
    'broccoli',
    'cauliflower',
    'cheese',
    'chicken',
    'chickpeas',
    'chocolate',
    'duck',
    'eggs',
    'fish',
    'fruit',
    'lamb',
    'lentils',
    'mushrooms',
    'nuts',
    'pasta',
    'pork',
    'potatoes',
    'rice',
    'salmon',
    'shrimp',
    'spinach',
    'squash',
    'tofu',
    'tomatoes',
    'turkey',
    'vegetables',
    'wheat',
    'zucchini',
)


# --- Method / equipment (multi-select facet) -------------------------------

METHODS: tuple[str, ...] = (
    'air_fryer',
    'dutch_oven',
    'grill',
    'instant_pot',
    'no_cook',
    'oven',
    'pressure_cooker',
    'sheet_pan',
    'slow_cooker',
    'smoker',
    'stovetop',
    'wok',
)


# --- Tags (multi-valued, open-ended editorial signals) ---------------------
#
# Starter set per the PRD. Tags are stored in the ``recipe_tags`` join table
# (one row per (recipe, tag)). New tag slugs land here via PR; the same value
# must be included in the corresponding I18N translations.
TAGS: tuple[str, ...] = (
    'advanced_technique',
    'beginner_friendly',
    'breakfast_for_dinner',
    'budget',
    'christmas',
    'comfort_food',
    'cozy',
    'creamy',
    'crispy',
    'easter',
    'festive',
    'freezer_friendly',
    'gluten_free_friendly_swap',
    'healthy',
    'hearty',
    'high_protein',
    'holiday',
    'kid_friendly',
    'leftovers',
    'light',
    'low_effort',
    'luxurious',
    'meal_prep',
    'one_pot',
    'party',
    'picky_eater_approved',
    'picnic',
    'refreshing',
    'romantic',
    'savory',
    'seasonal_fall',
    'seasonal_spring',
    'seasonal_summer',
    'seasonal_winter',
    'smoky',
    'spicy',
    'sweet',
    'thanksgiving',
    'weeknight',
    'brunch',
)


# --- Diet implication graph -------------------------------------------------
#
# A → B means "every recipe that is A is also B". The classifier and the
# search API both apply these so a vegan recipe shows up when the user
# filters for vegetarian (real-world expectation), but a vegetarian
# recipe is NOT auto-tagged vegan (the reverse doesn't hold).
DIET_IMPLIES: dict[str, tuple[str, ...]] = {
    'vegan': ('vegetarian', 'dairy_free', 'egg_free'),
    'keto': ('low_carb',),
    'paleo': ('gluten_free',),
}


def expand_diets(diets: list[str]) -> list[str]:
    """Return the input diet list expanded with all implied diets.
    Output is sorted and deduped."""
    out: set[str] = set()
    for d in diets:
        if d in DIETS_SET:
            out.add(d)
            for implied in DIET_IMPLIES.get(d, ()):
                if implied in DIETS_SET:
                    out.add(implied)
    return sorted(out)


# --- Convenience set views (O(1) `in` checks for validators) ---------------

CATEGORIES_SET = frozenset(CATEGORIES)
CUISINES_SET = frozenset(CUISINES)
DIETS_SET = frozenset(DIETS)
TIME_BUCKETS_SET = frozenset(TIME_BUCKETS)
MAIN_INGREDIENTS_SET = frozenset(MAIN_INGREDIENTS)
METHODS_SET = frozenset(METHODS)
TAGS_SET = frozenset(TAGS)


# --- Combined export for serialization (e.g. for client-side bootstrap) ---

def as_dict() -> dict[str, list[str]]:
    """Return all vocab lists as plain serializable lists.
    Useful for embedding in HTML at render-time so the gallery filter UI
    doesn't need a separate fetch to learn the valid values."""
    return {
        'categories': list(CATEGORIES),
        'cuisines': list(CUISINES),
        'diets': list(DIETS),
        'time_buckets': list(TIME_BUCKETS),
        'main_ingredients': list(MAIN_INGREDIENTS),
        'methods': list(METHODS),
        'tags': list(TAGS),
    }
