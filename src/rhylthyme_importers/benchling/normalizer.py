"""Convert a raw Benchling API response into the internal
``NormalizedProtocol`` shape.

Benchling has multiple ways to model a protocol; this module knows how
to read each one. ``normalize_protocol`` is the public entry point and
dispatches based on the shape of the raw payload:

  1. Protocol resource (``GET /api/v2/protocols/{id}``) — flat list of
     steps with explicit ``duration_minutes`` / instrument refs.
  2. Workflow Task (``GET /api/v2/workflow-tasks/{id}``) — steps live in
     ``schema.fields`` definitions plus a ``procedure`` rich-text field
     that may carry parametric duration hints in its description.
  3. Notebook Entry (``GET /api/v2/entries/{id}``) — steps come from
     entry blocks where ``block.type == "step"`` (or similar). Non-step
     blocks (text/table/figure) are skipped.

The normalizer is pure: given the JSON, return a ``NormalizedProtocol``.
No HTTP, no side effects.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .events import NormalizedInstrument, NormalizedProtocol, NormalizedStep


# Parametric duration hints inside step descriptions. Caught when
# Benchling's ``duration_minutes`` field is missing. Matches:
#   "incubate for 30 minutes" / "spin for 5 min" / "wait 2 hours" /
#   "for 30 sec" / "for 1h30m" (loose; tightened in Phase 2)
_DURATION_PATTERNS = [
    # "for 30 minutes" / "for 5 min" / "for 2 hours" / "for 30 seconds"
    re.compile(
        r"\bfor\s+(\d+(?:\.\d+)?)\s*"
        r"(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
        re.IGNORECASE,
    ),
    # Bare patterns: "incubate 30 minutes", "wait 2 hours", "spin 5 min"
    re.compile(
        r"\b(?:incubate|wait|spin|run|hold|rest|chill|cool|heat)\s+"
        r"(\d+(?:\.\d+)?)\s*"
        r"(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
        re.IGNORECASE,
    ),
]

_UNIT_TO_SECONDS = {
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}


def _parse_duration_seconds(raw: Dict[str, Any]) -> Optional[int]:
    """Find a duration in the Benchling step JSON, in order of preference.

    1. Explicit ``duration_minutes`` / ``duration_seconds`` field.
    2. ``duration`` object with ``seconds`` or ``minutes`` keys.
    3. Parametric ``for N <unit>`` / verb-prefixed pattern in description.
    Returns ``None`` when nothing matched.
    """
    # (1) explicit field
    if isinstance(raw.get("duration_seconds"), (int, float)):
        return int(raw["duration_seconds"])
    if isinstance(raw.get("duration_minutes"), (int, float)):
        return int(raw["duration_minutes"] * 60)
    # (2) object with sub-fields
    d = raw.get("duration")
    if isinstance(d, dict):
        if isinstance(d.get("seconds"), (int, float)):
            return int(d["seconds"])
        if isinstance(d.get("minutes"), (int, float)):
            return int(d["minutes"] * 60)
    # (3) parametric in description
    desc = raw.get("description") or ""
    if isinstance(desc, str):
        for pat in _DURATION_PATTERNS:
            m = pat.search(desc)
            if m:
                num = float(m.group(1))
                unit = m.group(2).lower()
                sec = _UNIT_TO_SECONDS.get(unit)
                if sec is not None:
                    return int(num * sec)
    return None


# Heuristic: classify a Benchling instrument's "kind" from its name so
# the program builder can pick a sensible `task`. Conservative — falls
# back to "other" rather than guessing wrong.
def _classify_instrument(name: str) -> str:
    n = (name or "").lower()
    if "thermocycler" in n or "pcr" in n: return "thermocycler"
    if "centrifuge" in n or "spin" in n: return "centrifuge"
    if "incubator" in n: return "incubator"
    if "plate reader" in n or "spectrophotometer" in n: return "plate_reader"
    if "shaker" in n: return "shaker"
    if "vortex" in n: return "vortex"
    if "balance" in n or "scale" in n: return "balance"
    return "other"


def _extract_instrument_refs(rs: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pull a normalized list of {ref_id, name} from a Benchling step JSON.

    Accepts the common ``instruments`` array (list of str ids or
    {id, name} dicts) AND the Workflow-Task style where a field named
    ``"Instrument"`` carries the ref. Returns an ordered list with
    duplicates preserved (caller may dedupe at the protocol level).
    """
    out: List[Dict[str, str]] = []
    for ref in rs.get("instruments") or []:
        if isinstance(ref, str) and ref:
            out.append({"ref_id": ref, "name": ref})
        elif isinstance(ref, dict):
            rid = str(ref.get("id") or ref.get("name") or "").strip()
            if not rid:
                continue
            out.append({"ref_id": rid, "name": str(ref.get("name") or rid)})
    # Workflow-task style: fields.Instrument.value = "Thermocycler 1"
    fields = rs.get("fields") or {}
    if isinstance(fields, dict):
        for fname, fval in fields.items():
            if not isinstance(fval, dict):
                continue
            if fname.lower() in {"instrument", "instruments", "equipment"}:
                v = fval.get("value")
                if isinstance(v, str) and v.strip():
                    out.append({"ref_id": v.strip(), "name": v.strip()})
                elif isinstance(v, list):
                    for item in v:
                        s = item if isinstance(item, str) else (item.get("name") if isinstance(item, dict) else None)
                        if s:
                            out.append({"ref_id": str(s), "name": str(s)})
    return out


def _step_from_raw(
    rs: Dict[str, Any],
    *,
    step_index: int,
    prev_step_id: Optional[str],
) -> tuple[NormalizedStep, List[Dict[str, str]]]:
    """Convert a single Benchling-step-shaped dict to a NormalizedStep
    plus its instrument references (caller folds into the protocol
    instrument table).
    """
    sid = str(rs.get("id") or f"step_{step_index + 1}")
    sname = str(rs.get("name") or f"Step {step_index + 1}")
    sdesc = str(rs.get("description") or "")
    dur = _parse_duration_seconds(rs)
    temp = rs.get("temperature_c")
    ref_records = _extract_instrument_refs(rs)
    ref_ids = [r["ref_id"] for r in ref_records]

    predecessor = (
        str(rs["predecessor_id"])
        if rs.get("predecessor_id")
        else prev_step_id
    )
    step = NormalizedStep(
        step_id=sid,
        name=sname,
        description=sdesc,
        duration_seconds=dur,
        instrument_refs=ref_ids,
        temperature_c=float(temp) if isinstance(temp, (int, float)) else None,
        predecessor_id=predecessor,
    )
    return step, ref_records


def _build_instrument_table(
    ref_streams: List[List[Dict[str, str]]],
) -> List[NormalizedInstrument]:
    """Dedupe instrument refs collected across all steps. Preserves
    first-seen order so the program builder generates stable tracks.
    """
    seen: Dict[str, NormalizedInstrument] = {}
    for refs in ref_streams:
        for r in refs:
            rid = r["ref_id"]
            if rid not in seen:
                seen[rid] = NormalizedInstrument(
                    ref_id=rid,
                    name=r["name"],
                    kind=_classify_instrument(r["name"]),
                )
    return list(seen.values())


# ---- Shape-specific decoders -----------------------------------------

def _decode_protocol(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``GET /api/v2/protocols/{id}`` — steps array straight off the
    top level. The simplest shape."""
    return list(raw.get("steps") or [])


def _decode_workflow_task(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``GET /api/v2/workflow-tasks/{id}`` — steps come from a
    ``procedure`` rich-text field decomposed into a list, OR an
    explicit ``steps`` field when the schema author defined one.
    Falls back to a single "Run workflow task" step when no
    structure is present."""
    if isinstance(raw.get("steps"), list) and raw["steps"]:
        return list(raw["steps"])
    proc = raw.get("procedure")
    if isinstance(proc, list) and proc:
        # Already pre-split into step dicts. Pass through.
        return [p for p in proc if isinstance(p, dict)]
    # Single-step fallback: synthesize one step from the task's name +
    # description. Duration extraction still works on the description.
    return [{
        "id": str(raw.get("id") or "wftask_1"),
        "name": str(raw.get("name") or "Workflow task"),
        "description": str(raw.get("description") or ""),
    }]


def _decode_entry(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``GET /api/v2/entries/{id}`` — Notebook Entry. Steps come from
    blocks where ``type == "step"``. Non-step blocks (text/table/figure)
    are skipped. Some templates use ``type == "protocol-step"``; we
    accept both."""
    blocks = raw.get("blocks") or raw.get("days") or []
    if not isinstance(blocks, list):
        return []
    step_blocks: List[Dict[str, Any]] = []
    # Some Notebook Entries nest blocks under days[i].blocks. Flatten.
    flat: List[Dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if isinstance(b.get("blocks"), list):
            flat.extend(x for x in b["blocks"] if isinstance(x, dict))
        else:
            flat.append(b)
    for b in flat:
        btype = str(b.get("type") or "").lower()
        if btype in {"step", "protocol-step", "protocol_step"}:
            step_blocks.append(b)
    return step_blocks


def _shape_of(raw: Dict[str, Any]) -> str:
    """Detect which Benchling shape the raw payload represents."""
    ot = str(raw.get("object_type") or raw.get("type") or "").lower()
    if "workflow" in ot or "workflow_task" in ot:
        return "workflow_task"
    if "entry" in ot or "notebook" in ot:
        return "entry"
    # Heuristic fallbacks when object_type isn't set.
    if "blocks" in raw or "days" in raw:
        return "entry"
    if "procedure" in raw or "schema" in raw and "fields" in raw:
        return "workflow_task"
    return "protocol"


def normalize_protocol(raw: Dict[str, Any], *, tenant: str) -> NormalizedProtocol:
    """Convert a raw Benchling API response into the internal shape.

    Dispatches by detected payload shape (Protocol / Workflow Task /
    Notebook Entry). ``tenant`` is the Benchling subdomain (used to
    build the source URL).
    """
    shape = _shape_of(raw)
    if shape == "workflow_task":
        raw_steps = _decode_workflow_task(raw)
        url_path = "workflow-tasks"
    elif shape == "entry":
        raw_steps = _decode_entry(raw)
        url_path = "entries"
    else:
        raw_steps = _decode_protocol(raw)
        url_path = "protocols"

    benchling_id = str(raw.get("id") or "unknown")
    name = str(raw.get("name") or "Untitled Protocol")
    description = str(raw.get("description") or "")
    revision = str(raw.get("revision") or raw.get("version") or "")
    source_url = f"https://{tenant}.benchling.com/{url_path}/{benchling_id}"

    steps: List[NormalizedStep] = []
    ref_streams: List[List[Dict[str, str]]] = []
    prev_step_id: Optional[str] = None
    for i, rs in enumerate(raw_steps):
        if not isinstance(rs, dict):
            continue
        step, refs = _step_from_raw(rs, step_index=i, prev_step_id=prev_step_id)
        steps.append(step)
        ref_streams.append(refs)
        prev_step_id = step.step_id

    return NormalizedProtocol(
        benchling_id=benchling_id,
        name=name,
        description=description,
        steps=steps,
        instruments=_build_instrument_table(ref_streams),
        source_url=source_url,
        tenant=tenant,
        revision=revision,
    )
