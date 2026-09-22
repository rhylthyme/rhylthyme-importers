"""Slide-deck importer, on a deck built in the test with python-pptx."""

import pytest
from pptx import Presentation

from conftest import assert_program_shape
from rhylthyme_importers import SlideDeckImporter


@pytest.fixture
def deck(tmp_path):
    prs = Presentation()
    layout = prs.slide_layouts[1]  # title and content
    for title, body, notes in (
        ("Welcome", "Agenda for today", "2 min"),
        ("The problem", "Schedules that people follow are hard to write.", ""),
        ("Demo", "Live timeline", "5 minutes"),
    ):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    path = tmp_path / "talk.pptx"
    prs.save(path)
    return path


def test_recognises_pptx_only():
    imp = SlideDeckImporter()
    assert imp.can_import("talk.pptx") and imp.can_import("/x/TALK.PPTX")
    assert not imp.can_import("talk.pdf")


def test_one_step_per_slide_timed_from_speaker_notes(deck):
    result = SlideDeckImporter().import_from_url(str(deck))
    assert result.success, result.error
    program = result.program
    assert_program_shape(program)
    steps = [s for t in program["tracks"] for s in t["steps"]]
    # Short-bodied slides are section headers; "Demo" is a trailing header
    # with nothing under it and used to be dropped, notes and all.
    assert [s["name"] for s in steps] == ["The problem", "Demo"]
    assert [t["name"] for t in program["tracks"]] == ["Welcome", "Demo"]
    by_name = {s["name"]: s["duration"] for s in steps}
    assert by_name["Demo"]["defaultSeconds"] == 300, "from the speaker notes"
    assert by_name["The problem"]["defaultSeconds"] > 0, "estimated from the text"


def test_a_plain_deck_is_one_track_of_timed_slides(tmp_path):
    prs = Presentation()
    for i in range(4):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = f"Slide {i + 1}"
        slide.placeholders[1].text = "Enough words on this slide that it does not read as a section header."
        slide.notes_slide.notes_text_frame.text = f"{i + 1} min"
    path = tmp_path / "plain.pptx"
    prs.save(path)
    program = SlideDeckImporter().import_from_url(str(path)).program
    assert_program_shape(program)
    assert len(program["tracks"]) == 1
    assert [s["duration"]["defaultSeconds"] for s in program["tracks"][0]["steps"]] == [60, 120, 180, 240]


def test_missing_file_is_an_error(tmp_path):
    result = SlideDeckImporter().import_from_url(str(tmp_path / "nope.pptx"))
    assert result.success is False
