"""rhylthyme-import, driven through main() with the network faked."""

import json
import sys

import pytest

from conftest import FakeSession, load_fixture
from rhylthyme_importers import ImporterRegistry, cli


@pytest.fixture
def offline_mealdb(monkeypatch):
    imp = ImporterRegistry.get("themealdb")
    session = FakeSession({
        "lookup.php": load_fixture("themealdb", "lookup_52772.json"),
        "search.php": load_fixture("themealdb", "search_teriyaki.json"),
        "random.php": load_fixture("themealdb", "lookup_52772.json"),
        "categories.php": load_fixture("themealdb", "categories.json"),
    })
    monkeypatch.setattr(imp, "session", session)
    return session


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["rhylthyme-import", *argv])
    try:
        cli.main()
    except SystemExit as stop:
        return stop.code or 0
    return 0


def test_list_names_every_registered_importer(monkeypatch, capsys):
    assert run(monkeypatch, "list") == 0
    out = capsys.readouterr().out
    for name in ("themealdb", "spoonacular", "protocolsio", "cooklang", "opentrons", "recipe-scrapers"):
        assert name in out, name


def test_import_writes_a_program_file(monkeypatch, capsys, tmp_path, offline_mealdb):
    out = tmp_path / "curry.json"
    assert run(monkeypatch, "import", "https://www.themealdb.com/meal/52772", "-o", str(out), "--pretty") == 0
    program = json.loads(out.read_text())
    assert program["name"] == "Teriyaki Chicken Casserole"
    assert "themealdb" in capsys.readouterr().err, "progress goes to stderr; stdout is for the program"


def test_import_to_stdout_and_explicit_importer(monkeypatch, capsys, offline_mealdb):
    assert run(monkeypatch, "import", "52772", "-i", "themealdb") == 0
    out = capsys.readouterr().out
    assert '"programId"' in out


def test_search(monkeypatch, capsys, offline_mealdb):
    assert run(monkeypatch, "search", "teriyaki", "-i", "themealdb") == 0
    assert "Teriyaki" in capsys.readouterr().out


def test_mealdb_random_and_categories(monkeypatch, capsys, tmp_path, offline_mealdb):
    out = tmp_path / "r.json"
    assert run(monkeypatch, "mealdb", "random", "-o", str(out)) == 0
    assert json.loads(out.read_text())["name"]
    assert run(monkeypatch, "mealdb", "categories") == 0
    assert "Chicken" in capsys.readouterr().out


def test_unknown_importer_and_unimportable_url_fail(monkeypatch, capsys):
    assert run(monkeypatch, "search", "x", "-i", "no-such-importer") != 0
    assert run(monkeypatch, "import", "https://example.org/nothing-matches-this") != 0
