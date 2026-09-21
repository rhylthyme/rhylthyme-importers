"""
Phase 1 tests for the Opentrons importer.

Per the implementation plan, v1 covers the simulator + AST-fallback
paths via golden fixtures. Duration model, program builder, and
end-to-end assertions are deferred to later phases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rhylthyme_importers.opentrons import OpentronsImporter
from rhylthyme_importers.opentrons.ast_parser import parse as ast_parse
from rhylthyme_importers.opentrons.simulator import parse as sim_parse


FIXTURES = Path(__file__).parent / 'fixtures' / 'opentrons'


def _load_expected(name: str) -> list[dict]:
    with open(FIXTURES / f'{name}.events.json') as f:
        return json.load(f)


def _events_to_dict(events) -> list[dict]:
    return [e.to_dict() for e in events]


# ---- Simulator path ------------------------------------------------------

class TestSimulator:
    def test_trivial_protocol_emits_expected_event_stream(self):
        source = (FIXTURES / 'trivial.py').read_text()
        events = sim_parse(source, filename='trivial.py')
        assert _events_to_dict(events) == _load_expected('trivial')

    def test_setup_calls_emit_no_events(self):
        # Only load_labware / load_instrument / load_module — should be silent.
        source = (
            'def run(protocol):\n'
            '    plate = protocol.load_labware("foo", 1)\n'
            '    pipette = protocol.load_instrument("p300_single", "right")\n'
        )
        events = sim_parse(source)
        assert events == []


# ---- AST fallback path ---------------------------------------------------

class TestAstParser:
    def test_recognised_pipette_methods_emit_events(self):
        source = (FIXTURES / 'trivial.py').read_text()
        events = ast_parse(source)
        # AST parser produces the same command_types in the same order
        # as the simulator, but without mount info (it can't statically
        # tell which pipette is on which mount).
        types = [e.command_type for e in events if e.command_type != 'WARNING']
        assert types == ['pickup_tip', 'aspirate', 'dispense', 'drop_tip']

    def test_unrecognised_calls_produce_warning(self):
        # The AST parser sees several non-setup, non-recognised calls
        # (the `print` and `do_something_unknown` calls here) and emits
        # a trailing WARNING event.
        source = (
            'def run(protocol):\n'
            '    print("hello")\n'
            '    pipette = protocol.load_instrument("p300_single", "right")\n'
            '    pipette.pick_up_tip()\n'
            '    pipette.do_something_unknown()\n'  # bogus method
        )
        events = ast_parse(source)
        types = [e.command_type for e in events]
        assert 'pickup_tip' in types
        assert types[-1] == 'WARNING'

    def test_syntax_error_returns_single_warning(self):
        events = ast_parse('def run(protocol:\n  pass')  # invalid syntax
        assert len(events) == 1 and events[0].command_type == 'WARNING'


# ---- End-to-end through the importer ------------------------------------

class TestImporter:
    def test_trivial_protocol_yields_valid_program(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'trivial.py'))
        assert result.success, result.error
        prog = result.program
        # Schema shape — minimum the visualiser/validator needs.
        assert prog['schemaVersion']
        assert prog['programId']
        assert prog['name'] == 'Trivial single-aspirate-and-dispense'
        # 4 events all on the left mount → 1 track, 4 steps.
        assert len(prog['tracks']) == 1
        # Phase 2: track label includes pipette model.
        assert prog['tracks'][0]['name'].startswith('Left:')
        assert len(prog['tracks'][0]['steps']) == 4
        # Sequential startTriggers
        steps = prog['tracks'][0]['steps']
        assert steps[0]['startTrigger']['type'] == 'programStart'
        for i in range(1, 4):
            assert steps[i]['startTrigger']['type'] == 'afterStep'
            assert steps[i]['startTrigger']['stepId'] == steps[i-1]['stepId']
        # Durations are non-zero
        assert all(s['duration']['seconds'] > 0 for s in steps)
        # Resource constraint marks the mount
        assert prog['resourceConstraints']
        assert prog['resourceConstraints'][0]['task'] == 'pipetting'
        assert prog['resourceConstraints'][0]['maxConcurrent'] == 1

    def test_non_protocol_python_file_is_rejected(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_source('x = 1\nprint(x)\n')
        assert not result.success
        assert 'run()' in (result.error or '')

    def test_can_import_detects_py_extension(self):
        importer = OpentronsImporter(allow_local_files=True)
        assert importer.can_import('protocol.py')
        assert not importer.can_import('protocol.json')
        assert not importer.can_import('https://example.com/foo')


# ---- Phase 2: full pipette coverage -------------------------------------

class TestPhase2Simulator:
    """Per-command tests for the Phase 2 simulator vocabulary."""

    def _events(self, body: str):
        source = (
            'def run(protocol):\n'
            '    p = protocol.load_instrument("p300_single_gen2", "left")\n'
            + body
        )
        return sim_parse(source)

    def test_every_low_level_pipette_command_emits_event(self):
        events = self._events(
            '    p.pick_up_tip()\n'
            '    p.aspirate(50, "A1")\n'
            '    p.air_gap(10)\n'
            '    p.dispense(50, "B1")\n'
            '    p.blow_out("B1")\n'
            '    p.touch_tip()\n'
            '    p.mix(3, 50, "B1")\n'
            '    p.move_to("trash")\n'
            '    p.drop_tip()\n'
        )
        types = [e.command_type for e in events]
        assert types == [
            'pickup_tip', 'aspirate', 'air_gap', 'dispense',
            'blow_out', 'touch_tip', 'mix', 'move_to', 'drop_tip',
        ]
        # mix carries the repetitions through
        mix_event = next(e for e in events if e.command_type == 'mix')
        assert mix_event.args['repetitions'] == 3

    def test_pickup_tip_alias_normalised(self):
        # Opentrons accepts both pick_up_tip and pickup_tip.
        events = self._events('    p.pickup_tip()\n    p.pick_up_tip()\n')
        assert [e.command_type for e in events] == ['pickup_tip', 'pickup_tip']

    def test_transfer_expands_to_low_level_sequence(self):
        events = self._events('    p.transfer(50, "A1", "B1")\n')
        # Default new_tip='always' for a single cycle.
        assert [e.command_type for e in events] == [
            'pickup_tip', 'aspirate', 'dispense', 'drop_tip',
        ]

    def test_transfer_with_new_tip_once(self):
        events = self._events(
            '    p.transfer([50, 50], ["A1", "A2"], ["B1", "B2"], new_tip="once")\n'
        )
        assert [e.command_type for e in events] == [
            'pickup_tip', 'aspirate', 'dispense', 'aspirate', 'dispense', 'drop_tip',
        ]

    def test_distribute_aspirates_once_dispenses_per_dest(self):
        events = self._events(
            '    p.distribute(30, "A1", ["B1", "B2", "B3"])\n'
        )
        assert [e.command_type for e in events] == [
            'pickup_tip', 'aspirate', 'dispense', 'dispense', 'dispense', 'drop_tip',
        ]

    def test_consolidate_aspirates_per_source_dispenses_once(self):
        events = self._events(
            '    p.consolidate(30, ["A1", "A2", "A3"], "B1")\n'
        )
        assert [e.command_type for e in events] == [
            'pickup_tip', 'aspirate', 'aspirate', 'aspirate', 'dispense', 'drop_tip',
        ]

    def test_delay_records_total_seconds(self):
        source = (
            'def run(protocol):\n'
            '    protocol.delay(seconds=10, minutes=1)\n'
        )
        events = sim_parse(source)
        assert len(events) == 1
        assert events[0].command_type == 'delay'
        assert events[0].args['seconds'] == 70

    def test_pause_emits_pause_event(self):
        source = (
            'def run(protocol):\n'
            '    protocol.pause("waiting for user")\n'
        )
        events = sim_parse(source)
        assert len(events) == 1
        assert events[0].command_type == 'pause'

    def test_setup_calls_still_emit_no_events(self):
        source = (
            'def run(protocol):\n'
            '    plate = protocol.load_labware("foo", 1)\n'
            '    mod = protocol.load_module("heaterShakerModuleV1", 7)\n'
            '    pip = protocol.load_instrument("p300_single", "right")\n'
        )
        assert sim_parse(source) == []


class TestPhase2AstParity:
    """The AST fallback should produce a subset of the simulator's event
    types when run against the same source. Helpers stay opaque (not
    expanded) but their command_types are still recognised."""

    def test_pcr_setup_ast_is_subset_of_simulator(self):
        source = (FIXTURES / 'pcr_setup.py').read_text()
        sim_events = sim_parse(source)
        ast_events = ast_parse(source)
        sim_types = [e.command_type for e in sim_events
                     if e.command_type != 'WARNING']
        ast_types = [e.command_type for e in ast_events
                     if e.command_type != 'WARNING']
        # Every AST event should be a recognised type the simulator also knows
        # (the AST parser doesn't see for-loops or expand helpers, so the
        # counts/ordering differ; we just check the *type set* is consistent).
        assert set(ast_types).issubset(set(sim_types))
        # The pause + delay are statically visible — they MUST appear.
        assert 'pause' in ast_types
        assert 'delay' in ast_types

    def test_serial_dilution_ast_recognises_helpers(self):
        source = (FIXTURES / 'serial_dilution.py').read_text()
        ast_events = ast_parse(source)
        types = [e.command_type for e in ast_events
                 if e.command_type != 'WARNING']
        # The AST parser sees `distribute(...)` and `transfer(...)` as
        # single calls — it does NOT expand them. Both must appear.
        assert 'distribute' in types
        assert 'transfer' in types


class TestPhase2Builder:
    """Two-mount, delay/pause, and track-label assertions on the
    builder output. The serial-dilution fixture exercises 8-channel
    on left + single on right."""

    def test_pcr_setup_renders_one_mount_plus_protocol_track(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'pcr_setup.py'))
        assert result.success
        prog = result.program
        track_names = [t['name'] for t in prog['tracks']]
        # Right mount track + Protocol track for delay/pause.
        assert any(n.startswith('Right:') for n in track_names)
        assert 'Protocol' in track_names

    def test_pause_renders_as_indefinite_manual(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'pcr_setup.py'))
        prog = result.program
        proto_track = next(t for t in prog['tracks'] if t['name'] == 'Protocol')
        pause_step = next(s for s in proto_track['steps'] if s['name'].startswith('Pause'))
        # Indefinite duration with a display-estimate defaultSeconds
        # (the schema requires defaultSeconds for indefinite steps).
        assert pause_step['duration']['type'] == 'indefinite'
        assert pause_step['duration']['defaultSeconds'] > 0
        # First event on the protocol track is the pause, so its trigger
        # is programStart, not manual. (Manual triggers apply to non-first
        # pause steps.) Either way, the duration is what makes it manual.

    def test_delay_renders_as_fixed_duration_with_delay_task(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'pcr_setup.py'))
        prog = result.program
        proto_track = next(t for t in prog['tracks'] if t['name'] == 'Protocol')
        delay_step = next(s for s in proto_track['steps'] if s['name'].startswith('Delay'))
        assert delay_step['task'] == 'delay'
        assert delay_step['duration']['type'] == 'fixed'
        assert delay_step['duration']['seconds'] == 30

    def test_two_mount_protocol_produces_two_tracks_with_constraints(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'serial_dilution.py'))
        prog = result.program
        pipette_tracks = [t for t in prog['tracks'] if t['name'] != 'Protocol']
        assert len(pipette_tracks) == 2
        names = [t['name'] for t in pipette_tracks]
        assert any('Left:' in n for n in names)
        assert any('Right:' in n for n in names)
        # 8-channel surfaces in the left mount's label.
        assert any('8-channel' in n for n in names)
        # One resourceConstraints entry per pipette track, maxConcurrent: 1.
        assert len(prog['resourceConstraints']) == 2
        assert all(rc['maxConcurrent'] == 1 for rc in prog['resourceConstraints'])

    def test_helper_expansion_results_in_correct_step_counts(self):
        """The simulator's expansion of distribute/transfer/mix into
        low-level events should be reflected in the rendered step count
        per track."""
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'serial_dilution.py'))
        prog = result.program
        left = next(t for t in prog['tracks'] if 'Left:' in t['name'])
        right = next(t for t in prog['tracks'] if 'Right:' in t['name'])
        # Left: distribute expands to 10 (1 pickup + 1 aspirate + 7 dispenses
        # + 1 drop), and each of 7 serial-dilution iterations is 5 events
        # (pickup, aspirate, dispense, mix, drop) → 10 + 35 = 45.
        assert len(left['steps']) == 45
        # Right: one transfer call expands to 4 (pickup + aspirate + dispense + drop).
        assert len(right['steps']) == 4


# ---- Phase 3: modules with background tracks -----------------------------

class TestPhase3Modules:
    """Per-module-kind tests + the ELISA fixture end-to-end."""

    def _events(self, body: str):
        source = 'def run(protocol):\n' + body
        return sim_parse(source)

    def test_heater_shaker_commands_emit_typed_events(self):
        events = self._events(
            '    hs = protocol.load_module("heaterShakerModuleV1", 7)\n'
            '    hs.close_labware_latch()\n'
            '    hs.set_and_wait_for_shake_speed(1500)\n'
            '    hs.deactivate_shaker()\n'
            '    hs.open_labware_latch()\n'
        )
        types = [e.command_type for e in events]
        assert types == [
            'heater_shaker.close_labware_latch',
            'heater_shaker.shake',
            'heater_shaker.deactivate_shaker',
            'heater_shaker.open_labware_latch',
        ]
        # All four events carry the same module_id.
        assert {e.module_id for e in events} == {'heater-shaker-slot-7'}

    def test_magnetic_engage_disengage(self):
        events = self._events(
            '    m = protocol.load_module("magneticModuleV2", 4)\n'
            '    m.engage(height_from_base=4)\n'
            '    m.disengage()\n'
        )
        assert [e.command_type for e in events] == [
            'magnetic.engage', 'magnetic.disengage',
        ]

    def test_temperature_module_set_and_await(self):
        events = self._events(
            '    t = protocol.load_module("temperatureModuleV2", 9)\n'
            '    t.set_temperature(4)\n'
            '    t.await_temperature(4)\n'
            '    t.deactivate()\n'
        )
        types = [e.command_type for e in events]
        assert types == [
            'temperature.set_temperature',
            'temperature.await_temperature',
            'temperature.deactivate',
        ]
        # The set/await events carry the celsius arg through.
        set_evt = next(e for e in events if e.command_type == 'temperature.set_temperature')
        assert set_evt.args['celsius'] == 4

    def test_thermocycler_execute_profile_sums_step_seconds(self):
        events = self._events(
            '    tc = protocol.load_module("thermocyclerModuleV2")\n'
            '    tc.execute_profile(\n'
            '        steps=[\n'
            '            {"temperature": 95, "hold_time_seconds": 30},\n'
            '            {"temperature": 60, "hold_time_seconds": 30},\n'
            '            {"temperature": 72, "hold_time_seconds": 60},\n'
            '        ],\n'
            '        repetitions=25,\n'
            '    )\n'
        )
        # One event with the precomputed total: (30+30+60)*25 = 3000s.
        prof = next(e for e in events if e.command_type == 'thermocycler.execute_profile')
        assert prof.args['seconds'] == 3000
        assert prof.args['repetitions'] == 25
        assert prof.args['step_count'] == 3

    def test_absorbance_reader_initialize_then_read(self):
        events = self._events(
            '    a = protocol.load_module("absorbanceReaderV1")\n'
            '    a.initialize(mode="single", wavelengths=[450])\n'
            '    a.read()\n'
        )
        types = [e.command_type for e in events]
        assert types == ['absorbance.initialize', 'absorbance.read']


class TestPhase3Builder:
    """The ELISA fixture exercises four module kinds; verify the builder
    produces a separate track + resource constraint per loaded module
    and tags events with the right task vocabulary."""

    def test_elisa_program_has_one_track_per_module(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'elisa.py'))
        assert result.success
        prog = result.program
        names = [t['name'] for t in prog['tracks']]
        # One pipette track + four module tracks.
        assert any('Left:' in n for n in names)
        assert any('Heater Shaker' in n for n in names)
        assert any('Magnetic' in n for n in names)
        assert any('Temperature' in n for n in names)
        assert any('Absorbance' in n for n in names)

    def test_elisa_emits_resource_constraint_per_module(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'elisa.py'))
        prog = result.program
        # 1 pipette + 4 modules = 5 constraints, each maxConcurrent=1.
        assert len(prog['resourceConstraints']) == 5
        assert all(rc['maxConcurrent'] == 1 for rc in prog['resourceConstraints'])

    def test_module_steps_use_protocols_io_task_vocab(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'elisa.py'))
        prog = result.program
        tasks = {
            s['task']
            for t in prog['tracks']
            for s in t['steps']
        }
        # Module steps should bring in heating/incubation/measurement/mixing
        # alongside the pipette track's pipetting task.
        assert 'pipetting' in tasks
        assert 'heating' in tasks       # temperature.set_temperature
        assert 'incubation' in tasks    # magnetic.engage
        assert 'mixing' in tasks        # heater_shaker.shake
        assert 'measurement' in tasks   # absorbance.read

    def test_thermocycler_profile_duration_comes_from_steps(self):
        """An execute_profile call with explicit hold_times should
        produce a step whose duration matches the precomputed seconds."""
        source = (
            'def run(protocol):\n'
            '    tc = protocol.load_module("thermocyclerModuleV2")\n'
            '    tc.execute_profile(steps=[\n'
            '        {"temperature": 95, "hold_time_seconds": 30},\n'
            '        {"temperature": 60, "hold_time_seconds": 30},\n'
            '    ], repetitions=10)\n'
        )
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_source(source)
        prog = result.program
        tc_track = next(t for t in prog['tracks'] if 'Thermocycler' in t['name'])
        profile_step = next(
            s for s in tc_track['steps'] if 'Execute Profile' in s['name']
        )
        # (30 + 30) * 10 = 600s
        assert profile_step['duration']['seconds'] == 600


# ---- Phase 4: Flex hardware ---------------------------------------------

class TestPhase4Flex:
    """Flex pipettes (1ch / 8ch / 96ch) + gripper."""

    def test_flex_96_channel_routes_to_dedicated_track(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'cell_culture_passage.py'))
        assert result.success
        prog = result.program
        names = [t['name'] for t in prog['tracks']]
        # 96-channel track is its own thing — not labelled "Left:".
        assert any(n.startswith('Flex 96-channel') for n in names)
        # Right mount p50 still gets a Right: track.
        assert any(n.startswith('Right:') for n in names)
        # Gripper gets its own track.
        assert any(n == 'Flex Gripper' for n in names)

    def test_flex_96_emits_shared_gantry_constraint(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'cell_culture_passage.py'))
        prog = result.program
        constraints = prog['resourceConstraints']
        # Exactly ONE pipetting constraint when 96-channel is loaded
        # (the shared gantry), not per-mount.
        pipetting_constraints = [c for c in constraints if c['task'] == 'pipetting']
        assert len(pipetting_constraints) == 1
        assert 'gantry' in pipetting_constraints[0]['description'].lower()
        assert pipetting_constraints[0]['maxConcurrent'] == 1

    def test_gripper_renders_with_its_own_resource_constraint(self):
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_url(str(FIXTURES / 'cell_culture_passage.py'))
        prog = result.program
        constraints = prog['resourceConstraints']
        gripper_c = [c for c in constraints if 'Gripper' in c['description']]
        assert len(gripper_c) == 1
        assert gripper_c[0]['maxConcurrent'] == 1
        # Gripper steps appear on a dedicated track with duration from
        # the static estimate.
        gripper_track = next(t for t in prog['tracks'] if t['name'] == 'Flex Gripper')
        assert len(gripper_track['steps']) == 2
        for step in gripper_track['steps']:
            assert step['duration']['seconds'] > 0

    def test_gripper_move_labware_command_type(self):
        source = (
            'def run(protocol):\n'
            '    plate = protocol.load_labware("foo", 1)\n'
            '    temp = protocol.load_module("temperatureModuleV2", 9)\n'
            '    protocol.move_labware(plate, temp, use_gripper=True)\n'
        )
        events = sim_parse(source)
        assert len(events) == 1
        assert events[0].command_type == 'gripper.move_labware'
        assert events[0].mount == 'gripper'

    def test_manual_move_labware_renders_as_pause(self):
        # Without use_gripper=True, move_labware is a manual operator
        # action — surfaces as a pause prompt, not a gripper step.
        source = (
            'def run(protocol):\n'
            '    plate = protocol.load_labware("foo", 1)\n'
            '    protocol.move_labware(plate, 5)\n'
        )
        events = sim_parse(source)
        assert len(events) == 1
        assert events[0].command_type == 'pause'

    def test_flex_pipette_model_appears_in_track_label(self):
        # Flex 1ch / 8ch render with their model in the track name.
        source = (
            'def run(protocol):\n'
            '    p = protocol.load_instrument("flex_8channel_50", "left")\n'
            '    p.pick_up_tip()\n'
            '    p.drop_tip()\n'
        )
        importer = OpentronsImporter(allow_local_files=True)
        result = importer.import_from_source(source)
        prog = result.program
        left = next(t for t in prog['tracks'] if t['name'].startswith('Left:'))
        # The humaniser turns flex_8channel_50 into "Flex 8-channel 50"
        # (channel-count token gets normalised).
        assert '8-channel' in left['name']
        assert 'Flex' in left['name']


class TestLocalFilesAreOptIn:
    """An importer reachable from user input must not read local paths."""

    def test_default_importer_refuses_a_local_path(self):
        result = OpentronsImporter().import_from_url(str(FIXTURES / 'trivial.py'))
        assert result.success is False
        assert 'local files are not accepted' in result.error

    def test_the_registered_importer_is_the_default_kind(self):
        from rhylthyme_importers import ImporterRegistry

        registered = ImporterRegistry.get('opentrons')
        assert registered is not None
        assert getattr(registered, 'allow_local_files', False) is False
