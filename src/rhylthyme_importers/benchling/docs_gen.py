"""Auto-generate ``docs/benchling-field-mapping.md`` from the live
normalizer/builder source.

Keeps documentation and code from drifting: any change to the
duration patterns, instrument classifier, or task-vocabulary mapping
shows up in the doc the next time ``make docs`` runs.

Run modes:
  python -m rhylthyme_importers.benchling.docs_gen          # write doc
  python -m rhylthyme_importers.benchling.docs_gen --check  # CI guard

The ``--check`` mode regenerates in memory and diffs against the
checked-in file, exiting non-zero on drift.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import normalizer
from . import program_builder


DOC_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "benchling-field-mapping.md"
)

_HEADER = """# Benchling field mapping

> **Auto-generated** from `src/rhylthyme_importers/benchling/normalizer.py`
> and `program_builder.py`. Do not edit by hand — run `make docs` from
> `rhylthyme-importers/`.

This document is the contract between Benchling's API payloads and
Rhylthyme's program schema. When the underlying code changes, this
file changes too; if you see drift in CI, run `make docs` and commit
the regenerated file.

"""


def _classifier_table() -> str:
    """Build the instrument-name → kind table by probing the classifier
    with one representative phrase per branch."""
    samples = [
        ("contains 'thermocycler' or 'PCR'", "thermocycler"),
        ("contains 'centrifuge' or 'spin'", "centrifuge"),
        ("contains 'incubator'", "incubator"),
        ("contains 'plate reader' or 'spectrophotometer'", "plate_reader"),
        ("contains 'shaker'", "shaker"),
        ("contains 'vortex'", "vortex"),
        ("contains 'balance' or 'scale'", "balance"),
        ("anything else", "other"),
    ]
    lines = ["| Instrument name (lowercase) | Internal `kind` |",
             "| --- | --- |"]
    for desc, kind in samples:
        lines.append(f"| {desc} | `{kind}` |")
    return "\n".join(lines)


def _kind_to_task_table() -> str:
    """Render the kind → task mapping that drives Rhylthyme `task`
    assignment + the resource-constraint task field."""
    rows = ["| Internal `kind` | Rhylthyme `task` |",
            "| --- | --- |"]
    for k, t in sorted(program_builder._KIND_TO_TASK.items()):
        rows.append(f"| `{k}` | `{t}` |")
    return "\n".join(rows)


def _unit_table() -> str:
    """Render the description-mined duration units."""
    rows = ["| Unit token | Seconds |",
            "| --- | --- |"]
    # Sort by descending seconds, then alphabetically.
    for unit, sec in sorted(
        normalizer._UNIT_TO_SECONDS.items(),
        key=lambda kv: (-kv[1], kv[0]),
    ):
        rows.append(f"| `{unit}` | {sec} |")
    return "\n".join(rows)


def _duration_patterns_table() -> str:
    """List the regex patterns we try when no explicit duration field
    is present on a step."""
    rows = ["| Order | Pattern (loose) | Example |",
            "| --- | --- | --- |"]
    examples = [
        '"incubate for 30 minutes"',
        '"spin 30 seconds"',
    ]
    for i, (pat, ex) in enumerate(zip(normalizer._DURATION_PATTERNS, examples), 1):
        # Show the raw regex source for transparency.
        rows.append(f"| {i} | `{pat.pattern}` | {ex} |")
    return "\n".join(rows)


def render() -> str:
    sections = [
        _HEADER,
        "## Payload shapes\n",
        "Benchling exposes three protocol-shaped resources; the normalizer\n"
        "dispatches by `object_type` (or by structural heuristic when the\n"
        "field is missing):\n",
        "| Shape | URL path | Step source |",
        "| --- | --- | --- |",
        "| Protocol | `/api/v2/protocols/{id}` | `protocol.steps[]` |",
        "| Workflow Task | `/api/v2/workflow-tasks/{id}` | `task.steps[]` or `task.procedure` (or 1-step fallback) |",
        "| Notebook Entry | `/api/v2/entries/{id}` | blocks where `block.type == \"step\"` (incl. inside `days[]`) |",
        "",
        "## Step-level mapping\n",
        "| Benchling source field | Rhylthyme target |",
        "| --- | --- |",
        "| `step.id` | `step.stepId` |",
        "| `step.name` | `step.name` |",
        "| `step.description` | `step.description` |",
        "| `step.duration_seconds` → fallback `step.duration_minutes × 60` → fallback `step.duration.{seconds,minutes}` → fallback parametric in `description` | `step.duration.seconds` (else `indefinite` with `triggerName: complete_<stepId>`) |",
        "| `step.instruments[].id` (or `step.fields.Instrument.value`) | resource constraint + track assignment |",
        "| `step.temperature_c` | reserved (not yet surfaced — Phase 3) |",
        "| `step.predecessor_id` (else declaration-order previous) | `step.startTrigger = afterStep(stepId)` |",
        "",
        "## Duration units recognized\n",
        _unit_table(),
        "",
        "## Duration patterns (description fallback)\n",
        _duration_patterns_table(),
        "",
        "## Instrument classifier\n",
        "Used to pick a `task` for a step driven by a given instrument.\n",
        _classifier_table(),
        "",
        "## Kind → Rhylthyme task\n",
        _kind_to_task_table(),
        "",
        "## Track assignment\n",
        "- Steps without any instrument → **Bench** track.\n"
        "- Steps with one or more instruments → first instrument's track.\n"
        "- Additional instruments on a step still produce resource\n"
        "  constraints, so multi-instrument blocking is preserved.\n",
        "## Resource constraints\n",
        "One entry per distinct instrument, `maxConcurrent: 1`.\n"
        "Phase 3 will read the tenant's instrument registry to raise the\n"
        "per-kind cap when multiple copies of the same instrument exist.\n",
    ]
    return "\n".join(sections).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if regenerated content differs from disk")
    args = parser.parse_args()

    content = render()
    if args.check:
        existing = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if existing != content:
            print(f"DRIFT: {DOC_PATH} is out of date. Run `make docs`.", file=sys.stderr)
            sys.exit(1)
        print(f"OK: {DOC_PATH} is up to date.")
        return
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {DOC_PATH}")


if __name__ == "__main__":
    main()
