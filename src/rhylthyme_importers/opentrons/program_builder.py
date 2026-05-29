"""
Assemble a Rhylthyme program JSON from a recognised event stream.

Phase 2 coverage:
- One track per pipette mount, with model-decorated labels
  (e.g. ``Left: P300 single-channel``).
- ``delay`` rendered as a fixed-duration step with task ``delay``.
- ``pause`` rendered as an indefinite-duration step with
  ``startTrigger.type: manual``.
- Helper-method events (``transfer`` / ``distribute`` / ``consolidate``)
  from the AST fallback land on a pipette track as a single step.
- Protocol-level events (``delay``, ``pause``, top-level ``home``)
  render on a dedicated ``Protocol`` track that does not impose a
  pipette-mount resource constraint.

Later phases add module tracks, the Flex gripper, 96-channel blocking
constraints, and cross-track dependencies. The output shape doesn't
change; only the contents grow.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .duration_model import seconds_for
from .events import CommandEvent


_PIPETTE_TASK_TYPES = {
    'pickup_tip', 'drop_tip',
    'aspirate', 'dispense', 'air_gap', 'blow_out', 'touch_tip',
    'mix', 'move_to', 'home',
    'transfer', 'distribute', 'consolidate',
}

_PROTOCOL_TASK_TYPES = {'delay', 'pause'}


# Task-vocabulary mapping for module commands. Matches the slugs used by
# the protocols.io importer so the existing resource-constraint UI
# applies uniformly.
_MODULE_TASKS = {
    'heater_shaker.shake': 'mixing',
    'heater_shaker.set_target_temperature': 'heating',
    'heater_shaker.wait_for_temperature': 'heating',
    'heater_shaker.deactivate_shaker': 'mixing',
    'heater_shaker.deactivate_heater': 'heating',
    'heater_shaker.open_labware_latch': 'measurement',
    'heater_shaker.close_labware_latch': 'measurement',
    'magnetic.engage': 'incubation',
    'magnetic.disengage': 'measurement',
    'temperature.set_temperature': 'heating',
    'temperature.await_temperature': 'incubation',
    'temperature.deactivate': 'measurement',
    'thermocycler.open_lid': 'measurement',
    'thermocycler.close_lid': 'measurement',
    'thermocycler.set_block_temperature': 'heating',
    'thermocycler.set_lid_temperature': 'heating',
    'thermocycler.execute_profile': 'incubation',
    'thermocycler.deactivate': 'measurement',
    'absorbance.initialize': 'measurement',
    'absorbance.read': 'measurement',
    'absorbance.open_lid': 'measurement',
    'absorbance.close_lid': 'measurement',
}


def _task_for(command_type: str) -> str:
    """Map a CommandEvent.command_type to the Rhylthyme task vocabulary.
    Matches the protocols.io importer's slugs so existing resource-
    constraint UI lights up the same way."""
    if command_type in _MODULE_TASKS:
        return _MODULE_TASKS[command_type]
    if command_type == 'mix':
        return 'mixing'
    if command_type == 'delay':
        return 'delay'
    if command_type == 'pause':
        return 'delay'  # manual pause renders under the same constraint family
    if command_type == 'home':
        return 'measurement'
    return 'pipetting'


def _module_track_label(module_id: str) -> str:
    """Human-readable label from a module_id like ``heater-shaker-slot-7``."""
    if '-slot-' in module_id:
        kind, slot = module_id.split('-slot-', 1)
        return f'Slot {slot.upper()}: {_humanise_kind(kind)}'
    return _humanise_kind(module_id)


def _humanise_kind(kind: str) -> str:
    return kind.replace('-', ' ').replace('_', ' ').title()


def _humanise(command_type: str) -> str:
    return command_type.replace('_', ' ').title()


def _step_id(track_key: str, index: int) -> str:
    return f'{track_key or "pipette"}-step-{index}'


def _humanise_model(model: str, channels: int) -> str:
    """Render an Opentrons pipette model string for the track label.
    ``p300_single_gen2`` → ``P300 single-channel``; ``flex_8channel_50``
    → ``Flex 8-channel``."""
    if not model:
        return 'pipette'
    parts = model.replace('_', ' ').strip().split()
    if not parts:
        return 'pipette'
    # Drop generation suffixes (gen1/gen2) — they don't carry meaning
    # in the track label.
    parts = [p for p in parts if not p.lower().startswith('gen')]
    # Normalise channel-count tokens to a consistent "N-channel" form.
    # ``multi`` is OT-2 slang for 8-channel; rewrite for clarity.
    normalised: list[str] = []
    has_channel_token = False
    for p in parts:
        low = p.lower()
        if low == 'multi':
            normalised.append(f'{channels or 8}-channel')
            has_channel_token = True
            continue
        if low.endswith('channel') and low != 'channel':
            # e.g. flex_8channel_50 → "8-channel"
            n_part = low.replace('channel', '')
            try:
                n = int(n_part)
                normalised.append(f'{n}-channel')
                has_channel_token = True
                continue
            except ValueError:
                pass
        if low == 'single':
            normalised.append('single-channel')
            has_channel_token = True
            continue
        normalised.append(p)
    if channels and channels > 1 and not has_channel_token:
        normalised.append(f'{channels}-channel')
    # Capitalise the first token (P20 / P300 / Flex), lowercase the rest.
    head = normalised[0].upper() if normalised[0].lower().startswith('p') else normalised[0].capitalize()
    tail = [p.lower() for p in normalised[1:]]
    return ' '.join([head] + tail)


def _track_label(mount: str, model_by_mount: Dict[str, str],
                 channels_by_mount: Dict[str, int]) -> str:
    if not mount or mount == 'pipette':
        return 'Pipette'
    if mount == 'flex_96':
        # 96-channel doesn't have a "side" — it spans the gantry.
        model = model_by_mount.get('flex_96') or model_by_mount.get('left') or ''
        return f'Flex 96-channel: {_humanise_model(model, 96)}' if model else 'Flex 96-channel'
    if mount == 'gripper':
        return 'Flex Gripper'
    model = model_by_mount.get(mount, '')
    channels = channels_by_mount.get(mount, 1)
    side = mount.capitalize()
    if model:
        return f'{side}: {_humanise_model(model, channels)}'
    return side


def build_program(
    events: Iterable[CommandEvent],
    *,
    name: str = 'Imported Opentrons protocol',
    description: str = '',
    program_id: str = 'opentrons-imported',
    schema_version: str = '0.2.0-alpha',
    model_by_mount: Dict[str, str] | None = None,
    channels_by_mount: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    """Build a Rhylthyme program dict from a recognised CommandEvent stream."""
    events = list(events)
    warnings = [e for e in events if e.command_type == 'WARNING']
    work_events = [e for e in events if e.command_type != 'WARNING']

    model_by_mount = dict(model_by_mount or {})
    channels_by_mount = dict(channels_by_mount or {})

    # Group: pipette events by mount; module events by module_id; the
    # rest on a shared 'Protocol' track.
    by_mount: Dict[str, List[CommandEvent]] = {}
    by_module: Dict[str, List[CommandEvent]] = {}
    protocol_events: List[CommandEvent] = []
    ast_module_events: List[CommandEvent] = []  # module events with no module_id (AST fallback)
    for e in work_events:
        if e.module_id:
            by_module.setdefault(e.module_id, []).append(e)
            continue
        if e.command_type in _MODULE_TASKS:
            # AST fallback emitted a module command but didn't know which
            # module to attribute it to. Park on a generic 'Modules' track.
            ast_module_events.append(e)
            continue
        if e.command_type in _PROTOCOL_TASK_TYPES:
            protocol_events.append(e)
        elif e.command_type == 'home' and e.mount is None:
            # Top-level home (protocol.home()), not pipette.home().
            protocol_events.append(e)
        elif e.command_type in _PIPETTE_TASK_TYPES:
            mount = e.mount or 'pipette'
            by_mount.setdefault(mount, []).append(e)
        else:
            # Unknown — park on a pipette-style track so it still renders.
            mount = e.mount or 'pipette'
            by_mount.setdefault(mount, []).append(e)

    tracks: List[Dict[str, Any]] = []
    resource_constraints: List[Dict[str, Any]] = []

    # Stable mount ordering: left first, then right, then flex_96, then
    # gripper, then anything else.
    def _mount_order(m: str) -> tuple:
        rank = {'left': 0, 'right': 1, 'flex_96': 2, 'gripper': 3}.get(m, 4)
        return (rank, m)

    flex_96_present = 'flex_96' in by_mount
    for mount in sorted(by_mount.keys(), key=_mount_order):
        track_events = by_mount[mount]
        label = _track_label(mount, model_by_mount, channels_by_mount)
        steps = _build_track_steps(track_events, mount)
        tracks.append({
            'trackId': f'track-{mount}',
            'name': label,
            'steps': steps,
        })
        # Skip per-mount pipetting constraints when a 96-channel is loaded
        # — the 96 occupies the full gantry, so a single shared constraint
        # over all pipetting events is correct. The gripper has its own
        # task vocabulary (measurement) so it doesn't collide.
        if flex_96_present and mount in ('left', 'right', 'flex_96'):
            continue
        if mount == 'gripper':
            resource_constraints.append({
                'task': 'measurement',
                'maxConcurrent': 1,
                'description': 'Flex Gripper',
            })
        else:
            resource_constraints.append({
                'task': 'pipetting',
                'maxConcurrent': 1,
                'description': f'{label} pipette mount',
            })

    if flex_96_present:
        # One shared gantry constraint — prevents the 96-channel from
        # running in parallel with either single-mount pipette.
        resource_constraints.append({
            'task': 'pipetting',
            'maxConcurrent': 1,
            'description': 'Flex gantry (96-channel blocks both mounts)',
        })

    # One track per loaded module, ordered by module_id for stability.
    for module_id in sorted(by_module.keys()):
        module_events = by_module[module_id]
        label = _module_track_label(module_id)
        # Slugify module_id for safe Rhylthyme stepId prefixes.
        track_key = module_id.replace('-', '_')
        steps = _build_track_steps(module_events, track_key)
        tracks.append({
            'trackId': f'track-{module_id}',
            'name': label,
            'steps': steps,
        })
        resource_constraints.append({
            'task': _MODULE_TASKS.get(
                module_events[0].command_type, 'incubation'
            ),
            'maxConcurrent': 1,
            'description': label,
        })

    if ast_module_events:
        # AST fallback module events: group on a single shared 'Modules'
        # track so the user still sees the work, even without per-module
        # attribution. No resource constraint here — without module_id
        # we can't model real hardware contention.
        steps = _build_track_steps(ast_module_events, 'modules')
        tracks.append({
            'trackId': 'track-modules',
            'name': 'Modules',
            'steps': steps,
        })

    if protocol_events:
        steps = _build_track_steps(protocol_events, 'protocol')
        tracks.append({
            'trackId': 'track-protocol',
            'name': 'Protocol',
            'steps': steps,
        })

    program: Dict[str, Any] = {
        'schemaVersion': schema_version,
        'programId': program_id,
        'name': name,
        'description': description,
        'environmentType': 'laboratory',
        'tracks': tracks,
        'resourceConstraints': resource_constraints,
        # Actors = number of pipette mounts in use (modules + gripper
        # run themselves and don't need a human-attention slot).
        'actors': max(1, len([
            t for t in tracks
            if t['trackId'].startswith('track-')
            and t['trackId'].split('-', 1)[1] in ('left', 'right', 'pipette', 'flex_96')
        ])),
    }
    if warnings:
        program['metadata'] = {
            'opentronsImportWarnings': [
                {'index': w.index, **dict(w.args)} for w in warnings
            ],
        }
    return program


def _build_track_steps(events: List[CommandEvent], track_key: str) -> List[Dict[str, Any]]:
    """Turn a list of events into Rhylthyme step dicts with sequential
    startTriggers. Handles delay/pause specially per the PRD."""
    out: List[Dict[str, Any]] = []
    prev_step_id: str | None = None
    for i, e in enumerate(events):
        sid = _step_id(track_key, i)
        if e.command_type == 'pause':
            # Indefinite duration; the user resumes via a manual trigger.
            # ``defaultSeconds`` is the display estimate for the timeline
            # (the schema requires it).
            step: Dict[str, Any] = {
                'stepId': sid,
                'name': 'Pause' + (f' — {e.args["msg"]}' if e.args.get('msg') else ''),
                'task': _task_for(e.command_type),
                'duration': {'type': 'indefinite', 'defaultSeconds': 60},
                'startTrigger': (
                    {'type': 'programStart'} if prev_step_id is None
                    else {'type': 'manual'}
                ),
            }
        elif e.command_type == 'delay':
            step = {
                'stepId': sid,
                'name': f'Delay {seconds_for(e)}s',
                'task': _task_for(e.command_type),
                'duration': {'type': 'fixed', 'seconds': seconds_for(e)},
                'startTrigger': (
                    {'type': 'programStart'} if prev_step_id is None
                    else {'type': 'afterStep', 'stepId': prev_step_id}
                ),
            }
        else:
            step = {
                'stepId': sid,
                'name': _humanise(e.command_type),
                'task': _task_for(e.command_type),
                'duration': {'type': 'fixed', 'seconds': seconds_for(e)},
                'startTrigger': (
                    {'type': 'programStart'} if prev_step_id is None
                    else {'type': 'afterStep', 'stepId': prev_step_id}
                ),
            }
        out.append(step)
        prev_step_id = sid
    return out
