"""Tracer-bullet tests for the Benchling importer.

Runs offline against the saved golden fixture; no live HTTP. Subsequent
phases will add fixtures for Workflow tasks, Notebook Entries, missing
durations, and ambiguous instruments.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rhylthyme_importers.benchling import BenchlingImporter

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "benchling"


def _strip_volatile(program):
    """Remove fields that change between runs (imported_at). Returns a
    deep-copied program with the field replaced by a sentinel so the
    rest of the structure can be compared against the golden file."""
    import copy
    out = copy.deepcopy(program)
    src = (out.get("metadata") or {}).get("source") or {}
    if "imported_at" in src:
        src["imported_at"] = "2026-05-18T00:00:00+00:00"
    return out


def test_pcr_protocol_matches_golden_fixture():
    """The PCR-setup fixture round-trips through the importer to a
    stable Rhylthyme program JSON. Golden-file comparison ensures any
    accidental change in normalize/build is caught."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "pcr-protocol.json"),
        tenant="acme",
    )
    assert result.success, result.error
    expected = json.loads((FIXTURE_DIR / "pcr-protocol.expected.json").read_text())
    assert _strip_volatile(result.program) == expected


def test_pcr_protocol_validates_against_schema():
    """The emitted program is schema-valid."""
    from rhylthyme import validate_program  # noqa: PLC0415  (import-time cost)

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "rhylthyme-spec"
        / "src"
        / "rhylthyme_spec"
        / "schemas"
        / "program_schema_0.2.0-alpha.json"
    )
    if not schema_path.exists():
        pytest.skip(f"schema not at {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "pcr-protocol.json"),
        tenant="acme",
    )
    assert result.success, result.error
    ok, errors = validate_program(result.program, schema)
    assert ok, "schema validation failed:\n" + "\n".join(errors)


def test_normalizer_extracts_durations_in_seconds():
    """``duration_minutes`` on a Benchling step lands as ``duration.seconds``
    on the emitted Rhylthyme step."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "pcr-protocol.json"),
        tenant="acme",
    )
    assert result.success
    # Flatten across tracks — Phase 2 splits by instrument so Mix lives
    # on Bench and Cycle on Thermocycler 1.
    steps_by_id = {
        s["stepId"]: s
        for t in result.program["tracks"]
        for s in t["steps"]
    }
    # "Mix PCR reagents" was 5 minutes in the fixture.
    assert steps_by_id["step_mix"]["duration"] == {"type": "fixed", "seconds": 300}
    # "Run thermocycler program" was 90 minutes.
    assert steps_by_id["step_cycle"]["duration"] == {"type": "fixed", "seconds": 5400}


def test_steps_chain_via_afterStep_triggers():
    """Phase 2 splits by instrument but still preserves declaration-order
    predecessor links: the first step starts at programStart, each
    subsequent step references its declaration-order predecessor via
    afterStep — even across tracks."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "pcr-protocol.json"),
        tenant="acme",
    )
    assert result.success
    # Walk steps in DECLARATION order (collect across tracks, then sort
    # by the order they appeared in the source).
    declaration_order = [
        "step_mix", "step_load", "step_cycle",
        "step_gel_prep", "step_load_gel", "step_image",
    ]
    steps_by_id = {
        s["stepId"]: s
        for t in result.program["tracks"]
        for s in t["steps"]
    }
    # First step starts at programStart.
    assert steps_by_id[declaration_order[0]]["startTrigger"] == {"type": "programStart"}
    # Each subsequent step references its declaration-order predecessor.
    for i in range(1, len(declaration_order)):
        trigger = steps_by_id[declaration_order[i]]["startTrigger"]
        assert trigger["type"] == "afterStep"
        assert trigger["stepId"] == declaration_order[i - 1]


def test_resource_constraints_deduplicate_instruments():
    """The thermocycler is referenced by two steps but should appear
    only once in ``resourceConstraints``."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "pcr-protocol.json"),
        tenant="acme",
    )
    assert result.success
    rcs = result.program["resourceConstraints"]
    # Expect 3 instruments: thermocycler, gel box, UV imager.
    names = sorted(rc["description"] for rc in rcs)
    assert names == ["Gel box — bench top", "Plate reader / UV imager", "Thermocycler 1"]
    # All maxConcurrent = 1 in Phase 1.
    assert all(rc["maxConcurrent"] == 1 for rc in rcs)


def test_metadata_carries_benchling_provenance():
    """Source URL is built from the tenant; benchlingProtocolId is preserved
    for round-tripping back to Benchling."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "pcr-protocol.json"),
        tenant="acme",
    )
    assert result.success
    src = result.program["metadata"]["source"]
    assert src["type"] == "benchling"
    assert src["tenant"] == "acme"
    assert src["benchlingProtocolId"] == "prot_pcr_demo_001"
    assert src["url"] == "https://acme.benchling.com/protocols/prot_pcr_demo_001"


def test_missing_duration_emits_indefinite_step():
    """A step with no duration_minutes and no parseable description
    hint becomes an indefinite step with a triggerName."""
    raw = {
        "id": "prot_x",
        "name": "Open-ended",
        "description": "",
        "steps": [
            {"id": "s1", "name": "Wait for results"},
        ],
    }
    importer = BenchlingImporter()
    result = importer.import_from_raw(raw, tenant="acme")
    assert result.success
    step = result.program["tracks"][0]["steps"][0]
    assert step["duration"]["type"] == "indefinite"
    assert step["duration"]["triggerName"] == "complete_s1"


def test_description_pattern_mines_duration():
    """``for N minutes`` in a description fills duration when no
    explicit field is present."""
    raw = {
        "id": "prot_x",
        "name": "Mined-duration",
        "description": "",
        "steps": [
            {"id": "s1", "name": "Wait", "description": "Let it rest for 12 minutes."},
        ],
    }
    importer = BenchlingImporter()
    result = importer.import_from_raw(raw, tenant="acme")
    assert result.success
    step = result.program["tracks"][0]["steps"][0]
    assert step["duration"] == {"type": "fixed", "seconds": 720}


def test_can_import_matches_benchling_urls_and_json():
    importer = BenchlingImporter()
    assert importer.can_import("https://acme.benchling.com/foo/protocols/abc")
    assert importer.can_import("/tmp/saved-protocol.json")
    assert not importer.can_import("https://example.com/recipe")


def test_live_url_import_without_token_returns_helpful_error():
    """Phase 4 onward: a URL import with no token tells the user they
    need to connect a Benchling account, rather than dropping a stack
    trace."""
    importer = BenchlingImporter()
    result = importer.import_from_url("https://acme.benchling.com/foo/protocols/abc")
    assert not result.success
    assert "connected account" in (result.error or "")


def test_detect_tenant_from_url():
    """The tenant-subdomain detector handles the common URL shapes
    and rejects non-Benchling hosts cleanly."""
    from rhylthyme_importers.benchling.importer import detect_tenant_from_url
    assert detect_tenant_from_url(
        "https://acme.benchling.com/acme/f/lib_xxx/protocols/prot_abc/edit"
    ) == "acme"
    assert detect_tenant_from_url("https://example.com/foo") is None
    assert detect_tenant_from_url("") is None
    assert detect_tenant_from_url(None) is None  # type: ignore[arg-type]


# ---------- Phase 2: multi-track + new shapes ------------------------

def test_pcr_protocol_splits_into_per_instrument_tracks():
    """The PCR fixture has 3 distinct instruments + bench steps, so we
    expect 4 tracks (Bench + 3 instruments) in the emitted program."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "pcr-protocol.json"),
        tenant="acme",
    )
    assert result.success
    track_names = [t["name"] for t in result.program["tracks"]]
    # Bench (no-instrument steps) + each distinct instrument.
    assert "Bench" in track_names
    assert "Thermocycler 1" in track_names
    assert "Gel box — bench top" in track_names
    assert "Plate reader / UV imager" in track_names
    assert len(track_names) == 4


def test_workflow_task_fixture_imports_to_valid_program():
    """Workflow-task-shaped JSON (object_type='workflow_task' + steps
    with fields.Instrument shape) imports without falling back to the
    single-step synthesis path."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "workflow-task-elisa.json"),
        tenant="acme",
    )
    assert result.success
    # ELISA fixture has 6 steps total; check they all made it in.
    total_steps = sum(len(t["steps"]) for t in result.program["tracks"])
    assert total_steps == 6
    # Source URL should point at /workflow-tasks/, not /protocols/.
    src_url = result.program["metadata"]["source"]["url"]
    assert "/workflow-tasks/" in src_url
    # Fields.Instrument from the coat step is picked up.
    track_names = [t["name"] for t in result.program["tracks"]]
    assert "Cold incubator" in track_names
    assert "Plate washer" in track_names
    assert "Orbital shaker" in track_names
    assert "Plate reader" in track_names


def test_notebook_entry_fixture_skips_text_blocks():
    """Notebook entries contain non-step blocks (text/table/figure).
    Only step blocks should produce Rhylthyme steps."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "notebook-entry-blot.json"),
        tenant="acme",
    )
    assert result.success
    total_steps = sum(len(t["steps"]) for t in result.program["tracks"])
    # 6 step blocks in the fixture (2 text blocks ignored).
    assert total_steps == 6
    # Source URL should point at /entries/.
    src_url = result.program["metadata"]["source"]["url"]
    assert "/entries/" in src_url


def test_missing_durations_fixture_mixes_strategies():
    """The missing-durations fixture exercises all three duration paths:
    explicit, parametric-in-description, and unparseable → indefinite."""
    importer = BenchlingImporter()
    result = importer.import_from_file(
        str(FIXTURE_DIR / "missing-durations.json"),
        tenant="acme",
    )
    assert result.success
    steps_by_id = {
        s["stepId"]: s
        for t in result.program["tracks"]
        for s in t["steps"]
    }
    # No info anywhere → indefinite.
    assert steps_by_id["s1"]["duration"]["type"] == "indefinite"
    # "Incubate ... for 45 minutes" → fixed 45 minutes.
    assert steps_by_id["s2"]["duration"] == {"type": "fixed", "seconds": 2700}
    # "Spin 30 seconds" (verb-prefixed pattern) → fixed 30s.
    assert steps_by_id["s3"]["duration"] == {"type": "fixed", "seconds": 30}
    # "Wait for the result; varies" → indefinite.
    assert steps_by_id["s4"]["duration"]["type"] == "indefinite"
    # Explicit duration_minutes → fixed.
    assert steps_by_id["s5"]["duration"] == {"type": "fixed", "seconds": 300}


def test_shape_detection_falls_back_when_object_type_missing():
    """``object_type`` is preferred but optional. The detector falls
    back to shape heuristics (presence of ``blocks`` / ``days``
    indicates an entry; ``procedure`` indicates a workflow task)."""
    importer = BenchlingImporter()
    # Entry with no object_type.
    entry_raw = {
        "id": "ent_no_type",
        "name": "Entry without object_type",
        "days": [{"blocks": [{"type": "step", "id": "s1", "name": "Do thing"}]}],
    }
    result = importer.import_from_raw(entry_raw, tenant="acme")
    assert result.success
    assert "/entries/" in result.program["metadata"]["source"]["url"]


def test_all_phase_2_fixtures_validate_against_schema():
    """Schema validation across all four fixtures keeps regressions
    from slipping past the more permissive golden-file tests."""
    from rhylthyme import validate_program  # noqa: PLC0415

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "rhylthyme-spec"
        / "src"
        / "rhylthyme_spec"
        / "schemas"
        / "program_schema_0.2.0-alpha.json"
    )
    if not schema_path.exists():
        pytest.skip(f"schema not at {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    importer = BenchlingImporter()
    for fname in [
        "pcr-protocol.json",
        "workflow-task-elisa.json",
        "notebook-entry-blot.json",
        "missing-durations.json",
    ]:
        result = importer.import_from_file(
            str(FIXTURE_DIR / fname),
            tenant="acme",
        )
        assert result.success, f"{fname}: {result.error}"
        ok, errors = validate_program(result.program, schema)
        assert ok, f"{fname} schema invalid:\n" + "\n".join(errors)
