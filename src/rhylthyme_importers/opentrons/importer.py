"""
:class:`OpentronsImporter` — `BaseImporter` subclass wrapping the four
modules into a one-shot ``ImportResult`` for the CLI / web / MCP entry
points.

Detection is by file extension (``.py``) plus a content heuristic: the
source must define a ``run(`` function (the Opentrons protocol entry
point). This is conservative — a plain Python script won't be mistaken
for a protocol.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import BaseImporter, ImportResult
from .ast_parser import parse as ast_parse
from .events import CommandEvent
from .program_builder import build_program
from .simulator import SimulatorError, parse as sim_parse


_RUN_DEF = re.compile(r'^\s*def\s+run\s*\(', re.MULTILINE)
_METADATA_NAME = re.compile(
    r"metadata\s*=\s*\{[^}]*['\"]protocolName['\"]\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)


def _looks_like_protocol(source: str) -> bool:
    return bool(_RUN_DEF.search(source))


def _extract_protocol_name(source: str) -> Optional[str]:
    m = _METADATA_NAME.search(source)
    return m.group(1) if m else None


class OpentronsImporter(BaseImporter):
    """Convert an Opentrons Protocol API v2 ``.py`` file into a
    Rhylthyme program. Tracer-bullet implementation for Phase 1; later
    phases extend the four underlying modules in lockstep."""

    name = 'opentrons'
    description = 'Import an Opentrons Protocol API v2 (.py) file'
    supported_domains: List[str] = []  # file-based, no URL surface

    def __init__(self, allow_local_files: bool = False):
        # import_from_url() reads a LOCAL path. That is for the command line
        # and for files this process wrote itself (an upload); an importer
        # reachable from user input gets the source as text instead.
        self.allow_local_files = allow_local_files

    def can_import(self, url_or_query: str) -> bool:
        if not isinstance(url_or_query, str):
            return False
        return url_or_query.endswith('.py')

    def search(self, query: str) -> List[Dict[str, Any]]:
        # Opentrons protocols don't have a discoverable index. Search is
        # a no-op; the entry-point paths take a file or content directly.
        return []

    def import_from_url(self, url: str) -> ImportResult:
        if not getattr(self, 'allow_local_files', False):
            return ImportResult(
                success=False,
                error='local files are not accepted here; pass the protocol source as text',
            )
        path = Path(url)
        if not path.exists():
            return ImportResult(success=False, error=f'no such file: {url}')
        return self.import_from_source(
            source=path.read_text(encoding='utf-8'),
            filename=str(path),
        )

    def import_from_source(
        self,
        source: str,
        *,
        filename: str = '<protocol>',
    ) -> ImportResult:
        """Primary entry point — bytes/string in, ``ImportResult`` out.

        Tries the stubbed simulator first; on failure falls back to the
        AST parser and tags the result with a ``WARNING`` event so the
        UI can surface "parsed statically; some steps may be missing."
        """
        if not _looks_like_protocol(source):
            return ImportResult(
                success=False,
                error='source does not define run(); not an Opentrons protocol',
            )

        events: List[CommandEvent]
        model_by_mount: Dict[str, str] = {}
        channels_by_mount: Dict[str, int] = {}
        try:
            events = sim_parse(source, filename=filename)
            # Reverse-engineer mount → model/channels from the recorded
            # events. The first event on each mount carries enough info
            # because the simulator only emits events that came from an
            # _InstrumentStub, which captured its model + channels at
            # load_instrument time. We expose those via a side-channel
            # in args when the simulator records the first event of each
            # mount; missing keys fall back to defaults in the builder.
            #
            # The simulator stores ``model``/``channels`` on the stub
            # rather than the event for forward-compat; the builder will
            # accept an empty map and just render bare side names.
            model_by_mount, channels_by_mount = _extract_mounts(source)
        except SimulatorError as e:
            events = ast_parse(source, filename=filename)
            events = [
                CommandEvent(
                    command_type='WARNING',
                    index=-1,
                    args={'message': f'simulator failed: {e}; AST fallback used'},
                ),
                *events,
            ]

        name = _extract_protocol_name(source) or 'Imported Opentrons protocol'
        program = build_program(
            events,
            name=name,
            description='',
            model_by_mount=model_by_mount,
            channels_by_mount=channels_by_mount,
        )
        return ImportResult(
            success=True,
            program=program,
            source_type='opentrons',
            source_url=filename,
        )


# ---- Mount discovery ----------------------------------------------------

_LOAD_INSTRUMENT = re.compile(
    r'load_instrument\s*\(\s*[\'"]([^\'"]+)[\'"]\s*,\s*[\'"](left|right)[\'"]',
)


def _extract_mounts(source: str) -> tuple[Dict[str, str], Dict[str, int]]:
    """Lift mount → model/channels mappings directly from the source
    text. We do this here (rather than from the simulator's event
    stream) so the builder gets useful track labels even when no events
    were emitted on a given mount.

    Flex 96-channel pipettes physically occupy both mount slots; we
    rewrite the recorded mount to ``flex_96`` so the builder renders a
    dedicated track and emits the shared gantry resource constraint.
    """
    from .simulator import _channels_from_model
    model_by_mount: Dict[str, str] = {}
    channels_by_mount: Dict[str, int] = {}
    for m in _LOAD_INSTRUMENT.finditer(source):
        model, mount = m.group(1), m.group(2)
        channels = _channels_from_model(model)
        key = 'flex_96' if channels == 96 else mount
        model_by_mount[key] = model
        channels_by_mount[key] = channels
    return model_by_mount, channels_by_mount
