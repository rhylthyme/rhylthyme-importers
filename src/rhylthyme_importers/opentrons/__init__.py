"""
Opentrons Protocol API (.py) importer.

Public surface:
- :class:`OpentronsImporter` — `BaseImporter` subclass; bytes/file in,
  Rhylthyme program JSON out. The tracer-bullet (Phase 1) surface.

Internal four-module split (see plans/opentrons-importer.md):
- :mod:`.simulator` — primary parser, runs the protocol against a
  stubbed `ProtocolContext` and records `CommandEvent`s.
- :mod:`.ast_parser` — fallback parser, static AST walk; used when
  the simulator can't execute (missing labware defs, etc.).
- :mod:`.duration_model` — pure `CommandEvent → seconds` lookup.
- :mod:`.program_builder` — `[CommandEvent]` → Rhylthyme program JSON.

Each module is independently testable; the importer just wires them.
"""

from ..base import ImporterRegistry
from .events import CommandEvent
from .importer import OpentronsImporter

# Self-register so /api/import (which routes via ImporterRegistry.get(source))
# can look up "opentrons" without each consumer doing it manually. Mirrors the
# pattern in the cooklang / themealdb / protocolsio importers.
ImporterRegistry.register(OpentronsImporter())

__all__ = ['CommandEvent', 'OpentronsImporter']
