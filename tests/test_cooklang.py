"""
Unit tests for CooklangImporter.

Tests exercise external behavior (output program JSON) given CookLang text input.
No file I/O or network calls are made — all fixtures are inline strings.
"""

import pytest
from rhylthyme_importers.cooklang import (
    CooklangImporter,
    github_blob_to_raw,
    _is_cook_input,
    _timer_to_seconds,
)


@pytest.fixture
def importer():
    return CooklangImporter()


# ---------------------------------------------------------------------------
# github_blob_to_raw
# ---------------------------------------------------------------------------

class TestGithubBlobToRaw:
    def test_converts_blob_url(self):
        url = "https://github.com/user/repo/blob/main/recipes/pasta.cook"
        assert github_blob_to_raw(url) == (
            "https://raw.githubusercontent.com/user/repo/main/recipes/pasta.cook"
        )

    def test_nested_path(self):
        url = "https://github.com/user/repo/blob/feature-branch/a/b/c.cook"
        assert github_blob_to_raw(url) == (
            "https://raw.githubusercontent.com/user/repo/feature-branch/a/b/c.cook"
        )

    def test_non_github_url_unchanged(self):
        url = "https://example.com/recipes/pasta.cook"
        assert github_blob_to_raw(url) == url

    def test_raw_url_unchanged(self):
        url = "https://raw.githubusercontent.com/user/repo/main/pasta.cook"
        assert github_blob_to_raw(url) == url


# ---------------------------------------------------------------------------
# _is_cook_input / can_import
# ---------------------------------------------------------------------------

class TestCanImport:
    @pytest.mark.parametrize("s", [
        "recipe.cook",
        "/home/user/recipes/pasta.cook",
        "https://example.com/pasta.cook",
        "https://github.com/user/repo/blob/main/pasta.cook",
    ])
    def test_true_for_cook_inputs(self, importer, s):
        assert importer.can_import(s) is True

    @pytest.mark.parametrize("s", [
        "recipe.json",
        "https://example.com/recipe.html",
        "https://github.com/user/repo/blob/main/recipe.json",
        "chicken tikka masala",
    ])
    def test_false_for_non_cook_inputs(self, importer, s):
        assert importer.can_import(s) is False


# ---------------------------------------------------------------------------
# _timer_to_seconds
# ---------------------------------------------------------------------------

class TestTimerToSeconds:
    def _make_timing(self, amount, unit):
        """Minimal duck-type stand-in for a cooklang-py Timing object."""
        class FakeQty:
            pass
        class FakeTiming:
            pass
        qty = FakeQty()
        qty.amount = amount
        qty.unit = unit
        t = FakeTiming()
        t.quantity = qty
        return t

    def test_minutes(self):
        assert _timer_to_seconds(self._make_timing(10, "minutes")) == 600

    def test_hours(self):
        assert _timer_to_seconds(self._make_timing(2, "hours")) == 7200

    def test_seconds(self):
        assert _timer_to_seconds(self._make_timing(30, "seconds")) == 30

    def test_singular_unit(self):
        assert _timer_to_seconds(self._make_timing(5, "minute")) == 300

    def test_zero_amount(self):
        assert _timer_to_seconds(self._make_timing(0, "minutes")) == 0


# ---------------------------------------------------------------------------
# Duration extraction
# ---------------------------------------------------------------------------

class TestDuration:
    TIMER_RECIPE = "Cook @pasta{400%g} in a #pot for ~{10%minutes}."

    def test_timer_produces_fixed_duration(self, importer):
        result = importer.import_from_content(self.TIMER_RECIPE)
        assert result.success
        step = result.program["tracks"][0]["steps"][0]
        assert step["duration"]["type"] == "fixed"
        assert step["duration"]["seconds"] == 600

    def test_no_timer_produces_nonzero_duration(self, importer):
        content = "Chop @onion{1} finely."
        result = importer.import_from_content(content)
        assert result.success
        step = result.program["tracks"][0]["steps"][0]
        assert step["duration"]["seconds"] > 0

    def test_prose_range_produces_variable_duration(self, importer):
        content = "Simmer the sauce for 5 to 15 minutes."
        result = importer.import_from_content(content)
        assert result.success
        step = result.program["tracks"][0]["steps"][0]
        assert step["duration"]["type"] == "variable"
        assert step["duration"]["minSeconds"] == 300
        assert step["duration"]["maxSeconds"] == 900
        assert step["duration"]["defaultSeconds"] == 600  # (5+15)*60//2

    def test_prose_single_time_produces_fixed_duration(self, importer):
        content = "Bake the bread for 30 minutes."
        result = importer.import_from_content(content)
        assert result.success
        step = result.program["tracks"][0]["steps"][0]
        assert step["duration"]["type"] == "fixed"
        assert step["duration"]["seconds"] == 1800


# ---------------------------------------------------------------------------
# Metadata mapping
# ---------------------------------------------------------------------------

class TestMetadata:
    FULL_META = """\
---
title: Pasta Arrabiata
description: A spicy Italian classic
servings: 4
source: https://example.com/arrabiata
tags: pasta, italian
---
Cook @pasta{400%g} for ~{10%minutes}.
"""

    def test_title_becomes_program_name(self, importer):
        result = importer.import_from_content(self.FULL_META)
        assert result.success
        assert result.program["name"] == "Pasta Arrabiata"

    def test_description_mapped(self, importer):
        result = importer.import_from_content(self.FULL_META)
        assert "spicy Italian" in result.program["description"]

    def test_servings_in_metadata(self, importer):
        result = importer.import_from_content(self.FULL_META)
        assert result.program["metadata"]["servings"] == 4

    def test_source_url_in_metadata(self, importer):
        result = importer.import_from_content(self.FULL_META)
        assert result.program["metadata"]["source"]["url"] == "https://example.com/arrabiata"

    def test_tags_in_metadata(self, importer):
        result = importer.import_from_content(self.FULL_META)
        assert result.program["metadata"]["tags"] is not None

    def test_filename_used_when_no_title(self, importer):
        content = "Chop @onion{1}."
        result = importer.import_from_content(content, source_name="my_recipe")
        assert result.success
        assert "My Recipe" in result.program["name"]

    def test_environment_type_is_kitchen(self, importer):
        content = "Chop @onion{1}."
        result = importer.import_from_content(content)
        assert result.program["environmentType"] == "kitchen"

    def test_actors_is_2(self, importer):
        content = "Chop @onion{1}."
        result = importer.import_from_content(content)
        assert result.program["actors"] == 2


# ---------------------------------------------------------------------------
# Ingredient aggregation
# ---------------------------------------------------------------------------

class TestIngredients:
    def test_ingredients_extracted(self, importer):
        content = (
            "Chop @onion{1} and @garlic{3%cloves}.\n\n"
            "Fry @onion{1} in @oil{2%tbsp}."
        )
        result = importer.import_from_content(content)
        assert result.success
        ingredients = result.program["metadata"]["ingredients"]
        names = [i["name"].lower() for i in ingredients]
        assert "onion" in names
        assert "garlic" in names
        assert "oil" in names

    def test_ingredients_deduplicated(self, importer):
        content = (
            "Add @salt{1%tsp}.\n\n"
            "Add more @salt{2%tsp} to taste."
        )
        result = importer.import_from_content(content)
        names = [i["name"].lower() for i in result.program["metadata"]["ingredients"]]
        assert names.count("salt") == 1

    def test_ingredient_measure_preserved(self, importer):
        content = "Use @flour{200%g}."
        result = importer.import_from_content(content)
        flour = next(
            i for i in result.program["metadata"]["ingredients"]
            if i["name"].lower() == "flour"
        )
        assert "200" in flour["measure"]
        assert "g" in flour["measure"]


# ---------------------------------------------------------------------------
# Track structure
# ---------------------------------------------------------------------------

class TestTracks:
    def test_single_track_for_single_task_recipe(self, importer):
        content = (
            "Boil @water{1%L} in a #pot.\n\n"
            "Add @pasta{400%g} and cook for ~{10%minutes} in #pot."
        )
        result = importer.import_from_content(content)
        assert result.success
        assert len(result.program["tracks"]) == 1

    def test_two_tracks_for_oven_and_stovetop(self, importer):
        content = (
            "Heat sauce in a #pan for ~{10%minutes}.\n\n"
            "Bake bread in #oven for ~{30%minutes}."
        )
        result = importer.import_from_content(content)
        assert result.success
        assert len(result.program["tracks"]) == 2
        track_ids = {t["trackId"] for t in result.program["tracks"]}
        assert "stovetop" in track_ids
        assert "oven" in track_ids

    def test_three_tracks_for_oven_stovetop_prep(self, importer):
        content = (
            "Chop @onion{1} finely.\n\n"
            "Fry @onion in #pan for ~{5%minutes}.\n\n"
            "Bake @chicken{1} in #oven for ~{40%minutes}."
        )
        result = importer.import_from_content(content)
        assert result.success
        assert len(result.program["tracks"]) == 3

    def test_only_first_recipe_step_starts_at_program_start(self, importer):
        """Triggers follow recipe order, not per-track ordering. Only the step
        that appears first in the source file starts at programStart; every
        later step (even on a different track) depends on its predecessor in
        recipe order. This prevents cookware sitting idle/burning while another
        track runs a long wait."""
        content = (
            "Fry @onion in #pan for ~{5%minutes}.\n\n"
            "Bake @chicken{1} in #oven for ~{40%minutes}."
        )
        result = importer.import_from_content(content)
        stovetop = next(t for t in result.program["tracks"] if t["trackId"] == "stovetop")
        oven = next(t for t in result.program["tracks"] if t["trackId"] == "oven")
        assert stovetop["steps"][0]["startTrigger"]["type"] == "programStart"
        assert oven["steps"][0]["startTrigger"] == {
            "type": "afterStep",
            "stepId": stovetop["steps"][0]["stepId"],
        }

    def test_steps_within_track_are_sequential(self, importer):
        content = (
            "Heat #pan.\n\n"
            "Fry @onion in #pan for ~{5%minutes}.\n\n"
            "Add @garlic to #pan and cook for ~{2%minutes}."
        )
        result = importer.import_from_content(content)
        stovetop = next(t for t in result.program["tracks"] if t["trackId"] == "stovetop")
        steps = stovetop["steps"]
        assert steps[0]["startTrigger"]["type"] == "programStart"
        assert steps[1]["startTrigger"]["type"] == "afterStep"
        assert steps[1]["startTrigger"]["stepId"] == steps[0]["stepId"]
        assert steps[2]["startTrigger"]["stepId"] == steps[1]["stepId"]

    def test_no_cookware_produces_prep_track(self, importer):
        content = "Chop @onion{1} finely."
        result = importer.import_from_content(content)
        assert result.success
        assert result.program["tracks"][0]["trackId"] == "prep"

    def test_cookware_context_carries_to_continuation_steps(self, importer):
        """A step with no explicit cookware but clear cooking verbs and a timer
        should inherit the cookware from the previous step. Without this, the
        continuation step falls back to the prep track and runs in parallel
        with the cooking, which breaks the cooking timeline."""
        content = (
            "Melt the @butter in a #frying pan{}.\n\n"
            "Pour in the @batter and cook for ~{2%minutes}."
        )
        result = importer.import_from_content(content)
        assert result.success
        # Both steps should be on the stovetop track — the pour step inherits
        # the frying pan from the melt step.
        assert len(result.program["tracks"]) == 1
        assert result.program["tracks"][0]["trackId"] == "stovetop"

    def test_melting_does_not_start_before_long_prep_finishes(self, importer):
        """The butter-burning regression: if a rest/wait step happens before
        a short stovetop prep, the stovetop prep must wait for the rest to
        finish rather than starting at t=0 and burning the fat."""
        content = (
            "Mix @flour{100%g} and @water{200%ml} in a #bowl{}.\n\n"
            "Leave to stand for ~{15%minutes}.\n\n"
            "Melt the @butter in a #frying pan{}.\n\n"
            "Pour in the batter and cook for ~{2%minutes}."
        )
        result = importer.import_from_content(content)
        assert result.success
        # Find the melt step on the stovetop track
        stovetop = next(t for t in result.program["tracks"] if t["trackId"] == "stovetop")
        melt = stovetop["steps"][0]
        # Its trigger must reference the preceding rest step, not programStart
        assert melt["startTrigger"]["type"] == "afterStep"
        prep = next(t for t in result.program["tracks"] if t["trackId"] == "prep")
        rest_step_id = prep["steps"][-1]["stepId"]  # last prep step before melt
        assert melt["startTrigger"]["stepId"] == rest_step_id

    def test_renamed_intermediate_still_chains_via_continuation(self, importer):
        """Ingredient-producer tracking alone would find no producer for
        '@batter' (it was never produced under that name — the mixing step
        only produced 'flour'/'water'), so this depends entirely on the
        cookware-continuation signal to get a real startTrigger instead of
        a spurious programStart."""
        content = (
            "Melt the @butter in a #frying pan{}.\n\n"
            "Pour in the @batter and cook for ~{2%minutes}."
        )
        result = importer.import_from_content(content)
        assert result.success
        stovetop = result.program["tracks"][0]["steps"]
        assert stovetop[0]["startTrigger"] == {"type": "programStart"}
        assert stovetop[1]["startTrigger"] == {
            "type": "afterStep", "stepId": stovetop[0]["stepId"],
        }

    def test_independent_bowls_merge_at_combine_step(self, importer):
        """Two threads that never share cookware or ingredient names until a
        later step explicitly reuses both of their tagged ingredients should
        produce a real logic:"all" merge referencing both producer steps —
        this is the actual new capability layered ingredient-producer
        tracking adds on top of the pre-existing document-order default."""
        content = (
            "Cream @butter{115%g} and @sugar{200%g} in a #bowl{}.\n\n"
            "Sift @flour{80%g} and @salt{1%tsp} in a #separate bowl{}.\n\n"
            "Fold the @flour and @butter mixtures together in a #bowl{}."
        )
        result = importer.import_from_content(content)
        assert result.success
        prep = next(t for t in result.program["tracks"] if t["trackId"] == "prep")
        cream_step, sift_step, fold_step = prep["steps"]

        trigger = fold_step["startTrigger"]
        assert trigger["logic"] == "all"
        referenced = {t["stepId"] for t in trigger["triggers"]}
        assert referenced == {cream_step["stepId"], sift_step["stepId"]}

    def test_continuation_step_becomes_new_producer_for_inherited_ingredients(self, importer):
        """A step that continues cooking (same bowl, no re-tagging) must take
        over producer ownership of everything the step it continues from
        owned — not just its own newly-tagged ingredients. Otherwise a later
        step referencing an earlier tag by name (e.g. '@butter' after a
        'whisk in the eggs' step that never re-tags butter) resolves to the
        stale original producer instead of the continuation, silently
        skipping whatever the continuation step added on top."""
        content = (
            "Cream @butter{115%g} and @sugar{200%g} in a #bowl{}.\n\n"
            "Whisk in @vanilla extract{5%mL} and @eggs{2}.\n\n"
            "Sift @flour{280%g} and @salt{3%g} in a #separate bowl{}.\n\n"
            "Fold the @flour and @butter mixtures together in a #bowl{}."
        )
        result = importer.import_from_content(content)
        assert result.success
        prep = next(t for t in result.program["tracks"] if t["trackId"] == "prep")
        cream_step, whisk_step, sift_step, fold_step = prep["steps"]

        # The whisk step continues from cream (same bowl, no new cookware).
        assert whisk_step["startTrigger"] == {
            "type": "afterStep", "stepId": cream_step["stepId"],
        }
        # Fold references "@butter" again — this must resolve to whisk (the
        # step that now owns butter/sugar/vanilla/eggs), not back to the
        # stale cream step, or the eggs/vanilla whisk added would be
        # silently dropped from the merge.
        trigger = fold_step["startTrigger"]
        assert trigger["logic"] == "all"
        referenced = {t["stepId"] for t in trigger["triggers"]}
        assert referenced == {whisk_step["stepId"], sift_step["stepId"]}
        assert cream_step["stepId"] not in referenced

    def test_meanwhile_keyword_starts_independent_parallel_branch(self, importer):
        """Explicit 'meanwhile' phrasing is the one positive signal that
        overrides the sequential document-order default and starts a
        genuinely independent branch at programStart, enabling real
        parallel scheduling instead of forced serialization."""
        content = (
            "Roast @chicken{1} in the #oven for ~{40%minutes}.\n\n"
            "Meanwhile, chop @onion{1} and @garlic{2%cloves} in a #bowl{}."
        )
        result = importer.import_from_content(content)
        assert result.success
        prep = next(t for t in result.program["tracks"] if t["trackId"] == "prep")
        chop_step = prep["steps"][0]
        assert chop_step["startTrigger"] == {"type": "programStart"}

    def test_in_a_separate_bowl_starts_independent_parallel_branch(self, importer):
        content = (
            "Cream @butter{115%g} and @sugar{200%g} in a #bowl{}.\n\n"
            "In a separate bowl, whisk @egg{2} until fluffy."
        )
        result = importer.import_from_content(content)
        assert result.success
        prep = next(t for t in result.program["tracks"] if t["trackId"] == "prep")
        whisk_step = prep["steps"][1]
        assert whisk_step["startTrigger"] == {"type": "programStart"}

    def test_melt_uses_short_default_duration(self, importer):
        """Untimed 'melt X in pan' should default to ~60s, not the generic
        5-minute stove-burner default. Otherwise butter sits burning between
        melt and the next step."""
        content = "Melt the @butter in a #frying pan{}."
        result = importer.import_from_content(content)
        step = result.program["tracks"][0]["steps"][0]
        assert step["duration"] == {"type": "fixed", "seconds": 60}

    def test_n_further_minute_parses_as_duration(self, importer):
        """Prose like 'cook for 1 further minute' must yield a 60s duration,
        not fall through to the task default."""
        content = "Flip the pancake and cook for 1 further minute."
        result = importer.import_from_content(content)
        step = result.program["tracks"][0]["steps"][0]
        assert step["duration"] == {"type": "fixed", "seconds": 60}

    def test_anonymous_timer_rendered_inline_in_description(self, importer):
        """`~{15%minutes}` has an empty `.name` — the renderer must still show
        the duration in the prose instead of dropping it entirely."""
        content = "Pour into a #bowl{} and leave to stand for ~{15%minutes}."
        result = importer.import_from_content(content)
        desc = result.program["tracks"][0]["steps"][0]["description"]
        assert "15 minutes" in desc, f"expected duration in description, got: {desc!r}"

    def test_named_timer_rendered_with_parenthesized_duration(self, importer):
        """Named timers like `~simmer{10%min}` render as 'simmer (10 min)'."""
        content = "Let the sauce ~simmer{10%min} on the #pan{}."
        result = importer.import_from_content(content)
        desc = result.program["tracks"][0]["steps"][0]["description"]
        assert "simmer (10 min)" in desc, f"got: {desc!r}"

    def test_step_name_strips_duration_phrase(self, importer):
        """Step names shouldn't carry 'for 15 minutes' — the duration field
        already surfaces it and leaving it in forces ugly mid-number truncation."""
        content = "Pour into a #bowl{} and leave to stand for ~{15%minutes}."
        result = importer.import_from_content(content)
        name = result.program["tracks"][0]["steps"][0]["name"]
        assert "15" not in name
        assert "minute" not in name.lower()

    def test_program_validates(self, importer):
        """Generated program should pass basic structural checks."""
        content = (
            "---\ntitle: Test\n---\n"
            "Chop @onion{1}.\n\n"
            "Fry in #pan for ~{5%minutes}."
        )
        result = importer.import_from_content(content)
        assert result.success
        p = result.program
        assert p["programId"]
        assert p["tracks"]
        assert all("stepId" in s for t in p["tracks"] for s in t["steps"])
        assert all("task" in s for t in p["tracks"] for s in t["steps"])


# ---------------------------------------------------------------------------
# Resource constraints
# ---------------------------------------------------------------------------

class TestConstraints:
    def test_only_used_tasks_in_constraints(self, importer):
        content = "Bake @cake{1} in #oven{} for ~{30%minutes}."
        result = importer.import_from_content(content)
        tasks = {c["task"] for c in result.program["resourceConstraints"]}
        assert "oven" in tasks
        assert "stove-burner" not in tasks

    def test_oven_max_concurrent_is_1(self, importer):
        content = "Bake @cake{1} in #oven{} for ~{30%minutes}."
        result = importer.import_from_content(content)
        oven = next(c for c in result.program["resourceConstraints"] if c["task"] == "oven")
        assert oven["maxConcurrent"] == 1

    def test_stove_burner_max_concurrent_is_2(self, importer):
        content = "Fry @onion{1} in a #pan for ~{5%minutes}."
        result = importer.import_from_content(content)
        burner = next(c for c in result.program["resourceConstraints"] if c["task"] == "stove-burner")
        assert burner["maxConcurrent"] == 2


# ---------------------------------------------------------------------------
# Cookware → task mapping
# ---------------------------------------------------------------------------

class TestCookwareMapping:
    @pytest.mark.parametrize("cookware,expected_task", [
        ("oven",        "oven"),
        ("baking sheet", "oven"),
        ("pan",         "stove-burner"),
        ("skillet",     "stove-burner"),
        ("wok",         "stove-burner"),
        ("pot",         "stove-burner"),
        ("grill",       "grill"),
        ("microwave",   "microwave"),
        ("fridge",      "refrigeration"),
        ("freezer",     "refrigeration"),
        ("knife",       "prep-work"),
        ("bowl",        "prep-work"),
    ])
    def test_cookware_mapping(self, importer, cookware, expected_task):
        # Use a multi-word cookware name as needed
        if " " in cookware:
            cook_annotation = f"#{cookware}{{}}"
        else:
            cook_annotation = f"#{cookware}"
        content = f"Use {cook_annotation} for ~{{5%minutes}}."
        result = importer.import_from_content(content)
        assert result.success, result.error
        step = result.program["tracks"][0]["steps"][0]
        assert step["task"] == expected_task

    def test_no_cookware_defaults_to_prep_work(self, importer):
        content = "Chop @onion{1} finely."
        result = importer.import_from_content(content)
        step = result.program["tracks"][0]["steps"][0]
        assert step["task"] == "prep-work"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_empty_content_returns_failure(self, importer):
        result = importer.import_from_content("")
        assert not result.success
        assert result.error

    def test_search_returns_empty_list(self, importer):
        assert importer.search("pasta") == []


def test_a_default_importer_never_reads_a_local_path(tmp_path, monkeypatch):
    """Whatever Cooklang reads comes back as recipe text, so an importer
    reachable from user input must not open files."""
    import requests

    from rhylthyme_importers.cooklang import CooklangImporter

    secret = tmp_path / "secrets.env"
    secret.write_text("API_KEY=super-secret-value-123\n")

    def no_such_url(url, **kw):
        raise requests.exceptions.MissingSchema(f"Invalid URL {url!r}")

    monkeypatch.setattr(requests, "get", no_such_url)
    result = CooklangImporter().import_from_url(str(secret))
    assert result.success is False
    assert "super-secret-value-123" not in json_dump(result)

    allowed = CooklangImporter(allow_local_files=True).import_from_url(str(secret))
    assert allowed.success is True, "the command line and the upload route opt in"


def json_dump(result):
    import json

    return json.dumps({"program": result.program, "error": result.error}, default=str)
