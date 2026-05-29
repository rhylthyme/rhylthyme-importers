# Opentrons importer — architecture (contributor reference)

Audience: anyone adding a command, a module kind, a Flex hardware
feature, or a new entry point. Pairs with
[`opentrons.md`](./opentrons.md) (user reference) and
[`plans/opentrons-importer.md`](../../plans/opentrons-importer.md)
(implementation phasing).

## Four modules, one data contract

```
                ┌────────────────────────┐
   .py source ─►│ simulator              │──┐
                │ (stubbed ProtocolContext)│  │
                └────────────────────────┘  │
                                            ▼
                                  ┌─────────────────┐
                                  │ list[CommandEvent]
                                  └─────────────────┘
                                            ▲
                ┌────────────────────────┐  │
   .py source ─►│ ast_parser             │──┘   (fallback on simulator failure;
                │ (static AST walk)      │       emits a WARNING event)
                └────────────────────────┘

                ┌─────────────────┐
                │ duration_model  │  CommandEvent → seconds (pure lookup)
                └────────┬────────┘
                         │
                         ▼
   list[CommandEvent] ─► program_builder ─► Rhylthyme program JSON
```

The data contract between the four modules is a single dataclass —
[`CommandEvent`](../src/rhylthyme_importers/opentrons/events.py).
Everything else is implementation detail behind that boundary.

```python
@dataclass(frozen=True)
class CommandEvent:
    command_type: str           # 'aspirate' / 'heater_shaker.shake' / 'WARNING' / ...
    index: int                  # monotonically increasing within a single parse
    args: Dict[str, Any]        # free-form per-command kwargs (volume, rpm, ...)
    mount: Optional[str]        # 'left' / 'right' / 'flex_96' / 'gripper' / None
    module_id: Optional[str]    # e.g. 'heater-shaker-slot-7' (None for non-module work)
    line: Optional[int]         # source line, when known
```

Growth here is purely additive — `mount='flex_96'`, `mount='gripper'`,
and `module_id` were each added without touching the duration model
or the AST parser. Don't break this shape: every consumer is fine
reading a new field as a `None` default.

## Per-module responsibilities

### `simulator.py`
Runs the protocol against a stubbed `ProtocolContext`. Each stub class
(pipette / labware / module kind) implements the methods we recognise
and records them via a shared `_Recorder`. Unknown method calls are
chain-returning no-ops (`lambda *a, **kw: self`) so a protocol with one
unsupported step doesn't crash the whole parse.

The full module surface lives in five stub classes:
`_InstrumentStub`, `_HeaterShakerStub`, `_MagneticStub`,
`_TemperatureStub`, `_ThermocyclerStub`, `_AbsorbanceStub`. Each
inherits from `_ModuleBase` (for the module stubs) or uses
`_InstrumentStub` (for pipettes). The `_resolve_module_class` helper
maps Opentrons `load_module(...)` names to the right class.

### `ast_parser.py`
Fallback when the simulator raises. Walks the AST, recognises bare
`thing.method(...)` patterns, emits the corresponding `CommandEvent`
without mount/module awareness. Helper methods (`transfer`, etc.) are
NOT expanded here — the parser can't know runtime arg shapes. A
trailing `WARNING` event is emitted whenever ANY non-setup, non-
recognised call appears so the UI can render its "parsed statically"
banner.

### `duration_model.py`
Two public symbols: the `DURATION_SECONDS` dict (single source of
truth, regenerated into `docs/opentrons.md` via `make docs`) and
`seconds_for(event)`. Special-case rules live in the function:
- `delay` reads `event.args['seconds']`.
- `mix` returns `base × event.args['repetitions']`.
- `thermocycler.execute_profile` reads its precomputed `seconds`
  (computed at parse time from the protocol's step list).

To add a command, add an entry to `DURATION_SECONDS` and update
`_NOTES` in `docs_gen.py` if you want a hint cell.

### `program_builder.py`
Owns the Rhylthyme program-JSON shape. Groups events by `mount` →
pipette tracks, by `module_id` → module tracks, falls everything else
onto a shared `Protocol` track. Emits one `resourceConstraints` entry
per pipette mount AND per module, except when a 96-channel pipette is
loaded (then a single shared gantry constraint replaces the per-mount
ones — see `flex_96_present` in `build_program`).

To add a new track type, extend `_track_label` and add the mount-
ordering rank to `_mount_order`.

### `importer.py`
Thin wrapper. Tries the simulator, falls back to AST, threads the
mount → model map into the builder so track labels can carry pipette
models (`Left: P300 single-channel`). Public surface is just
`OpentronsImporter` (a `BaseImporter` subclass) plus the helpers it
needs.

## Data flow for a typical protocol

1. CLI / web upload / MCP hands source bytes to
   `OpentronsImporter.import_from_source(source)`.
2. The importer runs the simulator. The simulator's stubs record
   every recognised method call into a flat `list[CommandEvent]`.
   Setup calls (load_labware / load_instrument / load_module) emit
   nothing.
3. If the simulator raises, the AST parser runs against the same
   source and emits the subset it can statically recognise. A
   `WARNING` event is prepended so downstream consumers (and the UI)
   know.
4. The importer reverse-engineers mount → model mappings from the
   source via a tiny regex (`_extract_mounts`) so the builder can
   render model-decorated track labels even if no events fired on a
   given mount.
5. The builder groups events by mount + module_id, looks up each
   event's duration via `seconds_for`, and emits the program JSON.
6. The result rides back as an `ImportResult` — the same shape every
   importer uses.

## Adding a new command

1. **Simulator**: add a method on the relevant stub class that emits
   a `CommandEvent` with the new `command_type`. The simplest pattern
   is `_emit('foo.bar')`; for module stubs use `_emit(...)` on the
   shared base class.
2. **AST parser**: add the method name to `_PIPETTE_COMMANDS` or
   `_MODULE_COMMANDS_BY_KIND`. If the name is unique across modules,
   it lands in `_MODULE_METHOD_TO_TYPE` automatically.
3. **Duration model**: add a `DURATION_SECONDS[command_type]` entry.
   Optionally add a note in `docs_gen.py::_NOTES`.
4. **Program builder**: if the new command needs a non-default task
   slug, add it to `_MODULE_TASKS` (or extend `_task_for`). Most
   pipette-side additions need no builder changes.
5. **Tests**: add a `tests/test_opentrons_importer.py` case asserting
   the simulator emits the expected event and (if user-visible) the
   builder renders it as a step.
6. **Docs**: run `make docs` to regenerate the duration table. Update
   the user-facing matrix in `docs/opentrons.md` to describe the new
   command.

## Adding a new module kind

1. Subclass `_ModuleBase` in `simulator.py` with the methods that
   matter. Each method calls `self._emit(...)`.
2. Add an entry to `_resolve_module_class` matching the Opentrons
   `load_module(...)` name.
3. Add the method names to `_MODULE_COMMANDS_BY_KIND` in the AST
   parser, and bump `_MODULE_TASKS` in the builder so each new
   command_type maps to the right task slug.
4. Add entries to `DURATION_SECONDS` for every command_type.
5. Add a fixture under `tests/fixtures/opentrons/` exercising the
   new module + a small test class in `test_opentrons_importer.py`.

## CI guardrails

- `make docs-check` regenerates the duration-model table into
  `docs/opentrons.md` and exits non-zero if the file changes. Wire
  this into CI so doc drift fails the build.
- The importer test suite covers every recognised
  command + entry point. Don't merge a change that removes coverage
  without replacing it.
