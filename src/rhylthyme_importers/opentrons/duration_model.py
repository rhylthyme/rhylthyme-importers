"""
Pure ``CommandEvent → seconds`` lookup.

Static estimates only. Phase 2 covers the full pipette command surface
and protocol-level ``delay`` / ``pause``. Later phases swap this for a
volume- and distance-aware model; the public function signature stays
the same so neither the parser nor the program builder needs to change.

``delay`` reads its ``seconds`` arg directly (since the protocol told us
exactly how long to wait). ``pause`` reports zero seconds because the
program builder turns it into an indefinite-duration step with a manual
trigger — the wait time is whatever the human operator takes.
``mix`` scales by repetition count (each rep is one aspirate + one
dispense pair).

Helper methods (``transfer`` / ``distribute`` / ``consolidate``) are only
seen via the AST fallback path; the simulator expands them into their
constituent commands. Estimates here are conservative single-cycle
defaults so AST-parsed protocols still display sane totals.

Documentation auto-generates a table from ``DURATION_SECONDS`` via a
``make docs`` step (Phase 6), so the dict here is the source of truth.
"""

from __future__ import annotations

from .events import CommandEvent


# command_type → seconds.
DURATION_SECONDS: dict[str, int] = {
    # Tip handling
    'pickup_tip': 6,
    'drop_tip': 4,
    # Liquid transfer
    'aspirate': 4,
    'dispense': 4,
    'air_gap': 3,
    'blow_out': 3,
    # In-place
    'mix': 6,            # per repetition, scaled in seconds_for()
    'touch_tip': 3,
    # Movement
    'move_to': 8,
    'home': 12,
    # Helper-method fallbacks (AST-only path)
    'transfer': 18,
    'distribute': 22,
    'consolidate': 22,
    # Protocol-level
    'delay': 0,          # reads event.args['seconds']
    'pause': 0,          # rendered as indefinite-duration / manual
    # Heater-shaker. ``shake`` is the duration of the shake; protocols
    # typically start shaking, run other work, then deactivate. The
    # default below is a placeholder when the protocol doesn't carry
    # an explicit duration arg — phase 4+ may pull more from context.
    'heater_shaker.shake': 60,
    'heater_shaker.set_target_temperature': 8,
    'heater_shaker.wait_for_temperature': 60,
    'heater_shaker.deactivate_shaker': 8,
    'heater_shaker.deactivate_heater': 8,
    'heater_shaker.open_labware_latch': 6,
    'heater_shaker.close_labware_latch': 6,
    # Magnetic. ``engage`` settles the beads; default of 30s matches the
    # typical wait used in published ELISA / DNA prep protocols.
    'magnetic.engage': 30,
    'magnetic.disengage': 6,
    # Temperature module. ``set_temperature`` is a command; the wait
    # happens when the protocol then calls ``await_temperature``.
    'temperature.set_temperature': 6,
    'temperature.await_temperature': 120,
    'temperature.deactivate': 6,
    # Thermocycler. ``execute_profile`` reads its precomputed seconds.
    'thermocycler.open_lid': 30,
    'thermocycler.close_lid': 30,
    'thermocycler.set_block_temperature': 60,
    'thermocycler.set_lid_temperature': 60,
    'thermocycler.execute_profile': 0,  # reads event.args['seconds']
    'thermocycler.deactivate': 6,
    # Absorbance module (Flex).
    'absorbance.initialize': 30,
    'absorbance.read': 45,
    'absorbance.open_lid': 6,
    'absorbance.close_lid': 6,
    # Flex gripper (Phase 4). One labware move on the Flex deck is
    # ~15s including grip, lift, traverse, release. The model is a
    # static estimate; the actual time depends on source/dest distance.
    'gripper.move_labware': 15,
}

_DEFAULT_SECONDS = 0


def seconds_for(event: CommandEvent) -> int:
    """Estimated wall-clock seconds for ``event``.

    Special cases:
    - ``delay``: returns the ``seconds`` arg the protocol provided.
    - ``mix``: returns ``mix_base * repetitions`` (default 1 rep).
    - ``pause``: returns 0 — the program builder renders it as
      indefinite/manual instead.
    """
    ct = event.command_type
    if ct == 'delay':
        try:
            return max(0, int(event.args.get('seconds') or 0))
        except (TypeError, ValueError):
            return 0
    if ct == 'mix':
        base = DURATION_SECONDS.get('mix', 0)
        try:
            reps = int(event.args.get('repetitions') or 1)
        except (TypeError, ValueError):
            reps = 1
        return max(base, base * reps)
    if ct == 'thermocycler.execute_profile':
        try:
            return max(0, int(event.args.get('seconds') or 0))
        except (TypeError, ValueError):
            return 0
    return DURATION_SECONDS.get(ct, _DEFAULT_SECONDS)
