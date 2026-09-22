"""Recipe-site importer. recipe-scrapers itself is not under test; a fake
scraper object stands in for it so no page is fetched."""

import pytest

from conftest import assert_program_shape
from rhylthyme_importers import ImporterRegistry, RecipeScrapersImporter


class FakeScraper:
    def title(self): return "Weeknight Chili"
    def description(self): return "A fast chili."
    def author(self): return "Test Kitchen"
    def site_name(self): return "Example Eats"
    def cuisine(self): return "American"
    def category(self): return "Dinner"
    def yields(self): return "4 servings"
    def image(self): return "https://example.org/chili.jpg"
    def total_time(self): return 45
    def cook_time(self): return 30
    def prep_time(self): return 15
    def ingredients(self): return ["1 lb ground beef", "1 onion", "2 cans beans"]
    def instructions_list(self): return ["Brown the beef.", "Add onion and cook 5 minutes.", "Add beans and simmer 30 minutes."]
    def instructions(self): return "\n".join(self.instructions_list())


@pytest.fixture
def importer(monkeypatch):
    import recipe_scrapers

    monkeypatch.setattr(recipe_scrapers, "scrape_me", lambda url, **kw: FakeScraper())
    monkeypatch.setattr(recipe_scrapers, "scrape_html", lambda html=None, org_url=None, **kw: FakeScraper())
    return RecipeScrapersImporter()


def test_registered_and_matches_supported_hosts_only():
    imp = ImporterRegistry.get("recipe-scrapers")
    assert isinstance(imp, RecipeScrapersImporter)
    assert imp.can_import("https://www.seriouseats.com/the-best-chili-recipe")
    assert imp.can_import("https://www.bbcgoodfood.com/recipes/classic-lasagne")
    assert not imp.can_import("https://example.org/recipe")
    assert not imp.can_import("not a url")


def test_import_from_url(importer):
    result = importer.import_from_url("https://www.seriouseats.com/weeknight-chili")
    assert result.success, result.error
    program = result.program
    assert_program_shape(program)
    assert program["name"] == "Weeknight Chili"
    steps = [s for t in program["tracks"] for s in t["steps"]]
    assert [s["name"] for s in steps][-1].startswith("Add beans")
    assert len(program["metadata"]["ingredients"]) == 3
    # A step that names its own time keeps it.
    simmer = next(s for s in steps if "simmer" in s["name"])
    assert simmer["duration"]["defaultSeconds"] == 30 * 60
    assert simmer["task"] == "stove-burner"
    assert program["metadata"]["source"]["url"] == "https://www.seriouseats.com/weeknight-chili"


def test_import_from_html(importer):
    result = importer.import_from_html("<html>ignored by the fake</html>", "https://www.seriouseats.com/x")
    assert result.success and result.program["name"] == "Weeknight Chili"


def test_scraper_failure_is_an_error_result(monkeypatch):
    import recipe_scrapers

    def boom(url, **kw):
        raise ValueError("no scraper for this site")

    monkeypatch.setattr(recipe_scrapers, "scrape_me", boom)
    result = RecipeScrapersImporter().import_from_url("https://www.seriouseats.com/x")
    assert result.success is False and "no scraper" in result.error
