"""
Doc-drift guards for the Opentrons importer.

These tests fail CI when:

- ``docs/opentrons.md`` is out of sync with ``DURATION_SECONDS``
  (i.e. someone added a command type to the dict without running
  ``make docs``).
- The fixture files under ``tests/fixtures/opentrons/`` aren't
  referenced as worked examples in the docs (so docs and tests
  can't drift apart silently).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rhylthyme_importers.opentrons import docs_gen
from rhylthyme_importers.opentrons.duration_model import DURATION_SECONDS


DOCS = Path(__file__).resolve().parent.parent / 'docs' / 'opentrons.md'
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'opentrons'


def test_duration_model_table_in_docs_matches_python_source():
    """Run the generator in --check mode; non-zero exit means drift."""
    exit_code = docs_gen.run(check_only=True)
    assert exit_code == 0, (
        'docs/opentrons.md is out of date relative to '
        'DURATION_SECONDS. Run `make docs` and commit the result.'
    )


def test_every_command_type_has_a_docs_note_entry():
    """Catch the case where a contributor adds to DURATION_SECONDS
    but forgets to add a hint in docs_gen._NOTES. The cell can be
    empty — what matters is that someone considered whether to
    document it."""
    for ct in DURATION_SECONDS:
        assert ct in docs_gen._NOTES, (
            f'{ct!r} is in DURATION_SECONDS but missing from '
            'docs_gen._NOTES. Add an entry (empty string OK if no '
            'hint is needed).'
        )


def test_each_fixture_protocol_appears_in_docs_examples():
    """Worked examples in the docs reuse the golden fixture files
    verbatim. CI fails when a fixture exists but isn't documented."""
    docs_text = DOCS.read_text(encoding='utf-8')
    for fixture in FIXTURES.glob('*.py'):
        # Either the fixture name OR its file basename should appear
        # in the user-facing docs so users can find the worked example.
        stem = fixture.stem
        assert stem in docs_text, (
            f'Fixture {fixture.name} is not referenced anywhere in '
            f'docs/opentrons.md. Add a "Worked examples" entry for it '
            'so the fixture and docs stay in sync.'
        )
