"""
Primary Opentrons parser: run the protocol against a stubbed
``ProtocolContext`` and record every recognised method call as a
:class:`CommandEvent`.

The stub deliberately mimics only enough of the Opentrons Protocol API
that a typical protocol's ``run(protocol)`` function executes without
exceptions. Anything the protocol calls that the stub doesn't recognise
is silently allowed (no-op) — the goal is to extract the call sequence,
not to validate the protocol.

Phase 2 coverage:
- Setup: ``load_labware``, ``load_adapter``, ``load_instrument``,
  ``load_module``. No events emitted.
- Low-level pipette commands: ``pick_up_tip`` / ``pickup_tip``,
  ``drop_tip``, ``aspirate``, ``dispense``, ``mix``, ``move_to``,
  ``air_gap``, ``blow_out``, ``touch_tip``, ``home``.
- Helper methods: ``transfer``, ``distribute``, ``consolidate``. These
  expand into the equivalent low-level event stream, matching what the
  Opentrons run log actually emits at run time (pickup → aspirate →
  dispense → drop, possibly broadcast across multi-element args, with
  ``new_tip`` honoured).
- Protocol-level: ``protocol.delay(seconds=N)`` and ``protocol.pause(...)``.

Pure function: ``parse(source: str) -> list[CommandEvent]``.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional

from .events import CommandEvent


# ---- Recorder -----------------------------------------------------------

class _Recorder:
    """Mutable list of events shared across every stub in a single parse."""

    def __init__(self) -> None:
        self.events: List[CommandEvent] = []
        self._next_index = 0

    def emit(self, command_type: str, *, mount: Optional[str] = None,
             module_id: Optional[str] = None, **args: Any) -> None:
        self.events.append(CommandEvent(
            command_type=command_type,
            index=self._next_index,
            args=dict(args),
            mount=mount,
            module_id=module_id,
        ))
        self._next_index += 1


# ---- Labware stub -------------------------------------------------------

class _LabwareStub:
    """Stand-in for an Opentrons Labware object.

    Real labware exposes wells, columns, etc. that protocols index into
    (``plate['A1']``). Returning ``self`` from ``__getitem__`` and a
    no-op-returns-self for any attribute means whatever the protocol
    does with the result also flows. For slice access (``plate.wells()[:8]``)
    we still return a stub so subsequent indexing stays safe.
    """

    def __init__(self, name: str = '') -> None:
        self.name = name

    def __getitem__(self, _key: Any) -> '_LabwareStub':
        return self

    def __iter__(self):
        # Real labware lists are iterable. Yield a single stub so the
        # protocol can `for well in plate.wells()` without exploding.
        yield self

    def __len__(self) -> int:
        return 1

    def __getattr__(self, _name: str) -> Any:
        # wells, rows, top, bottom, ... — every attribute is callable
        # and returns self for chained access.
        return lambda *a, **kw: self


# ---- Helper expansion ---------------------------------------------------

def _coerce_seq(v: Any) -> list:
    """Best-effort: turn a transfer/distribute argument into a list so we
    can iterate over it. Plain stubs and scalars return a single-element
    list (one cycle); real Python lists pass through."""
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    # Strings, numbers, stubs — treat as a single cycle.
    return [v]


# ---- Instrument stub ----------------------------------------------------

class _InstrumentStub:
    """Stand-in for an Opentrons InstrumentContext (a pipette).

    Each method that represents real work emits one ``CommandEvent``;
    helper methods (`transfer`, `distribute`, `consolidate`) expand into
    the equivalent low-level event stream.
    """

    def __init__(self, recorder: _Recorder, mount: str, model: str,
                 channels: int = 1) -> None:
        self._rec = recorder
        self.mount = mount
        self.model = model
        self.channels = channels

    # ---- Low-level pipette commands ----

    def pick_up_tip(self, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('pickup_tip', mount=self.mount)
        return self

    pickup_tip = pick_up_tip  # Opentrons accepts both spellings

    def drop_tip(self, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('drop_tip', mount=self.mount)
        return self

    def return_tip(self, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('drop_tip', mount=self.mount)
        return self

    def aspirate(self, volume: Any = None, location: Any = None,
                 *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('aspirate', mount=self.mount, volume=_to_jsonable(volume))
        return self

    def dispense(self, volume: Any = None, location: Any = None,
                 *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('dispense', mount=self.mount, volume=_to_jsonable(volume))
        return self

    def mix(self, repetitions: Any = 1, volume: Any = None,
            location: Any = None, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        reps = repetitions if isinstance(repetitions, int) else 1
        self._rec.emit('mix', mount=self.mount,
                       repetitions=reps, volume=_to_jsonable(volume))
        return self

    def move_to(self, location: Any = None, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('move_to', mount=self.mount)
        return self

    def air_gap(self, volume: Any = None, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('air_gap', mount=self.mount, volume=_to_jsonable(volume))
        return self

    def blow_out(self, location: Any = None, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('blow_out', mount=self.mount)
        return self

    def touch_tip(self, location: Any = None, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('touch_tip', mount=self.mount)
        return self

    def home(self, *args: Any, **kwargs: Any) -> '_InstrumentStub':
        self._rec.emit('home', mount=self.mount)
        return self

    # ---- Helper methods ----
    #
    # In real Opentrons, ``transfer`` / ``distribute`` / ``consolidate``
    # expand into the corresponding sequence of low-level calls at
    # run time. We replicate that expansion here so the recorded event
    # stream matches what an Opentrons run log emits.

    def transfer(self, volume: Any, source: Any, dest: Any,
                 *args: Any, **kwargs: Any) -> '_InstrumentStub':
        new_tip = kwargs.get('new_tip', 'always')
        vols = _coerce_seq(volume)
        srcs = _coerce_seq(source)
        dsts = _coerce_seq(dest)
        # Broadcast: cycles is the longest of the three argument lists,
        # matching Opentrons' actual transfer semantics for multi-well args.
        cycles = max(len(vols), len(srcs), len(dsts))

        def _pickup_once():
            self._rec.emit('pickup_tip', mount=self.mount)

        def _drop_once():
            self._rec.emit('drop_tip', mount=self.mount)

        if new_tip == 'once':
            _pickup_once()
        for i in range(cycles):
            if new_tip == 'always':
                _pickup_once()
            v = vols[i] if i < len(vols) else vols[-1]
            self._rec.emit('aspirate', mount=self.mount, volume=_to_jsonable(v))
            self._rec.emit('dispense', mount=self.mount, volume=_to_jsonable(v))
            if new_tip == 'always':
                _drop_once()
        if new_tip == 'once':
            _drop_once()
        return self

    def distribute(self, volume: Any, source: Any, dest: Any,
                   *args: Any, **kwargs: Any) -> '_InstrumentStub':
        new_tip = kwargs.get('new_tip', 'once')
        dsts = _coerce_seq(dest)
        if new_tip in ('once', 'always'):
            self._rec.emit('pickup_tip', mount=self.mount)
        # distribute: ONE aspirate (the combined volume), THEN N dispenses
        self._rec.emit('aspirate', mount=self.mount, volume=_to_jsonable(volume))
        for _ in dsts:
            self._rec.emit('dispense', mount=self.mount, volume=_to_jsonable(volume))
        if new_tip in ('once', 'always'):
            self._rec.emit('drop_tip', mount=self.mount)
        return self

    def consolidate(self, volume: Any, source: Any, dest: Any,
                    *args: Any, **kwargs: Any) -> '_InstrumentStub':
        new_tip = kwargs.get('new_tip', 'once')
        srcs = _coerce_seq(source)
        if new_tip in ('once', 'always'):
            self._rec.emit('pickup_tip', mount=self.mount)
        # consolidate: N aspirates, then ONE dispense
        for _ in srcs:
            self._rec.emit('aspirate', mount=self.mount, volume=_to_jsonable(volume))
        self._rec.emit('dispense', mount=self.mount, volume=_to_jsonable(volume))
        if new_tip in ('once', 'always'):
            self._rec.emit('drop_tip', mount=self.mount)
        return self

    # Anything we haven't taught the stub becomes a no-op chaining call
    # so protocols don't crash on commands we'll teach in later phases.
    def __getattr__(self, _name: str) -> Any:
        return lambda *a, **kw: self


# ---- Module stubs -------------------------------------------------------
#
# Phase 3: each module kind gets its own stub class that records the
# commands its real API exposes. Every recorded event carries the
# stable ``module_id`` so the program builder can assign them to a
# dedicated track AND emit one resource constraint per module.

class _ModuleBase:
    """Common base: each module records events with a stable id and
    delegates unknown attribute access to a no-op so a protocol that
    calls a future-phase method (e.g. a thermocycler step we haven't
    modelled) doesn't crash."""

    # Subclasses override.
    kind: str = 'module'

    def __init__(self, recorder: _Recorder, module_id: str, slot: Any) -> None:
        self._rec = recorder
        self.module_id = module_id
        self.slot = slot

    def _emit(self, command_type: str, **args: Any) -> None:
        self._rec.emit(command_type, module_id=self.module_id, **args)

    def __getattr__(self, _name: str) -> Any:
        # Default: any unrecognised method is a chain-returning no-op.
        # Subclasses define methods above this layer for things they
        # explicitly record.
        return lambda *a, **kw: _LabwareStub()


class _HeaterShakerStub(_ModuleBase):
    kind = 'heater-shaker'

    def set_and_wait_for_shake_speed(self, rpm: Any = None,
                                     *args: Any, **kw: Any) -> Any:
        self._emit('heater_shaker.shake', rpm=_to_jsonable(rpm))
        return self

    def set_target_temperature(self, celsius: Any = None,
                               *args: Any, **kw: Any) -> Any:
        self._emit('heater_shaker.set_target_temperature',
                   celsius=_to_jsonable(celsius))
        return self

    def wait_for_temperature(self, *args: Any, **kw: Any) -> Any:
        self._emit('heater_shaker.wait_for_temperature')
        return self

    def deactivate_shaker(self, *args: Any, **kw: Any) -> Any:
        self._emit('heater_shaker.deactivate_shaker')
        return self

    def deactivate_heater(self, *args: Any, **kw: Any) -> Any:
        self._emit('heater_shaker.deactivate_heater')
        return self

    def open_labware_latch(self, *args: Any, **kw: Any) -> Any:
        self._emit('heater_shaker.open_labware_latch')
        return self

    def close_labware_latch(self, *args: Any, **kw: Any) -> Any:
        self._emit('heater_shaker.close_labware_latch')
        return self


class _MagneticStub(_ModuleBase):
    kind = 'magnetic'

    def engage(self, height_from_base: Any = None,
               *args: Any, **kw: Any) -> Any:
        self._emit('magnetic.engage',
                   height_from_base=_to_jsonable(height_from_base))
        return self

    def disengage(self, *args: Any, **kw: Any) -> Any:
        self._emit('magnetic.disengage')
        return self


class _TemperatureStub(_ModuleBase):
    kind = 'temperature'

    def set_temperature(self, celsius: Any = None,
                        *args: Any, **kw: Any) -> Any:
        self._emit('temperature.set_temperature',
                   celsius=_to_jsonable(celsius))
        return self

    def await_temperature(self, celsius: Any = None,
                          *args: Any, **kw: Any) -> Any:
        self._emit('temperature.await_temperature',
                   celsius=_to_jsonable(celsius))
        return self

    def deactivate(self, *args: Any, **kw: Any) -> Any:
        self._emit('temperature.deactivate')
        return self


class _ThermocyclerStub(_ModuleBase):
    kind = 'thermocycler'

    def open_lid(self, *args: Any, **kw: Any) -> Any:
        self._emit('thermocycler.open_lid')
        return self

    def close_lid(self, *args: Any, **kw: Any) -> Any:
        self._emit('thermocycler.close_lid')
        return self

    def set_block_temperature(self, celsius: Any = None,
                              *args: Any, **kw: Any) -> Any:
        self._emit('thermocycler.set_block_temperature',
                   celsius=_to_jsonable(celsius))
        return self

    def set_lid_temperature(self, celsius: Any = None,
                            *args: Any, **kw: Any) -> Any:
        self._emit('thermocycler.set_lid_temperature',
                   celsius=_to_jsonable(celsius))
        return self

    def execute_profile(self, steps: Any = None, repetitions: Any = 1,
                        *args: Any, **kw: Any) -> Any:
        # Total seconds = repetitions * sum of each step's `hold_time_seconds`.
        # We compute it here so the duration model can read a precomputed
        # value from event.args, matching the protocol's true wall-clock.
        total = 0
        try:
            reps = int(repetitions or 1)
        except (TypeError, ValueError):
            reps = 1
        if isinstance(steps, (list, tuple)):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                try:
                    total += int(step.get('hold_time_seconds') or 0)
                    total += int(step.get('hold_time_minutes') or 0) * 60
                except (TypeError, ValueError):
                    pass
            total *= reps
        self._emit('thermocycler.execute_profile',
                   seconds=total,
                   repetitions=reps,
                   step_count=(len(steps) if isinstance(steps, (list, tuple)) else 0))
        return self

    def deactivate(self, *args: Any, **kw: Any) -> Any:
        self._emit('thermocycler.deactivate')
        return self


class _AbsorbanceStub(_ModuleBase):
    kind = 'absorbance'

    def initialize(self, mode: Any = None, wavelengths: Any = None,
                   *args: Any, **kw: Any) -> Any:
        self._emit('absorbance.initialize',
                   mode=_to_jsonable(mode),
                   wavelengths=_to_jsonable(wavelengths))
        return self

    def read(self, *args: Any, **kw: Any) -> Any:
        self._emit('absorbance.read')
        return self

    def open_lid(self, *args: Any, **kw: Any) -> Any:
        self._emit('absorbance.open_lid')
        return self

    def close_lid(self, *args: Any, **kw: Any) -> Any:
        self._emit('absorbance.close_lid')
        return self


# Map Opentrons load_module() name → stub class.
def _resolve_module_class(load_name: str) -> type[_ModuleBase]:
    n = load_name.lower()
    if 'heatershaker' in n or 'heater_shaker' in n or 'heater-shaker' in n:
        return _HeaterShakerStub
    if 'magnetic' in n or 'magnetic_block' in n or 'magneticblock' in n:
        return _MagneticStub
    if 'temperature' in n:
        return _TemperatureStub
    if 'thermocycler' in n:
        return _ThermocyclerStub
    if 'absorbance' in n:
        return _AbsorbanceStub
    return _ModuleBase  # unknown module → no events, just a no-op stub


# ---- Protocol context stub ----------------------------------------------

class _ProtocolContextStub:
    """The object an Opentrons ``run(protocol)`` function receives."""

    def __init__(self, recorder: _Recorder) -> None:
        self._rec = recorder
        self.deck: Any = _LabwareStub('deck')

    # ---- Setup calls: emit no events ----

    def load_labware(self, load_name: Any = '', location: Any = None,
                     *args: Any, **kwargs: Any) -> _LabwareStub:
        return _LabwareStub(str(load_name))

    def load_adapter(self, *args: Any, **kwargs: Any) -> _LabwareStub:
        return _LabwareStub()

    def load_instrument(self, instrument_name: Any = '', mount: Any = '',
                        *args: Any, **kwargs: Any) -> _InstrumentStub:
        channels = _channels_from_model(str(instrument_name))
        # The Flex 96-channel pipette physically occupies BOTH gantry
        # mounts. Tag its events with a synthetic ``flex_96`` mount so
        # the program builder can render a dedicated track and emit the
        # shared gantry resource constraint instead of per-mount ones.
        mount_str = str(mount)
        if channels == 96:
            mount_str = 'flex_96'
        return _InstrumentStub(
            self._rec,
            mount=mount_str,
            model=str(instrument_name),
            channels=channels,
        )

    def load_module(self, module_name: Any = '', location: Any = None,
                    *args: Any, **kwargs: Any) -> _ModuleBase:
        cls = _resolve_module_class(str(module_name))
        # Stable id: ``<kind>-slot-<location>`` (or just ``<kind>`` for
        # the thermocycler / absorbance reader, which occupy fixed slots
        # and don't have an explicit ``location``).
        slot_token = (
            str(location).replace(' ', '_').lower() if location not in (None, '') else ''
        )
        module_id = f'{cls.kind}-slot-{slot_token}' if slot_token else cls.kind
        return cls(self._rec, module_id=module_id, slot=location)

    # ---- Protocol-level work ----

    def delay(self, seconds: Any = 0, minutes: Any = 0,
              msg: Any = None, *args: Any, **kwargs: Any) -> None:
        total = 0
        try:
            total = int(seconds or 0) + int(minutes or 0) * 60
        except (TypeError, ValueError):
            total = 0
        self._rec.emit('delay', seconds=total, msg=_to_jsonable(msg))

    def pause(self, msg: Any = None, *args: Any, **kwargs: Any) -> None:
        self._rec.emit('pause', msg=_to_jsonable(msg))

    def home(self, *args: Any, **kwargs: Any) -> None:
        self._rec.emit('home')

    def move_labware(self, labware: Any = None, new_location: Any = None,
                     *args: Any, **kwargs: Any) -> None:
        """``protocol.move_labware(...)`` is how Flex protocols drive the
        gripper. When called with ``use_gripper=True`` (or implicitly on
        Flex with the gripper attached), emit a step on the Gripper
        track; otherwise the move is performed manually by the operator
        and we model it as a pause-like manual step."""
        use_gripper = bool(kwargs.get('use_gripper'))
        if use_gripper:
            self._rec.emit('gripper.move_labware', mount='gripper')
        else:
            # Manual move: indefinite duration, surfaces in the Protocol
            # track as a pause so the operator knows to intervene.
            self._rec.emit('pause', msg='Move labware manually')

    # Anything else — comment, set_rail_lights, etc. — is a silent no-op.
    def __getattr__(self, _name: str) -> Any:
        return lambda *a, **kw: None


# ---- Pipette model → channel count --------------------------------------

def _channels_from_model(model: str) -> int:
    """Best-effort: OT-2 and Flex pipette model strings encode their
    channel count. ``p300_multi`` / ``p300_8`` / ``flex_1channel_50`` etc."""
    m = model.lower()
    if '96' in m:
        return 96
    if 'multi' in m or '_8' in m or '8channel' in m or '8_channel' in m:
        return 8
    return 1


# ---- Helpers ------------------------------------------------------------

def _to_jsonable(v: Any) -> Any:
    """Coerce a value into something json.dumps can handle.
    Real Opentrons protocols pass labware/well objects (stubs here);
    stringify them so the args bag stays serializable."""
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return repr(v)


# ---- Public entry point -------------------------------------------------

class SimulatorError(Exception):
    """Raised when the protocol can't be executed against the stub."""


def parse(source: str, *, filename: str = '<protocol>') -> List[CommandEvent]:
    """Run ``source`` against the stubbed ProtocolContext and return the
    recorded event list. Raises :class:`SimulatorError` on failure — the
    caller falls back to the AST parser."""
    try:
        code = compile(source, filename, 'exec')
    except SyntaxError as e:
        raise SimulatorError(f'syntax error at line {e.lineno}: {e.msg}') from e

    rec = _Recorder()
    ns: dict = {'__name__': '__main__'}
    try:
        exec(code, ns, ns)
    except Exception as e:
        raise SimulatorError(f'protocol module import failed: {e}') from e

    runner = ns.get('run')
    if not callable(runner):
        raise SimulatorError('protocol has no callable run(protocol) function')

    ctx = _ProtocolContextStub(rec)
    try:
        runner(ctx)
    except Exception as e:
        raise SimulatorError(f'protocol run() raised {type(e).__name__}: {e}') from e

    return list(rec.events)
