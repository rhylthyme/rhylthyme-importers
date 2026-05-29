"""
Fallback Opentrons parser: walks the AST and recognises the same
command vocabulary as the simulator, statically.

Used when :func:`opentrons.simulator.parse` raises. The AST parser
emits one :class:`CommandEvent` per recognised ``thing.method(...)`` it
finds, in source order. It can't know which mount a pipette is on or
which module is loaded where, so the ``mount`` and ``module_id`` fields
stay ``None``.

Helper methods (``transfer`` / ``distribute`` / ``consolidate``) are
emitted as a SINGLE event of the same name — we can't statically know
the runtime expansion (which depends on arg shapes, ``new_tip``, etc.),
so the program builder treats these as opaque pipette steps. This is
a known difference from the simulator's expanded stream.

A trailing ``WARNING`` event is emitted whenever any unrecognised
``thing.method(...)`` call appears, so the UI can surface "this protocol
was parsed statically; some steps may be missing."
"""

from __future__ import annotations

import ast
from typing import List

from .events import CommandEvent


# Methods on an instrument stub that we treat as recognised pipette
# commands. Mirrors the simulator's _InstrumentStub surface.
_PIPETTE_COMMANDS = {
    'pick_up_tip', 'pickup_tip',
    'drop_tip', 'return_tip',
    'aspirate', 'dispense',
    'mix', 'move_to', 'air_gap', 'blow_out', 'touch_tip', 'home',
    # Helpers emit a single event; simulator does the real expansion.
    'transfer', 'distribute', 'consolidate',
}

# Methods on the ProtocolContext we treat as work.
_PROTOCOL_COMMANDS = {
    'delay', 'pause', 'home', 'move_labware',
}

# Methods on module stubs. AST-side, we can't know WHICH module a call
# is on without semantic analysis, so all of these get tagged with a
# generic ``module`` prefix in the recorded command_type. The simulator
# attaches the real module_id; the AST fallback uses ``module_id=None``
# so the builder parks them on a generic "Modules" track.
_MODULE_COMMANDS_BY_KIND = {
    'heater_shaker': {
        'set_and_wait_for_shake_speed': 'heater_shaker.shake',
        'set_target_temperature': 'heater_shaker.set_target_temperature',
        'wait_for_temperature': 'heater_shaker.wait_for_temperature',
        'deactivate_shaker': 'heater_shaker.deactivate_shaker',
        'deactivate_heater': 'heater_shaker.deactivate_heater',
        'open_labware_latch': 'heater_shaker.open_labware_latch',
        'close_labware_latch': 'heater_shaker.close_labware_latch',
    },
    'magnetic': {
        'engage': 'magnetic.engage',
        'disengage': 'magnetic.disengage',
    },
    'temperature': {
        'set_temperature': 'temperature.set_temperature',
        'await_temperature': 'temperature.await_temperature',
        'deactivate': 'temperature.deactivate',
    },
    'thermocycler': {
        'open_lid': 'thermocycler.open_lid',
        'close_lid': 'thermocycler.close_lid',
        'set_block_temperature': 'thermocycler.set_block_temperature',
        'set_lid_temperature': 'thermocycler.set_lid_temperature',
        'execute_profile': 'thermocycler.execute_profile',
    },
    'absorbance': {
        'initialize': 'absorbance.initialize',
        'read': 'absorbance.read',
    },
}

# Flat lookup: method name → preferred command_type. Methods unique to
# one module (engage, execute_profile, read) map directly; ambiguous
# names (deactivate, open_lid, close_lid) stay out of this table so the
# AST parser doesn't guess.
_MODULE_METHOD_TO_TYPE: dict[str, str] = {}
_AMBIGUOUS: dict[str, int] = {}
for _kind, _ms in _MODULE_COMMANDS_BY_KIND.items():
    for _method in _ms:
        _AMBIGUOUS[_method] = _AMBIGUOUS.get(_method, 0) + 1
for _kind, _ms in _MODULE_COMMANDS_BY_KIND.items():
    for _method, _ct in _ms.items():
        if _AMBIGUOUS.get(_method, 0) == 1:
            _MODULE_METHOD_TO_TYPE[_method] = _ct

# Setup calls we explicitly want to NOT count as work.
_SETUP_CALLS = {'load_labware', 'load_adapter', 'load_instrument', 'load_module'}


def _method_name(call: ast.Call) -> str:
    """Return the rightmost identifier in `something.method(...)`,
    or ``''`` if the call isn't an attribute access."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


def _normalise(method: str) -> str:
    """Map Opentrons aliases to the canonical CommandEvent.command_type."""
    if method in ('pick_up_tip', 'pickup_tip'):
        return 'pickup_tip'
    if method == 'return_tip':
        return 'drop_tip'
    return method


def _kwargs(call: ast.Call) -> dict:
    """Best-effort: collect keyword arguments whose values are literal
    ints, floats, strings, or None. Anything else is dropped."""
    out: dict = {}
    for kw in call.keywords:
        if kw.arg is None:
            continue
        v = kw.value
        if isinstance(v, ast.Constant):
            out[kw.arg] = v.value
    return out


def parse(source: str, *, filename: str = '<protocol>') -> List[CommandEvent]:
    """Walk the source's AST and return recognised command events."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return [CommandEvent(
            command_type='WARNING',
            index=0,
            args={'message': 'AST parse failed; protocol has a SyntaxError'},
        )]

    events: List[CommandEvent] = []
    next_index = 0
    saw_unrecognised = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = _method_name(node)
        if not method:
            continue
        if method in _SETUP_CALLS:
            continue
        if method in _PIPETTE_COMMANDS:
            events.append(CommandEvent(
                command_type=_normalise(method),
                index=next_index,
                args=_kwargs(node),
                line=getattr(node, 'lineno', None),
            ))
            next_index += 1
        elif method in _PROTOCOL_COMMANDS:
            args = _kwargs(node)
            # delay(seconds=N, minutes=M) — pre-aggregate so the duration
            # model sees a single 'seconds' arg, matching the simulator.
            if method == 'delay':
                total = 0
                try:
                    total = int(args.get('seconds') or 0) + int(args.get('minutes') or 0) * 60
                except (TypeError, ValueError):
                    total = 0
                args = {'seconds': total}
                ct = 'delay'
            elif method == 'move_labware':
                # Only count as a gripper event when use_gripper=True is
                # explicitly set. Other moves are manual / informational.
                if args.get('use_gripper') is True:
                    ct = 'gripper.move_labware'
                else:
                    # Manual move — render as a pause-like prompt.
                    ct = 'pause'
                    args = {'msg': 'Move labware manually'}
            else:
                ct = method
            events.append(CommandEvent(
                command_type=ct,
                index=next_index,
                args=args,
                line=getattr(node, 'lineno', None),
            ))
            next_index += 1
        elif method in _MODULE_METHOD_TO_TYPE:
            # Unambiguous module command (engage, execute_profile, read, ...).
            # We don't statically know the module_id, so leave it None
            # — the builder parks these on a generic 'Modules' track.
            events.append(CommandEvent(
                command_type=_MODULE_METHOD_TO_TYPE[method],
                index=next_index,
                args=_kwargs(node),
                line=getattr(node, 'lineno', None),
            ))
            next_index += 1
        else:
            saw_unrecognised = True

    if saw_unrecognised:
        events.append(CommandEvent(
            command_type='WARNING',
            index=next_index,
            args={'message': 'AST fallback used; some calls were not recognised'},
        ))

    return events
