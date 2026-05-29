"""``NormalizedProtocol`` → Rhylthyme program JSON.

Pure transform. No HTTP, no Benchling-isms — only this module knows
about Rhylthyme's schema, so the normalizer stays decoupled from the
output format.

Track assignment (Phase 2): one track per distinct instrument, plus a
"Bench" track for steps with no instrument. Within a track, steps run
in their declaration order. Cross-track predecessors (e.g. a step on
the gel-box track that depends on a step on the thermocycler track)
emit an explicit ``afterStep`` trigger referencing the predecessor's
stepId — Rhylthyme renders that as a dashed cross-track edge in the
DAG view.

Steps that reference more than one instrument go onto the FIRST
instrument's track; subsequent instruments still produce resource
constraints, so a centrifuge-then-PCR step still blocks both
instruments via the constraint system.

Resource constraints: one entry per distinct instrument with
``maxConcurrent: 1`` (Phase 2 doesn't yet read the tenant registry to
raise the per-kind cap).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from .events import NormalizedProtocol, NormalizedStep


# Map our internal instrument "kind" to a Rhylthyme task taxonomy term.
# Reuses the protocols.io + Opentrons importers' vocabulary so existing
# resource-constraint UI lights up the same way.
_KIND_TO_TASK = {
    "thermocycler": "heating",
    "centrifuge": "centrifugation",
    "incubator": "incubation",
    "plate_reader": "measurement",
    "shaker": "mixing",
    "vortex": "mixing",
    "balance": "measurement",
    "other": "manual",
}

# Filled per build_program() call so _step_task can look up the kind for
# a given instrument ref id without re-walking the instrument list.
_INSTRUMENT_KIND_LOOKUP: Dict[str, str] = {}


def _step_task(step: NormalizedStep) -> str:
    """Pick a Rhylthyme `task` value for a step. Uses the FIRST
    instrument's kind when present; falls back to a name-keyword
    heuristic; defaults to ``manual``.
    """
    if step.instrument_refs:
        return _KIND_TO_TASK.get(_INSTRUMENT_KIND_LOOKUP.get(step.instrument_refs[0], "other"), "manual")
    name_lc = (step.name or "").lower()
    if "incubate" in name_lc or "wait" in name_lc: return "incubation"
    if "mix" in name_lc or "stir" in name_lc or "vortex" in name_lc: return "mixing"
    if "spin" in name_lc or "centrifuge" in name_lc: return "centrifugation"
    if "heat" in name_lc or "pcr" in name_lc: return "heating"
    if "cool" in name_lc or "chill" in name_lc: return "cooling"
    if "measure" in name_lc or "read" in name_lc: return "measurement"
    if "pipette" in name_lc or "transfer" in name_lc: return "pipetting"
    return "manual"


def _track_id_for_instrument(ref_id: str) -> str:
    """Slugify an instrument ref id into a stable trackId."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", ref_id.lower()).strip("-")
    return f"inst-{slug}" if slug else "inst-unknown"


def build_program(protocol: NormalizedProtocol) -> Dict[str, Any]:
    """Render a Rhylthyme program JSON from a normalized Benchling protocol."""
    global _INSTRUMENT_KIND_LOOKUP
    _INSTRUMENT_KIND_LOOKUP = {i.ref_id: i.kind for i in protocol.instruments}
    inst_name_by_id = {i.ref_id: i.name for i in protocol.instruments}

    program_id = f"benchling-{protocol.benchling_id}".replace(" ", "-").lower()

    # ----- Multi-track assignment -----
    # Each instrument gets its own track. Steps with no instrument go
    # onto a "Bench" track. Multi-instrument steps go on the first
    # instrument's track (additional refs still produce constraints).
    BENCH_TRACK_ID = "bench"
    track_steps_by_id: Dict[str, List[Dict[str, Any]]] = {}
    track_name_by_id: Dict[str, str] = {}
    track_order: List[str] = []

    def _ensure_track(tid: str, tname: str) -> None:
        if tid not in track_steps_by_id:
            track_steps_by_id[tid] = []
            track_name_by_id[tid] = tname
            track_order.append(tid)

    seen_step_ids = {s.step_id for s in protocol.steps}
    step_track_assignment: Dict[str, str] = {}

    # First pass: decide track per step and record the order they appear.
    for idx, step in enumerate(protocol.steps):
        if step.instrument_refs:
            primary = step.instrument_refs[0]
            tid = _track_id_for_instrument(primary)
            tname = inst_name_by_id.get(primary, primary)
        else:
            tid = BENCH_TRACK_ID
            tname = "Bench"
        _ensure_track(tid, tname)
        step_track_assignment[step.step_id] = tid

    # Second pass: emit step JSON with predecessor-aware triggers. The
    # "first step on a track" gets the natural predecessor from the
    # protocol's declaration order; subsequent steps on the same track
    # chain off the previous step on that track if it's the natural
    # predecessor, OR keep the cross-track predecessor as an explicit
    # afterStep reference (handled natively by Rhylthyme).
    last_step_on_track: Dict[str, str] = {}
    for idx, step in enumerate(protocol.steps):
        tid = step_track_assignment[step.step_id]

        # Resolve predecessor. If recorded predecessor isn't in this
        # protocol's set, fall back to the previous declaration order.
        recorded_pred = step.predecessor_id if step.predecessor_id in seen_step_ids else None
        pred = recorded_pred
        if pred is None and idx > 0:
            pred = protocol.steps[idx - 1].step_id

        # Duration: explicit when supplied; otherwise indefinite with a
        # triggerName. We don't invent durations.
        if step.duration_seconds is not None and step.duration_seconds > 0:
            duration_block: Dict[str, Any] = {
                "type": "fixed",
                "seconds": int(step.duration_seconds),
            }
        else:
            duration_block = {
                "type": "indefinite",
                "triggerName": f"complete_{step.step_id}",
                "defaultSeconds": 60,
            }

        # Start trigger.
        start_trigger: Dict[str, Any]
        if pred is None:
            start_trigger = {"type": "programStart"}
        else:
            start_trigger = {"type": "afterStep", "stepId": pred}

        track_steps_by_id[tid].append({
            "stepId": step.step_id,
            "name": step.name,
            "description": step.description,
            "task": _step_task(step),
            "duration": duration_block,
            "startTrigger": start_trigger,
        })
        last_step_on_track[tid] = step.step_id

    tracks: List[Dict[str, Any]] = []
    for tid in track_order:
        tracks.append({
            "trackId": tid,
            "name": track_name_by_id[tid],
            "description": "Bench work" if tid == BENCH_TRACK_ID else f"Work on {track_name_by_id[tid]}",
            "steps": track_steps_by_id[tid],
        })

    # ----- Resource constraints -----
    # One entry per distinct instrument; v1 caps each at maxConcurrent=1.
    resource_constraints: List[Dict[str, Any]] = []
    for inst in protocol.instruments:
        resource_constraints.append({
            "task": _KIND_TO_TASK.get(inst.kind, "manual"),
            "maxConcurrent": 1,
            "description": inst.name,
        })

    return {
        "programId": program_id,
        "name": protocol.name,
        "description": protocol.description,
        "version": "1.0.0",
        "environmentType": "laboratory",
        "startTrigger": {"type": "manual"},
        "tracks": tracks,
        "resourceConstraints": resource_constraints,
        "metadata": {
            "source": {
                "type": "benchling",
                "url": protocol.source_url,
                "tenant": protocol.tenant,
                "benchlingProtocolId": protocol.benchling_id,
                "revision": protocol.revision,
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "importer": "benchling",
            },
        },
    }
