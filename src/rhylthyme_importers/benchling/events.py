"""Internal data contract between the three Benchling importer modules.

These classes are the single seam Phases 2+ extend. New fields can be
added as ``Optional``; existing fields must not change shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class NormalizedInstrument:
    """A piece of shared equipment referenced by a protocol step.

    ``ref_id`` is the stable Benchling instrument id we use to deduplicate
    references across steps. ``name`` is the human-readable name (e.g.
    "Thermocycler 1", "Centrifuge — bench top").
    """

    ref_id: str
    name: str
    # Type tag drives `task` assignment in the program builder.
    # One of: "thermocycler", "centrifuge", "incubator", "plate_reader",
    # "shaker", "vortex", "balance", "other".
    kind: str = "other"


@dataclass(frozen=True)
class NormalizedStep:
    """One step in a normalized protocol.

    ``duration_seconds`` is ``None`` when Benchling didn't supply a
    duration AND no parametric hint could be mined from the description.
    The program builder emits such steps as ``indefinite`` with a
    ``triggerName`` so the bench user can mark-complete manually.

    ``predecessor_id`` is the step this step depends on. ``None`` means
    "first step in this track / starts at programStart".
    """

    step_id: str
    name: str
    description: str = ""
    duration_seconds: Optional[int] = None
    instrument_refs: List[str] = field(default_factory=list)
    temperature_c: Optional[float] = None
    predecessor_id: Optional[str] = None


@dataclass(frozen=True)
class NormalizedProtocol:
    """Internal representation of a Benchling protocol that's ready to
    hand to the program builder. Independent of which Benchling shape
    (Protocol, Workflow, Notebook Entry) produced it."""

    benchling_id: str
    name: str
    description: str
    steps: List[NormalizedStep]
    instruments: List[NormalizedInstrument]
    source_url: str
    tenant: str
    revision: str = ""
