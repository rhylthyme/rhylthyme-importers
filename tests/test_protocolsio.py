"""
Unit tests for ProtocolsIOImporter.

Tests exercise _parse_materials and _convert_to_program directly against
raw protocol_data dicts (the shape returned by the protocols.io API's
`protocol` field) — no network calls are made.
"""

from unittest.mock import patch

import pytest
from rhylthyme_importers.protocolsio import ProtocolsIOImporter, _truncate_step_title


@pytest.fixture
def importer():
    return ProtocolsIOImporter(access_token="test-token")


# ---------------------------------------------------------------------------
# _parse_materials
# ---------------------------------------------------------------------------

class TestParseMaterials:
    def test_parses_materials_text_html_list(self, importer):
        """The real, common case: protocols.io almost always carries
        materials as an HTML bullet list in materials_text rather than the
        structured materials array (confirmed against a live protocol)."""
        protocol_data = {
            "materials": [],
            "materials_text": (
                "<ul><li>Trizol</li><li>100% ethanol (new every time)</li>"
                "<li>RNase free water (new every time)</li></ul>"
            ),
        }
        materials = importer._parse_materials(protocol_data)
        assert materials == [
            {"name": "Trizol", "measure": ""},
            {"name": "100% ethanol (new every time)", "measure": ""},
            {"name": "RNase free water (new every time)", "measure": ""},
        ]

    def test_prefers_structured_materials_over_text(self, importer):
        protocol_data = {
            "materials": [{"name": "Trizol"}, {"title": "Ethanol"}],
            "materials_text": "<ul><li>Should be ignored</li></ul>",
        }
        materials = importer._parse_materials(protocol_data)
        assert materials == [
            {"name": "Trizol", "measure": ""},
            {"name": "Ethanol", "measure": ""},
        ]

    def test_deduplicates(self, importer):
        protocol_data = {
            "materials_text": "<ul><li>Trizol</li><li>Trizol</li></ul>",
        }
        materials = importer._parse_materials(protocol_data)
        assert materials == [{"name": "Trizol", "measure": ""}]

    def test_no_materials_present(self, importer):
        assert importer._parse_materials({}) == []
        assert importer._parse_materials({"materials_text": ""}) == []

    def test_parses_component_reagent_spans_in_materials_text(self, importer):
        """Some protocols format materials_text as tagged reagent
        paragraphs rather than an <li> bullet list (confirmed against a
        live protocol, id 895: "<p>STEP MATERIALS</p><p>
        <span class="component-reagent">Anti-Phosphoserine Antibody...
        </span></p>...")."""
        protocol_data = {
            "materials_text": (
                '<p>STEP MATERIALS</p>'
                '<p> <span class="component-reagent">Trizol</span> </p>'
                '<p> <span class="component-reagent">100% ethanol</span> </p>'
            ),
        }
        materials = importer._parse_materials(protocol_data)
        assert materials == [
            {"name": "Trizol", "measure": ""},
            {"name": "100% ethanol", "measure": ""},
        ]

    def test_parses_dash_prefixed_paragraph_list(self, importer):
        """Some protocols format materials_text as one dash-prefixed
        paragraph per item (confirmed against a live protocol, id
        318778): "<p>- HSM</p><p>- CAR T PE (BD/624255)</p>..."."""
        protocol_data = {
            "materials_text": "<p>Reagent/Supplies:</p><p>- HSM</p><p>- CAR T PE (BD/624255)</p>",
        }
        materials = importer._parse_materials(protocol_data)
        assert materials == [
            {"name": "HSM", "measure": ""},
            {"name": "CAR T PE (BD/624255)", "measure": ""},
        ]

    def test_parses_bare_paragraph_list_no_bullet(self, importer):
        """Some protocols format materials_text as one bare paragraph per
        item with no marker at all (confirmed against a live protocol,
        id 107710): "<p>AMPure XP beads</p><p>DNA LoBind Tubes...</p>"."""
        protocol_data = {
            "materials_text": "<p>AMPure XP beads</p><p>DNA LoBind Tubes (1.5 ml)</p>",
        }
        materials = importer._parse_materials(protocol_data)
        assert materials == [
            {"name": "AMPure XP beads", "measure": ""},
            {"name": "DNA LoBind Tubes (1.5 ml)", "measure": ""},
        ]

    def test_skips_table_captions_in_paragraph_fallback(self, importer):
        """Regression: some protocols format materials_text as an actual
        embedded <table> with captions like "Table 1: Specifications of
        the equipment" (confirmed against a live protocol, id 321704) —
        without this filter, the caption text gets treated as if it were
        itself a material name."""
        protocol_data = {
            "materials_text": (
                "<p>Table 1: Specifications of the equipment</p>"
                "<p>Table 2: Specification of reagents required</p>"
            ),
        }
        assert importer._parse_materials(protocol_data) == []

    def test_aggregates_component_reagent_tags_across_steps_as_last_resort(self, importer):
        """Many real protocols have NO materials_text at all but DO
        inline-tag reagents within individual step text as the author
        writes each step (confirmed against a live protocol, id 88439 —
        empty materials_text, 17+ tagged reagent mentions across steps).
        Aggregating these is what turns such a protocol from a single-row
        generic-mode fold view into a real, multi-row one."""
        protocol_data = {
            "materials_text": "",
            "steps": [
                {"step": '<p>Add <span class="component-reagent">Trizol</span> to tube.</p>'},
                {"step": '<p>Wash with <span class="component-reagent">100% ethanol</span>.</p>'},
                {"step": '<p>Add <span class="component-reagent">Trizol</span> again.</p>'},
            ],
        }
        materials = importer._parse_materials(protocol_data)
        assert materials == [
            {"name": "Trizol", "measure": ""},
            {"name": "100% ethanol", "measure": ""},
        ]


# ---------------------------------------------------------------------------
# _extract_component_spans / mentionedIngredients
# ---------------------------------------------------------------------------

class TestExtractComponentSpans:
    def test_extracts_and_cleans_spans(self, importer):
        html_text = (
            '<p>Run gel with <span class="component-reagent">Anti-Phosphoserine '
            'Antibody &amp; Buffer</span> for QC.</p>'
        )
        assert importer._extract_component_spans(html_text, "reagent") == [
            "Anti-Phosphoserine Antibody & Buffer",
        ]

    def test_no_spans_returns_empty(self, importer):
        assert importer._extract_component_spans("<p>Plain text, no tags.</p>", "reagent") == []
        assert importer._extract_component_spans("", "reagent") == []

    def test_step_entry_gets_mentioned_ingredients_metadata(self, importer):
        protocol_data = {
            "title": "Test protocol",
            "steps": [
                {
                    "number": 1,
                    "step": '<p>Add <span class="component-reagent">Trizol</span> now.</p>',
                },
                {"number": 2, "step": "<p>Wait.</p>"},
            ],
        }
        program = importer._convert_to_program(protocol_data, "some-id")
        steps = program["tracks"][0]["steps"]
        assert steps[0]["metadata"]["mentionedIngredients"] == ["Trizol"]
        assert "metadata" not in steps[1]


# ---------------------------------------------------------------------------
# _convert_to_program — materials feed metadata.ingredients
# ---------------------------------------------------------------------------

class TestConvertToProgram:
    def test_materials_become_metadata_ingredients(self, importer):
        """metadata.ingredients is the exact field program_to_fold's
        recipe mode already reads — reusing it means lab protocols get a
        working fold view (rows = materials, brackets = steps using them)
        with no separate lab-specific code path."""
        protocol_data = {
            "title": "RNA Extraction",
            "steps": [
                {
                    "number": 1,
                    "step": "<p>Add 770 uL of Trizol to the tube.</p>",
                    "section": "",
                },
            ],
            "materials_text": "<ul><li>Trizol</li><li>RNase free water</li></ul>",
        }
        program = importer._convert_to_program(protocol_data, "bc76izre")
        assert program["metadata"]["ingredients"] == [
            {"name": "Trizol", "measure": ""},
            {"name": "RNase free water", "measure": ""},
        ]

    def test_steps_sorted_by_number_not_array_position(self, importer):
        """A real protocol (protocols.io id 88439) was found with its
        `steps` array in the order 5, 9, 8, 7, 6, 1, 4, 3, 2, 10 — each
        step's own "number" field is the true intended sequence; array
        position reflects something else entirely (apparently edit
        history). Building the afterStep chain straight from array order
        scrambled the whole protocol into a nonsensical fold view."""
        protocol_data = {
            "title": "Scrambled protocol",
            "steps": [
                {"number": "5", "step": "Fifth", "section": ""},
                {"number": "2", "step": "Second", "section": ""},
                {"number": "1", "step": "First", "section": ""},
                {"number": "4", "step": "Fourth", "section": ""},
                {"number": "3", "step": "Third", "section": ""},
            ],
        }
        program = importer._convert_to_program(protocol_data, "some-id")
        descriptions = [s["description"] for s in program["tracks"][0]["steps"]]
        assert descriptions == ["First", "Second", "Third", "Fourth", "Fifth"]

    def test_substeps_excluded_from_sort_and_chain(self, importer):
        protocol_data = {
            "title": "Protocol with substeps",
            "steps": [
                {"number": "1", "step": "First", "section": "", "is_substep": False},
                {"number": "1.1", "step": "First sub", "section": "", "is_substep": True},
                {"number": "2", "step": "Second", "section": "", "is_substep": False},
            ],
        }
        program = importer._convert_to_program(protocol_data, "some-id")
        descriptions = [s["description"] for s in program["tracks"][0]["steps"]]
        assert descriptions == ["First", "Second"]

    def test_unparseable_number_sorts_last_not_crashes(self, importer):
        protocol_data = {
            "title": "Protocol with a weird number",
            "steps": [
                {"number": "2", "step": "Second", "section": ""},
                {"number": None, "step": "Unknown position", "section": ""},
                {"number": "1", "step": "First", "section": ""},
            ],
        }
        program = importer._convert_to_program(protocol_data, "some-id")
        descriptions = [s["description"] for s in program["tracks"][0]["steps"]]
        assert descriptions == ["First", "Second", "Unknown position"]

    def test_no_materials_key_omitted_when_empty(self, importer):
        protocol_data = {"title": "Empty protocol", "steps": []}
        program = importer._convert_to_program(protocol_data, "some-id")
        assert "ingredients" not in program["metadata"]

    def test_title_html_entities_decoded(self, importer):
        """A real protocol titled '2&times;CTAB Protocol...' must render
        as '2×CTAB Protocol...', not leak the raw HTML entity."""
        protocol_data = {"title": "2&times;CTAB Protocol", "steps": []}
        program = importer._convert_to_program(protocol_data, "some-id")
        assert program["name"] == "2×CTAB Protocol"

    def test_null_steps_falls_back_to_placeholder_not_a_crash(self, importer):
        """protocols.io returns `steps` present but explicitly null for a
        protocol whose real content is an uploaded .docx with no
        structured steps at all (confirmed against a live protocol,
        id 99806) — `.get("steps", [])` only substitutes its default for
        a MISSING key, not an explicit None value, so this crashed the
        whole import with 'NoneType object is not iterable' instead of
        reaching the existing placeholder-step fallback."""
        protocol_data = {"title": "Docx-only protocol", "steps": None}
        program = importer._convert_to_program(protocol_data, "some-id")
        assert len(program["tracks"][0]["steps"]) == 1
        assert program["tracks"][0]["steps"][0]["stepId"] == "step_01"

    def test_null_authors_and_creator_do_not_crash(self, importer):
        protocol_data = {"title": "Protocol", "steps": [], "authors": None, "creator": None}
        program = importer._convert_to_program(protocol_data, "some-id")
        assert "authors" not in program["metadata"]


# ---------------------------------------------------------------------------
# _extract_duration
# ---------------------------------------------------------------------------

class TestExtractDuration:
    def test_matches_minutes(self, importer):
        assert importer._extract_duration("Incubate for 5 minutes", {}) == 300

    def test_matches_hours(self, importer):
        assert importer._extract_duration("Block for 2hrs at RT", {}) == 7200

    def test_does_not_match_molarity_as_minutes(self, importer):
        """Regression: 'Add 50mM Tris buffer' was read as '50 minutes'
        (3000s) — the digits+unit-letter pattern matched "50m" with no
        word-boundary check, so it didn't notice "M" immediately follows.
        Same bug class as "10mL" -> "10 minutes". This is an extremely
        common false-positive shape in real wetlab protocol text."""
        assert importer._extract_duration("Add 50mM Tris buffer", {}) == 300  # falls to default, not 3000

    def test_does_not_match_milliliters_as_minutes(self, importer):
        assert importer._extract_duration("Add 10mL of 1x TBST", {}) == 300  # falls to default, not 600

    def test_does_not_match_dilution_ratio_as_mmss(self, importer):
        """Regression: 'Probe with antibody at 1:2000 dilution' — an
        extremely common phrasing in western blot / IHC protocols — was
        read as a 1min:2000sec duration (2060s, clamped nowhere near
        reasonable). The MM:SS pattern now requires the seconds group to
        be a valid 00-59 range with hard boundaries."""
        assert importer._extract_duration("Probe with antibody at 1:2000 dilution", {}) == 300

    def test_matches_genuine_mmss_duration(self, importer):
        assert importer._extract_duration("Wait 1:30 before proceeding", {}) == 90

    def test_component_duration_tags_take_priority_and_sum(self, importer):
        """protocols.io's own inline component-duration tags (author-
        entered HH:MM:SS) are authoritative, not a guess — and a step can
        have several distinct timed sub-actions each tagged separately
        (confirmed against a live protocol, id 895, step with five
        separate component-duration tags). These should be preferred
        over — and summed, not just first-matched like — the regex
        text-parsing fallback."""
        step_data = {
            "step": (
                '<p>Wash <span class="component-duration">00:05:00</span> '
                'then spin <span class="component-duration">00:02:00</span>.</p>'
            ),
        }
        # Text alone would match "5" as the first duration pattern hit;
        # the tags (5min + 2min = 7min = 420s) must win instead.
        assert importer._extract_duration("Wash 5 minutes then spin", step_data) == 420

    def test_falls_back_to_text_when_no_component_duration_tags(self, importer):
        step_data = {"step": "<p>Incubate for 5 minutes.</p>"}
        assert importer._extract_duration("Incubate for 5 minutes", step_data) == 300


# ---------------------------------------------------------------------------
# _truncate_step_title
# ---------------------------------------------------------------------------

class TestTruncateStepTitle:
    def test_short_title_unchanged(self):
        assert _truncate_step_title("Step 1: Mix reagents") == "Step 1: Mix reagents"

    def test_long_title_word_boundary_truncated(self):
        title = "Step 1: 1. Preparation of Allelic Entry-Clone Pools"
        result = _truncate_step_title(title)
        assert len(result) <= 46  # 45 + ellipsis
        assert result.endswith("…")
        assert title.startswith(result[:-1])

    def test_does_not_stop_at_embedded_list_number_period(self):
        """Regression: BaseImporter.make_step_name() (designed for
        instructional prose) was previously used here and stopped at the
        FIRST period — which for a numbered outline title like
        "Step 1: 1. Preparation of Allelic Entry-Clone Pools" is the
        period right after the embedded list number, mangling the whole
        title down to just "Step 1: 1"."""
        title = "Step 1: 1. Preparation of Allelic Entry-Clone Pools"
        result = _truncate_step_title(title)
        assert result != "Step 1: 1"
        assert "Preparation" in result


# ---------------------------------------------------------------------------
# get_random_protocol
# ---------------------------------------------------------------------------

class TestGetRandomProtocol:
    def test_picks_a_result_from_search(self, importer):
        """protocols.io has no dedicated random endpoint (confirmed
        against the live API), so this simulates it via search — verify
        it delegates to search() and returns one of its results."""
        fake_results = [
            {"name": "Protocol A", "id": 1, "url": "...", "description": "", "author": ""},
            {"name": "Protocol B", "id": 2, "url": "...", "description": "", "author": ""},
        ]
        with patch.object(importer, "search", return_value=fake_results) as mock_search:
            picked = importer.get_random_protocol()
        assert mock_search.called
        assert picked in fake_results

    def test_returns_none_when_search_finds_nothing(self, importer):
        with patch.object(importer, "search", return_value=[]):
            assert importer.get_random_protocol() is None
