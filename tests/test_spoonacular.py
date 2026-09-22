"""Spoonacular importer, offline (fixtures captured from the real API with
the key removed)."""

import pytest

from conftest import FakeSession, assert_program_shape, load_fixture
from rhylthyme_importers import ImporterRegistry, SpoonacularImporter


@pytest.fixture
def importer():
    imp = SpoonacularImporter(api_key="test-key")
    imp.session = FakeSession({
        "/recipes/complexSearch": load_fixture("spoonacular", "complexSearch_chicken.json"),
        "/recipes/633959/information": load_fixture("spoonacular", "information_633959.json"),
        "/recipes/random": {"recipes": [load_fixture("spoonacular", "information_633959.json")]},
    })
    return imp


def test_registered_and_recognises_its_urls():
    imp = ImporterRegistry.get("spoonacular")
    assert isinstance(imp, SpoonacularImporter)
    assert imp.can_import("https://spoonacular.com/recipes/balti-chicken-633959")
    assert not imp.can_import("https://www.themealdb.com/meal/52772")


def test_without_a_key_it_says_so(monkeypatch):
    monkeypatch.delenv("SPOONACULAR_API_KEY", raising=False)
    imp = SpoonacularImporter()
    assert imp.search("chicken") == []
    result = imp.import_from_url("633959")
    assert result.success is False and "SPOONACULAR_API_KEY" in result.error


def test_the_key_is_sent_as_a_parameter_never_in_the_program(importer):
    result = importer.import_from_url("633959")
    assert result.success, result.error
    assert all(params.get("apiKey") == "test-key" for _url, params in importer.session.calls)
    import json
    assert "test-key" not in json.dumps(result.program)


@pytest.mark.parametrize("ref", ["633959", "https://spoonacular.com/recipes/balti-chicken-633959"])
def test_import(importer, ref):
    result = importer.import_from_url(ref)
    assert result.success, result.error
    program = result.program
    assert_program_shape(program)
    assert program["name"] == "Balti Chicken"
    steps = [s for t in program["tracks"] for s in t["steps"]]
    assert len(steps) >= 4, "the four analysed instructions"
    assert len(program["metadata"]["ingredients"]) == 7
    assert {s["task"] for s in steps} == {"prep-work", "stove-burner"}, "pan and wok steps go on the burner"


def test_search_carries_ids_and_urls(importer):
    hits = importer.search("chicken")
    assert [h["name"] for h in hits] == ["Bbq Chicken", "Turbo Chicken", "Balti Chicken"]
    assert hits[2]["url"].endswith("-633959")


def test_bad_references(importer):
    assert importer.import_from_url("https://spoonacular.com/recipes/").success is False
    assert importer.import_from_url("000000").success is False
