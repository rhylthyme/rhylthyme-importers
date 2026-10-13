"""Ontime rundowns: project files (v4 and v3), spreadsheets, pasted tables."""

import io
import json
import zipfile
from xml.sax.saxutils import escape
from pathlib import Path

import pytest

from rhylthyme_importers.base import ImporterRegistry
from rhylthyme_importers.ontime import (
    OntimeImporter,
    google_sheet_csv_url,
    looks_like_ontime_project,
    parse_time,
)

from conftest import FIXTURES, assert_program_shape

SCHEMA = (Path(__file__).resolve().parents[2] / 'rhylthyme-spec' / 'src' / 'rhylthyme_spec'
          / 'schemas' / 'program_schema_0.3.0-alpha.json')
EXAMPLE = FIXTURES / 'ontime' / 'product-launch.csv'
H, M = 3600, 60


@pytest.fixture
def importer():
    return OntimeImporter(allow_local_files=True)


def steps_by_id(program):
    return {s['stepId']: (t, s) for t in program['tracks'] for s in t['steps']}


def starts(program):
    """Show time each step starts, following the triggers."""
    by_id = {s['stepId']: s for t in program['tracks'] for s in t['steps']}

    def start(step):
        trigger = step['startTrigger']
        if trigger['type'] == 'afterStep':
            ref = by_id[trigger['stepId']]
            return start(ref) + ref['duration']['seconds']
        return trigger.get('offsetSeconds', 0)

    return {sid: start(s) for sid, s in by_id.items()}


def assert_valid(program, tmp_path):
    assert_program_shape(program)
    if not SCHEMA.exists():
        pytest.skip('rhylthyme-spec is not checked out next to this repo')
    from rhylthyme import validate_program_file

    path = tmp_path / 'program.json'
    path.write_text(json.dumps(program))
    assert validate_program_file(str(path), schema_file=str(SCHEMA))


def v4_event(eid, title, start, end, parent=None, **extra):
    event = {
        'type': 'event', 'id': eid, 'cue': extra.pop('cue', ''), 'title': title, 'note': '',
        'endAction': 'none', 'timerType': 'count-down', 'countToEnd': False, 'linkStart': False,
        'timeStrategy': 'lock-duration', 'timeStart': start * 1000, 'timeEnd': end * 1000,
        'duration': ((end - start) % 86400) * 1000, 'skip': False, 'colour': '', 'parent': parent,
        'custom': {}, 'revision': 0, 'delay': 0, 'dayOffset': 0, 'gap': 0,
        'timeWarning': 120000, 'timeDanger': 60000,
    }
    event.update(extra)
    return event


def v4_project(entries, order, title='Main stage', project_title='Spring Gala', extra_rundowns=None):
    flat = []
    for eid in order:
        flat.append(eid)
        if entries[eid]['type'] == 'group':
            flat.extend(entries[eid]['entries'])
    rundowns = {'default': {'id': 'default', 'title': title, 'order': order, 'flatOrder': flat,
                            'entries': entries, 'revision': 0}}
    rundowns.update(extra_rundowns or {})
    return {
        'rundowns': rundowns,
        'project': {'title': project_title, 'description': ''},
        'settings': {'app': 'ontime', 'version': '4.2.0'},
        'customFields': {'presenter': {'label': 'Presenter', 'type': 'text', 'colour': ''}},
    }


class TestTheExample:
    """tests/fixtures/ontime/product-launch.csv, the example in the docs."""

    def test_groups_become_tracks_and_times_survive(self, importer, tmp_path):
        result = importer.import_from_url(str(EXAMPLE))
        assert result.success, result.error
        program = result.program
        assert program['name'] == 'Product launch'
        assert program['environmentType'] == 'event'
        assert [t['name'] for t in program['tracks']] == ['Doors', 'Keynote', 'Rundown']
        at = starts(program)
        assert at == {
            '1-doors-open': 0,
            '2-welcome': 30 * M,
            '3-product-demo': 35 * M,
            '4-audience-q-a': 65 * M,
            '5-reception': 90 * M,  # 19:30, after a ten-minute gap
        }
        assert program['metadata']['ontime']['showStart'] == '18:00:00'
        assert program['description'] == (
            'Run of show imported from Ontime: 5 events and 1 marker, 18:00 to 21:00.')
        assert_valid(program, tmp_path)

    def test_the_gap_is_kept_as_a_clock_time(self, importer):
        program = importer.import_from_url(str(EXAMPLE)).program
        _, reception = steps_by_id(program)['5-reception']
        assert reception['startTrigger'] == {'type': 'programStartOffset', 'offsetSeconds': 90 * M}
        assert reception['metadata']['ontime']['timeStart'] == '19:30:00'

    def test_the_milestone_is_an_alert_on_the_next_event(self, importer):
        program = importer.import_from_url(str(EXAMPLE)).program
        _, qa = steps_by_id(program)['4-audience-q-a']
        assert qa['alerts'] == [{'event': 'start', 'message': 'Mics live in the house'}]
        assert qa['metadata']['ontime']['markers'][0]['kind'] == 'milestone'

    def test_cues_notes_and_custom_columns_are_kept(self, importer):
        program = importer.import_from_url(str(EXAMPLE)).program
        _, demo = steps_by_id(program)['3-product-demo']
        assert demo['name'] == 'Product demo'
        assert demo['description'] == 'Two clickers on stage'
        assert demo['metadata']['ontime']['cue'] == '3'
        assert demo['metadata']['ontime']['custom'] == {'presenter': 'CEO'}


class TestProjectFiles:
    def test_v4_project(self, importer, tmp_path):
        entries = {
            'g1': {'type': 'group', 'id': 'g1', 'title': 'Act one', 'note': '', 'entries': ['e1', 'e2'],
                   'targetDuration': None, 'colour': '', 'custom': {}, 'revision': 0},
            'e1': v4_event('e1', 'Overture', 19 * H, 19 * H + 10 * M, 'g1', cue='1',
                           custom={'presenter': 'Orchestra'}),
            'e2': v4_event('e2', 'Scene one', 19 * H + 10 * M, 19 * H + 40 * M, 'g1', cue='2', linkStart=True),
            'e3': v4_event('e3', 'Interval', 19 * H + 40 * M, 20 * H, cue='3', linkStart=True),
        }
        result = importer.import_from_source(json.dumps(v4_project(entries, ['g1', 'e3'])))
        assert result.success, result.error
        program = result.program
        assert program['name'] == 'Spring Gala: Main stage'
        assert [t['name'] for t in program['tracks']] == ['Act one', 'Rundown']
        assert program['tracks'][0]['metadata'] == {'ontime': {'groupId': 'g1'}}
        assert starts(program) == {'1-overture': 0, '2-scene-one': 10 * M, '3-interval': 40 * M}
        _, overture = steps_by_id(program)['1-overture']
        # custom fields are listed under their labels
        assert overture['metadata']['ontime']['custom'] == {'Presenter': 'Orchestra'}
        assert overture['metadata']['ontime']['id'] == 'e1'
        assert program['metadata']['ontime']['ontimeVersion'] == '4.2.0'
        assert_valid(program, tmp_path)

    def test_skipped_events_are_left_out_and_delays_ignored(self, importer):
        entries = {
            'e1': v4_event('e1', 'Welcome', 9 * H, 9 * H + 15 * M),
            'd1': {'type': 'delay', 'id': 'd1', 'duration': 5 * M * 1000, 'parent': None},
            'e2': v4_event('e2', 'Rehearsed bit', 9 * H + 15 * M, 9 * H + 30 * M, skip=True),
            'e3': v4_event('e3', 'Keynote', 9 * H + 30 * M, 10 * H + 30 * M),
        }
        program = importer.import_from_source(json.dumps(v4_project(entries, ['e1', 'd1', 'e2', 'e3']))).program
        assert set(steps_by_id(program)) == {'welcome', 'keynote'}
        assert starts(program)['keynote'] == 30 * M
        assert program['metadata']['ontime']['skippedEvents'] == ['Rehearsed bit']
        assert program['metadata']['ontime']['delaysIgnored'] == 1

    def test_a_show_through_midnight(self, importer, tmp_path):
        entries = {
            'e1': v4_event('e1', 'Headliner', 23 * H, 23 * H + 45 * M),
            'e2': v4_event('e2', 'Encore', 23 * H + 50 * M, 10 * M),  # ends 00:10
            'e3': v4_event('e3', 'House lights', 15 * M, 30 * M),
        }
        program = importer.import_from_source(json.dumps(v4_project(entries, ['e1', 'e2', 'e3']))).program
        assert starts(program) == {'headliner': 0, 'encore': 50 * M, 'house-lights': 75 * M}
        _, encore = steps_by_id(program)['encore']
        assert encore['duration']['seconds'] == 20 * M
        assert encore['metadata']['ontime']['timeEnd'] == '00:10:00'
        assert_valid(program, tmp_path)

    def test_overlapping_events_get_a_track_of_their_own(self, importer, tmp_path):
        entries = {
            'e1': v4_event('e1', 'Panel', 14 * H, 15 * H),
            'e2': v4_event('e2', 'Press call', 14 * H + 30 * M, 14 * H + 45 * M),
            'e3': v4_event('e3', 'Coffee', 15 * H, 15 * H + 30 * M),
        }
        program = importer.import_from_source(json.dumps(v4_project(entries, ['e1', 'e2', 'e3']))).program
        assert [t['name'] for t in program['tracks']] == ['Main stage', 'Main stage (overlap)']
        assert starts(program) == {'panel': 0, 'press-call': 30 * M, 'coffee': 60 * M}
        assert_valid(program, tmp_path)

    def test_pick_a_rundown_by_id_or_title(self, importer):
        other = {'id': 'rehearsal', 'title': 'Rehearsal', 'order': ['r1'], 'flatOrder': ['r1'], 'revision': 0,
                 'entries': {'r1': v4_event('r1', 'Line check', 8 * H, 9 * H)}}
        data = v4_project({'e1': v4_event('e1', 'Show', 20 * H, 21 * H)}, ['e1'],
                          extra_rundowns={'rehearsal': other})
        first = importer.import_from_source(json.dumps(data)).program
        assert first['metadata']['ontime']['otherRundowns'] == [{'id': 'rehearsal', 'title': 'Rehearsal'}]
        for pick in ('rehearsal', 'Rehearsal'):
            program = importer.import_from_source(json.dumps(data), rundown=pick).program
            assert list(steps_by_id(program)) == ['line-check']
        missing = importer.import_from_source(json.dumps(data), rundown='nope')
        assert not missing.success and 'rehearsal (Rehearsal)' in missing.error

    def test_v3_project_with_blocks(self, importer, tmp_path):
        data = {
            'settings': {'app': 'ontime', 'version': '3.8.0'},
            'rundown': [
                {'type': 'block', 'id': 'b1', 'title': 'Morning'},
                {'type': 'event', 'id': 'a', 'cue': '1', 'title': 'Registration', 'timeStart': 8 * H * 1000,
                 'timeEnd': 9 * H * 1000, 'duration': H * 1000, 'timerType': 'count-to-end', 'custom': {}},
                {'type': 'delay', 'id': 'd', 'duration': 600000},
                {'type': 'block', 'id': 'b2', 'title': 'Talks'},
                {'type': 'event', 'id': 'b', 'cue': '2', 'title': 'Opening talk', 'timeStart': 9 * H * 1000,
                 'timeEnd': 10 * H * 1000, 'duration': H * 1000, 'linkStart': True, 'custom': {}},
            ],
        }
        assert looks_like_ontime_project(data)
        program = importer.import_from_source(json.dumps(data)).program
        assert [t['name'] for t in program['tracks']] == ['Morning', 'Talks']
        _, reg = steps_by_id(program)['1-registration']
        assert reg['metadata']['ontime']['countToEnd'] is True
        assert starts(program)['2-opening-talk'] == H
        assert_valid(program, tmp_path)

    def test_days_roll_over_as_in_ontime(self, importer):
        # Same clock time as the event before: the next day. After an
        # event that runs past midnight: the next day.
        same = {'a': v4_event('a', 'A', 10 * H, 11 * H), 'b': v4_event('b', 'B', 10 * H, 10 * H + 30 * M)}
        program = importer.import_from_source(json.dumps(v4_project(same, ['a', 'b']))).program
        assert starts(program) == {'a': 0, 'b': 24 * H}
        assert len(program['tracks']) == 1
        late = {'a': v4_event('a', 'A', 23 * H, H), 'b': v4_event('b', 'B', 23 * H + 30 * M, 0)}
        program = importer.import_from_source(json.dumps(v4_project(late, ['a', 'b']))).program
        assert starts(program) == {'a': 0, 'b': 24 * H + 30 * M}

    def test_group_membership_comes_from_the_group(self, importer):
        # Ontime reads order and each group's entries; flatOrder and parent
        # are derived, so stale ones do not move or drop events.
        entries = {
            'g': {'type': 'group', 'id': 'g', 'title': 'G', 'entries': ['a', 'b']},
            'a': v4_event('a', 'A', 10 * H, 11 * H, 'g'),
            'b': v4_event('b', 'B', 11 * H, 12 * H, None),
        }
        data = v4_project(entries, ['g'])
        data['rundowns']['default']['flatOrder'] = ['g', 'a']
        program = importer.import_from_source(json.dumps(data)).program
        assert [(t['name'], [s['stepId'] for s in t['steps']]) for t in program['tracks']] == [('G', ['a', 'b'])]

    def test_a_block_in_a_version_4_rundown_is_a_group(self, importer):
        entries = {
            'b': {'type': 'block', 'id': 'b', 'title': 'Blk', 'entries': ['a']},
            'a': v4_event('a', 'A', 10 * H, 11 * H, 'b'),
            'c': v4_event('c', 'C', 11 * H, 12 * H),
        }
        rundown = {'id': 'r1', 'title': 'R', 'order': ['b', 'c'], 'entries': entries}
        program = importer.import_from_source(json.dumps({'rundowns': {'r1': rundown}})).program
        assert [t['name'] for t in program['tracks']] == ['Blk', 'Rundown']

    def test_lock_end_keeps_the_end(self, importer):
        b = v4_event('b', 'B', 11 * H, 12 * H, timeStrategy='lock-end', duration=1800000)
        program = importer.import_from_source(json.dumps(v4_project({'b': b}, ['b']))).program
        assert program['tracks'][0]['steps'][0]['duration']['seconds'] == H

    def test_not_an_ontime_project(self, importer):
        result = importer.import_from_source('{"tracks": []}')
        assert not result.success and 'not an Ontime project' in result.error
        assert not looks_like_ontime_project({'programId': 'x', 'tracks': [], 'rundown': []})


def xlsx(rows, sheet='Sheet1'):
    """A minimal workbook: inline strings, numbers as numbers."""
    def cell(ref, v):
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'

    body = ''.join(
        f'<row r="{r + 1}">' + ''.join(cell(f'{chr(65 + c)}{r + 1}', v) for c, v in enumerate(row) if v != '')
        + '</row>' for r, row in enumerate(rows))
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'
                                      f'<sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
                   '</Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>{body}</sheetData></worksheet>')
    return buf.getvalue()


class TestTables:
    def test_xlsx_with_a_title_above_the_header_and_spreadsheet_times(self, importer, tmp_path):
        blob = xlsx([
            ['Conference day one'],
            [],
            ['Time Start', 'Time End', 'Duration', 'Cue', 'Title', 'Timer Type', 'Room'],
            [0.375, 0.40625, '', 'A1', 'Keynote', 'count-down', 'Hall'],    # 09:00-09:45
            ['', '', 15, 'A2', 'Q&A', '', 'Hall'],                         # 15 minutes, follows on
            ['', '', '', '', 'Lunch', 'group', ''],
            ['12:00', '13:00', '', 'B1', 'Buffet', '', 'Foyer'],
        ], sheet='Day 1')
        result = importer.import_from_bytes(blob, filename='day-one.xlsx')
        assert result.success, result.error
        program = result.program
        assert program['name'] == 'Day 1'
        assert [t['name'] for t in program['tracks']] == ['Rundown', 'Lunch']
        assert starts(program) == {'a1-keynote': 0, 'a2-q-a': 45 * M, 'b1-buffet': 3 * H}
        _, keynote = steps_by_id(program)['a1-keynote']
        assert keynote['metadata']['ontime']['custom'] == {'Room': 'Hall'}
        assert keynote['metadata']['ontime']['timerType'] == 'count-down'
        assert_valid(program, tmp_path)

    def test_a_default_sheet_name_gives_way_to_the_file_name(self, importer):
        blob = xlsx([['title', 'duration'], ['Soundcheck', '00:30:00']])
        assert importer.import_from_bytes(blob, filename='band_call.xlsx').program['name'] == 'Band call'

    def test_pasted_markdown_table(self, importer, tmp_path):
        text = (
            '| Cue | Title | Start | End | Notes |\n'
            '|-----|-------|-------|-----|-------|\n'
            '| 1 | Walk-in | 7:00 pm | 7:30 pm | |\n'
            '| 2 | Act one | 7:30 pm | 8:15 pm | Spot on centre |\n'
        )
        result = importer.import_from_source(text, title='Play')
        assert result.success, result.error
        program = result.program
        assert program['name'] == 'Play'
        assert program['metadata']['ontime']['showStart'] == '19:00:00'
        _, act = steps_by_id(program)['2-act-one']
        assert act['startTrigger'] == {'type': 'afterStep', 'stepId': '1-walk-in'}
        assert act['description'] == 'Spot on centre'
        assert_valid(program, tmp_path)

    def test_pasted_tab_separated_rows_and_row_kinds(self, importer):
        text = '\n'.join('\t'.join(r) for r in [
            ['title', 'duration', 'timer type', 'skip'],
            ['Intro', '5m', '', ''],
            ['Not this one', '10m', 'skip-import', ''],
            ['Cut for time', '10m', '', 'x'],
            ['Standby', '', 'milestone', ''],
            ['Talk', '1h', '', ''],
            ['Unknown kind', '5m', 'banana', ''],
        ])
        program = importer.import_from_source(text).program
        assert starts(program) == {'intro': 0, 'talk': 5 * M}
        _, talk = steps_by_id(program)['talk']
        assert talk['alerts'] == [{'event': 'start', 'message': 'Standby'}]
        assert program['metadata']['ontime']['skippedEvents'] == ['Cut for time']

    def test_a_trailing_milestone_alerts_at_the_end(self, importer):
        program = importer.import_from_source('title,duration,timer type\nShow,30m,\nCurtain,,milestone\n').program
        _, show = steps_by_id(program)['show']
        assert show['alerts'] == [{'event': 'end', 'message': 'Curtain'}]

    def test_no_header(self, importer):
        result = importer.import_from_source('a,b\n1,2\n')
        assert not result.success and 'No header row' in result.error

    def test_a_table_without_titles_names_steps_by_cue(self, importer):
        program = importer.import_from_source('Cue,Time Start,Duration\n1,10:00,30m\n2,10:30,15m\n').program
        assert [s['name'] for t in program['tracks'] for s in t['steps']] == ['Cue 1', 'Cue 2']

    def test_a_linked_row_with_only_an_end_runs_to_its_end(self, importer):
        # Ontime keeps the end (lock-end) and starts a linked row when the
        # one before it ends.
        text = 'title,time start,time end,link start\nA,10:00,10:30,\nB,10:15,11:00,true\n'
        program = importer.import_from_source(text).program
        assert starts(program) == {'a': 0, 'b': 30 * M}
        _, b = steps_by_id(program)['b']
        assert b['duration']['seconds'] == 30 * M

    def test_yes_no_cells_follow_ontime(self, importer):
        # Any text but "false" is true, a 0 included.
        program = importer.import_from_source('title,time start,duration,skip\nA,10:00,30m,0\nB,10:30,15m,\n').program
        assert list(steps_by_id(program)) == ['b']

    def test_a_skipped_milestone_is_still_a_marker(self, importer):
        text = 'title,time start,duration,timer type,skip\nA,10:00,30m,,\nM,,,milestone,true\n'
        _, a = steps_by_id(importer.import_from_source(text).program)['a']
        assert a['alerts'] == [{'event': 'end', 'message': 'M'}]

    def test_markdown_under_a_heading_and_without_outer_pipes(self, importer):
        for text in (
            '# My show\n\n| Title | Time Start | Duration |\n|---|---|---|\n| A | 10:00 | 30m |\n| B | 10:30 | 15m |\n',
            'Title | Time Start | Duration\n--- | --- | ---\nA | 10:00 | 30m\nB | 10:30 | 15m\n',
        ):
            program = importer.import_from_source(text).program
            assert starts(program) == {'a': 0, 'b': 30 * M}, text

    def test_an_escaped_pipe_stays_in_its_cell(self, importer):
        text = '| Title | Time Start | Duration | Note |\n|---|---|---|---|\n| A \\| B | 10:00 | 30m | x |\n'
        program = importer.import_from_source(text).program
        step = program['tracks'][0]['steps'][0]
        assert step['name'] == 'A | B' and step['description'] == 'x'

    def test_separators_inside_quoted_cells(self, importer):
        for note in ('"col1\tcol2"', '"mics 1;2;3;4;5;6;7;8;9;10;11"'):
            text = f'Title,Time Start,Duration,Note\nA,10:00,30m,{note}\nB,10:30,15m,\n'
            assert starts(importer.import_from_source(text).program) == {'a': 0, 'b': 30 * M}

    def test_a_long_back_to_back_rundown(self, importer):
        text = 'title,duration\n' + ''.join(f'E{i},1m\n' for i in range(1500))
        program = importer.import_from_source(text).program
        assert len(program['tracks'][0]['steps']) == 1500
        assert program['tracks'][0]['steps'][-1]['startTrigger'] == {'type': 'afterStep', 'stepId': 'e1498'}

    def test_nothing_with_a_length(self, importer):
        result = importer.import_from_source('title,duration\nMarker,\n')
        assert not result.success and 'no events' in result.error


@pytest.mark.parametrize('value, seconds', [
    ('10:30:00', 10 * H + 30 * M),
    ('10:30', 10 * H + 30 * M),
    ('7:30 pm', 19 * H + 30 * M),
    ('12:15 am', 15 * M),
    ('12pm', 12 * H),
    ('7p', 19 * H),
    ('7:30p', 19 * H + 30 * M),
    ('7a', 7 * H),
    ('1030', 10 * H + 30 * M),
    ('930', 9 * H + 30 * M),
    ('103000', 10 * H + 30 * M),
    ('1h30', H + 30 * M),
    ('10h30', 10 * H + 30 * M),
    ('1m30', 90),
    ('1 hr 30 min', H + 30 * M),
    ('1h30m', H + 30 * M),
    ('45m', 45 * M),
    ('90s', 90),
    ('2025-05-01T18:45:00', 18 * H + 45 * M),
    (15, 15 * M),
    (0.5, 12 * H),
    (45000.75, 18 * H),
    ('', None),
    (None, None),
    ('soon', None),
])
def test_parse_time(value, seconds):
    assert parse_time(value) == seconds


def test_google_sheet_links_export_as_csv():
    assert google_sheet_csv_url('https://docs.google.com/spreadsheets/d/abc_123/edit#gid=42') == \
        'https://docs.google.com/spreadsheets/d/abc_123/export?format=csv&gid=42'
    assert google_sheet_csv_url('https://example.com/show.csv') == 'https://example.com/show.csv'


def test_local_files_need_permission():
    result = OntimeImporter().import_from_url(str(EXAMPLE))
    assert not result.success and 'local files' in result.error


def test_registered_by_name_never_guessed():
    assert isinstance(ImporterRegistry.get('ontime'), OntimeImporter)
    assert ImporterRegistry.get('ontime').can_import('show.csv') is False
