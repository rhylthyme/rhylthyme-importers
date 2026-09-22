"""TheMealDB importer, offline: the API answers come from fixtures captured
from the real service (tests/fixtures/themealdb)."""

import pytest

from conftest import FakeSession, assert_program_shape, load_fixture
from rhylthyme_importers import ImporterRegistry, TheMealDBImporter


@pytest.fixture
def importer():
    imp = TheMealDBImporter()
    imp.session = FakeSession({
        "lookup.php": lambda p: load_fixture("themealdb", "lookup_52772.json") if p.get("i") == "52772" else {"meals": None},
        "search.php": load_fixture("themealdb", "search_teriyaki.json"),
        "random.php": load_fixture("themealdb", "lookup_52772.json"),
        "categories.php": load_fixture("themealdb", "categories.json"),
    })
    return imp


def test_registered_and_recognises_its_urls():
    imp = ImporterRegistry.get("themealdb")
    assert isinstance(imp, TheMealDBImporter)
    assert imp.can_import("https://www.themealdb.com/meal/52772")
    assert imp.can_import("52772")
    assert not imp.can_import("https://www.seriouseats.com/x")
    assert ImporterRegistry.find_for_url("https://www.themealdb.com/meal/52772") is imp


@pytest.mark.parametrize("ref", ["52772", "https://www.themealdb.com/meal/52772", "https://www.themealdb.com/meal/52772-Teriyaki-Chicken-Casserole"])
def test_import_by_id_or_url(importer, ref):
    result = importer.import_from_url(ref)
    assert result.success, result.error
    program = result.program
    assert_program_shape(program)
    assert program["name"] == "Teriyaki Chicken Casserole"
    assert program["environmentType"] == "kitchen"
    names = [i["name"] for i in program["metadata"]["ingredients"]]
    assert "soy sauce" in " ".join(names).lower()
    steps = [s for t in program["tracks"] for s in t["steps"]]
    assert len(steps) >= 3
    assert program["metadata"]["source"]["url"].endswith("52772") or "themealdb" in program["metadata"]["source"]["url"]


def test_unknown_meal_is_an_error_not_an_exception(importer):
    result = importer.import_from_url("99999999")
    assert result.success is False
    assert "not found" in result.error.lower() or "99999999" in result.error


def test_search_random_and_categories(importer):
    hits = importer.search("teriyaki")
    assert [h["name"] for h in hits] and all(h["url"].startswith("https://www.themealdb.com/meal/") for h in hits)
    assert hits[0]["id"]
    assert importer.get_random_meal()["strMeal"] == "Teriyaki Chicken Casserole"
    categories = importer.get_categories()
    assert "Chicken" in categories and len(categories) > 5


def test_network_failure_degrades_to_empty_results():
    imp = TheMealDBImporter()

    class Down:
        def get(self, *a, **kw):
            raise ConnectionError("offline")

    imp.session = Down()
    assert imp.search("anything") == []
    assert imp.get_random_meal() is None
    assert imp.get_categories() == []
    assert imp.import_from_url("52772").success is False
