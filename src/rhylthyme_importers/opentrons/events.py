"""
Data contract between the four Opentrons modules.

``CommandEvent`` is the single shape that flows simulator/AST-parser →
duration model → program builder. It's deliberately small and additive:
later phases extend it with new fields (labware, well, volume, channel
count) without breaking earlier consumers.

The ``command_type`` namespace mirrors what an Opentrons protocol calls
on its instrument / module objects. For Phase 1 we recognise only the
minimum that's needed to produce a valid Rhylthyme schedule from a
trivial protocol:

- ``pickup_tip``
- ``drop_tip``
- ``aspirate``
- ``dispense``
- ``WARNING`` — emitted by the AST fallback when it can't recognise a
  statement; carried through to the program builder so the UI can
  surface "parsed statically" hints.

Later phases add ``mix``, ``move_to``, ``transfer`` (and its expansion),
``delay``, ``pause``, all module commands, and Flex-specific commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CommandEvent:
    """One recorded call from an Opentrons protocol.

    Attributes:
        command_type: lowercase string identifying the call (see module
            docstring for the Phase 1 vocabulary).
        index: monotonically-increasing event index, set by the parser.
            Used by the program builder to pin step ordering.
        args: free-form key/value bag for command-specific metadata.
            Phase 1 doesn't read any of these — they're recorded so
            later phases (which DO care about volume / mount / labware)
            can extend the model without changing the parser.
        mount: ``'left'`` / ``'right'`` / ``'gripper'`` / ``None``. The
            program builder uses this to assign the event to the right
            track. Modules carry ``None`` here and set ``module_id``.
        module_id: stable identifier for a loaded module (e.g.
            ``'heater-shaker-7'``). Phase 1 has none of these; here for
            forward compatibility.
        line: source-line number when known. Surfaced in error messages.
    """

    command_type: str
    index: int
    args: Dict[str, Any] = field(default_factory=dict)
    mount: Optional[str] = None
    module_id: Optional[str] = None
    line: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable form for test fixtures + debug logging."""
        return {
            'command_type': self.command_type,
            'index': self.index,
            'args': dict(self.args),
            'mount': self.mount,
            'module_id': self.module_id,
            'line': self.line,
        }
