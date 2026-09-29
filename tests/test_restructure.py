"""Tests for the rule-based rebuild of bulk-imported recipes."""

import copy

from rhylthyme_importers.restructure import (
    _alternative_label,
    _equipment,
    _step_name,
    parse_duration,
    restructure,
)


def _bulk(steps, name="Test Recipe"):
    """A program in the bulk importers' Preparation + Cooking Steps shape."""
    return {
        "programId": "test",
        "name": name,
        "schemaVersion": "0.1.0",
        "environmentType": "kitchen",
        "metadata": {"ingredients": [{"name": "flour"}, {"name": "sugar"}, {"name": "salt"},
                                     {"name": "ground turkey"}, {"name": "basil"}]},
        "tracks": [
            {"trackId": "prep", "name": "Preparation", "steps": [{
                "stepId": "prep_ingredients", "name": "Gather: flour, sugar",
                "description": "Gather: flour, sugar, salt", "task": "prep-work",
                "duration": {"type": "fixed", "seconds": 300},
                "startTrigger": {"type": "programStart"}}]},
            {"trackId": "cooking", "name": "Cooking Steps", "steps": [
                {"stepId": f"step_{i:02d}", "name": text[:60], "description": text,
                 "task": "prep-work", "duration": {"type": "fixed", "seconds": 180},
                 "startTrigger": {"type": "afterStep",
                                  "stepId": "prep_ingredients" if i == 1 else f"step_{i - 1:02d}"}}
                for i, text in enumerate(steps, start=1)]},
        ],
        "resourceConstraints": [{"task": t, "maxConcurrent": 1} for t in
                                ("prep-work", "oven", "stove-burner", "waiting")],
    }


def _steps(program):
    return {s["stepId"]: (t["name"], s) for t in program["tracks"] for s in t["steps"]}


def test_parse_duration_sums_times_and_ignores_storage():
    assert parse_duration("Bake 25 to 30 minutes, then let cool 10 minutes") == (2100, 2400)
    assert parse_duration("Will keep covered and chilled for up to 24 hrs.") is None
    assert parse_duration("Stir every 30 seconds.") is None


def test_step_name_starts_with_the_verb():
    assert _step_name("Once you have all your toppings on, transfer the bagels to the oven "
                      "and bake for 25 minutes.") == "Transfer the bagels to the oven"
    assert _step_name("In a large bowl, whisk together the flour, sugar and salt.") == \
        "Whisk together the flour, sugar and salt"
    assert _step_name("Start by preheating your oven to 375 degrees F and lining a sheet.") == \
        "Preheat your oven to 375 degrees F"
    assert _step_name("In a heavy-bottomed pan over medium-high heat, brown the pork.") == \
        "Brown the pork"


def test_equipment_reads_the_first_place_mentioned():
    assert _equipment({"description": "In a pan over medium-high heat, brown the pork. "
                                      "Transfer the roast to the slow cooker."}) == "stovetop"
    assert _equipment({"description": "Pour batter into the pan and bake for 40 minutes."}) == "oven"
    assert _equipment({"description": "Line a baking sheet with parchment."}) is None


def test_alternative_label():
    assert _alternative_label("For a gas grill:") == "Gas grill"
    assert _alternative_label("For a skillet:") == "Skillet"
    assert _alternative_label("For the filling:") is None
    assert _alternative_label("Notes:") is None


def test_note_with_inline_colon_is_not_a_component():
    from rhylthyme_importers.restructure import _header_label
    assert _header_label("Note: for flatter Snickerdoodle cookies") == ""
    assert _header_label("For the lemon vinaigrette") == "Lemon vinaigrette"
    assert _header_label("2.:") == ""
    assert _header_label("How to make the creamy Tuscan salmon:") == "Creamy Tuscan salmon"


def test_not_bulk_shape_is_unchanged():
    program = _bulk(["Mix."])
    program["tracks"].append({"trackId": "x", "name": "Extra", "steps": []})
    out, report = restructure(program)
    assert out is program and report["changed"] is False


def test_restructure_does_not_mutate_and_builds_equipment_tracks():
    program = _bulk([
        "Preheat oven to 350°F.",
        "In a large bowl, whisk together the flour, sugar and salt.",
        "Pour into a pan and bake for 30 minutes.",
        "Let cool for 10 minutes.",
    ])
    before = copy.deepcopy(program)
    out, report = restructure(program)
    assert program == before
    assert report["changed"] and out["metadata"]["restructured"]["version"] >= 1
    names = [t["name"] for t in out["tracks"]]
    assert names[0] == "Prep" and "Oven" in names and "Preparation" not in names
    steps = _steps(out)
    track, bake = steps["step_03"]
    assert track == "Oven" and bake["duration"] == {"type": "fixed", "seconds": 1800}
    waits = {t["stepId"] for t in bake["startTrigger"]["triggers"]}
    assert "step_02" in waits and any(w.startswith("preheat") for w in waits)
    assert steps["step_04"][1]["task"] == "waiting"


def test_alternative_methods_become_a_choice():
    program = _bulk([
        "Combine ground turkey and basil.",
        "For a gas grill:",
        "Preheat grill over medium-high heat. Grill the burgers for 5 minutes.",
        "For a skillet:",
        "Heat oil in a skillet over medium heat and cook the burgers for 6 minutes.",
        "Serve the burgers.",
    ])
    out, report = restructure(program)
    assert report["choice"] == 1 and out["schemaVersion"] == "0.2.0-alpha"
    steps = _steps(out)
    choice = next(s for _, s in steps.values() if s.get("choice"))
    assert [o["choiceId"] for o in choice["choice"]["options"]] == ["gas-grill", "skillet"]
    tracks = {t["name"]: t for t in out["tracks"]}
    assert "Gas grill" in tracks and "Skillet" in tracks
    grill_first = tracks["Gas grill"]["steps"][0]["startTrigger"]
    assert grill_first == {"type": "afterStep", "stepId": choice["stepId"], "choiceId": "gas-grill"}
    serve = steps["step_06"][1]["startTrigger"]
    assert serve["logic"] == "any"
    assert {t["stepId"] for t in serve["triggers"]} == {
        tracks["Gas grill"]["steps"][-1]["stepId"], tracks["Skillet"]["steps"][-1]["stepId"]}
