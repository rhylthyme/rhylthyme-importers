# Opentrons importer — user reference

End-user documentation for the Opentrons Protocol API (.py) importer.
Contributor / architecture notes live in
[`opentrons-architecture.md`](./opentrons-architecture.md).

## Quickstart — try it in 30 seconds

Save the following as `trivial.py` (a working 4-step Opentrons protocol):

```python
metadata = {
    'protocolName': 'Trivial single-aspirate-and-dispense',
    'apiLevel': '2.13',
}

def run(protocol):
    tips = protocol.load_labware('opentrons_96_tiprack_300ul', 1)
    plate = protocol.load_labware('corning_96_wellplate_360ul_flat', 2)
    pipette = protocol.load_instrument('p300_single_gen2', 'left',
                                       tip_racks=[tips])

    pipette.pick_up_tip()
    pipette.aspirate(100, plate['A1'])
    pipette.dispense(100, plate['B1'])
    pipette.drop_tip()
```

Three ways to import it — pick whichever fits:

### 1. Web app (no install)

1. Go to [kitchen.rhylthyme.com](https://kitchen.rhylthyme.com/).
2. In the left sidebar, expand **Upload Program**.
3. Drag your `trivial.py` onto the drop zone (or click the zone and pick
   the file). `.py` files are listed alongside `.json`, `.yaml`,
   `.pptx`, and `.cook` as accepted formats.
4. The visualization appears in the main pane: one
   **Left: P300 single-channel** track with four steps —
   `Pickup Tip → Aspirate → Dispense → Drop Tip`.

### 2. CLI

```bash
pip install rhylthyme-importers
rhylthyme-import-opentrons trivial.py | jq .
```

Prints the same program JSON the web upload renders.

### 3. MCP (Claude Desktop and other agents)

Once the Rhylthyme MCP connector is wired into your client, call the
`import_from_source` tool with:

```json
{ "source": "opentrons", "action": "import", "text": "<your .py source>" }
```

Pass a public `.py` URL via `"query": "<url>"` instead of `"text"` if
you'd rather not paste the source.

Larger example protocols live in the [worked examples](#worked-examples)
section below — those `.py` files double as the importer's golden-fixture
test inputs, so they stay in sync with the code.

## Supported features

The importer parses a protocol's `run(protocol)` body and emits one
Rhylthyme step per recognised method call. Anything the simulator can't
execute falls through to a static AST parser, which is more conservative
(no helper expansion, no mount/module awareness) but lets you import
protocols that reference labware definitions the importer doesn't ship.

Status: ✅ supported · 🚧 partial (AST-only or unexpanded) · ⏳ planned.

### Pipettes & mounts

| Hardware | Status | Notes |
|---|---|---|
| OT-2 single-channel (P20/P50/P300/P1000) | ✅ | track label = `<Side>: P<vol> single-channel` |
| OT-2 8-channel (`*_multi*`) | ✅ | track label = `<Side>: P<vol> 8-channel` |
| Flex 1-channel (`flex_1channel_*`) | ✅ | track label = `<Side>: Flex 1-channel <vol>` |
| Flex 8-channel (`flex_8channel_*`) | ✅ | track label = `<Side>: Flex 8-channel <vol>` |
| Flex 96-channel (`flex_96channel_1000`) | ✅ | dedicated `Flex 96-channel` track; emits a shared gantry resource constraint (`maxConcurrent: 1` over all `pipetting` tasks) so it cannot run concurrently with another mount pipette |
| Flex gripper (`protocol.move_labware(..., use_gripper=True)`) | ✅ | dedicated `Flex Gripper` track + a `measurement`-task constraint with `maxConcurrent: 1`. Manual `move_labware()` without `use_gripper=True` renders as a pause prompt on the Protocol track. |
| Two-mount protocols | ✅ | two parallel tracks, each with `maxConcurrent: 1` |

### Pipette commands

Each row maps to one `CommandEvent` per call.

| Method | Status | Notes |
|---|---|---|
| `pick_up_tip` / `pickup_tip` | ✅ | normalised to `pickup_tip` |
| `drop_tip` | ✅ | |
| `return_tip` | ✅ | rendered as `drop_tip` |
| `aspirate` | ✅ | |
| `dispense` | ✅ | |
| `mix` | ✅ | duration scales by `repetitions` |
| `move_to` | ✅ | |
| `air_gap` | ✅ | |
| `blow_out` | ✅ | |
| `touch_tip` | ✅ | |
| `home` (pipette) | ✅ | |
| `transfer` | ✅ | expanded to `pickup_tip → (aspirate → dispense)* → drop_tip`; honours `new_tip="always"/"once"/"never"`; broadcasts across list args |
| `distribute` | ✅ | expanded to `pickup_tip → aspirate → dispense* → drop_tip` |
| `consolidate` | ✅ | expanded to `pickup_tip → aspirate* → dispense → drop_tip` |

### Protocol-level

| Method | Status | Notes |
|---|---|---|
| `protocol.load_labware(...)` | ✅ | setup, emits no step |
| `protocol.load_adapter(...)` | ✅ | setup, emits no step |
| `protocol.load_instrument(...)` | ✅ | setup, emits no step |
| `protocol.load_module(...)` | ✅ | setup, emits no step (module commands run on the module's own track — see below) |
| `protocol.delay(seconds=N, minutes=M, msg=...)` | ✅ | renders as a fixed-duration step on the `Protocol` track; `task: delay` |
| `protocol.pause(msg=...)` | ✅ | renders as an indefinite-duration step on the `Protocol` track |
| `protocol.home()` | ✅ | renders on the `Protocol` track |
| `protocol.comment(...)` / `protocol.set_rail_lights(...)` etc. | 🚧 | silently no-op (not work) |

### Modules

Each loaded module gets its own track + a `maxConcurrent: 1` resource
constraint. Module commands map to the same task vocabulary as the
protocols.io importer (`heating`, `incubation`, `mixing`, `measurement`).

| Module | Status | Recognised commands |
|---|---|---|
| Heater-shaker (`heaterShakerModuleV1`) | ✅ | `set_and_wait_for_shake_speed`, `set_target_temperature`, `wait_for_temperature`, `deactivate_shaker`, `deactivate_heater`, `open_labware_latch`, `close_labware_latch` |
| Magnetic module v1 / v2 / block (`magneticModuleV2`, `magneticBlockV1`, etc.) | ✅ | `engage`, `disengage` |
| Temperature module (`temperatureModuleV1` / `V2`) | ✅ | `set_temperature`, `await_temperature`, `deactivate` |
| Thermocycler (`thermocyclerModuleV1` / `V2`) | ✅ | `open_lid`, `close_lid`, `set_block_temperature`, `set_lid_temperature`, `execute_profile`, `deactivate` |
| Absorbance module (Flex, `absorbanceReaderV1`) | ✅ | `initialize`, `read`, `open_lid`, `close_lid` |

**Background timing model.** Module commands run on their own track so
the pipette track can continue in parallel — the visualiser shows the
robot doing other work during a shake or thermocycler profile. The
duration of `thermocycler.execute_profile(steps=..., repetitions=N)` is
computed exactly: `sum(step.hold_time_seconds) * N`. Set-and-wait /
deactivate commands fall back to static estimates; later phases may
extend the model with volume / temperature awareness.

### Entry points

| Surface | Status | Notes |
|---|---|---|
| CLI: `rhylthyme-import-opentrons <file.py>` | ✅ | reads file or stdin (`-`), writes Rhylthyme program JSON to stdout |
| Web upload modal accepts `.py` | ✅ | `/api/upload` detects `.py` extension and routes through `OpentronsImporter`; structured error in modal UI on parse failure |
| MCP `import_from_source` opentrons variant | ✅ | Python local + Node remote servers both accept `source="opentrons"`; pass `query=<URL>` for a hosted `.py`, OR `text=<source>` for inline source. |

## Worked examples

The four files below are *both* the golden-fixture inputs for the
test suite *and* the worked-example reference protocols for these
docs — single source of truth. CI fails when one exists without
the other.

Each fixture lives at
`rhylthyme-importers/tests/fixtures/opentrons/<name>.py`. Run the
CLI on any of them to see exactly what an import produces:

```bash
rhylthyme-import-opentrons rhylthyme-importers/tests/fixtures/opentrons/<name>.py | jq .
```

| Fixture | Phase | What it exercises |
|---|---|---|
| `trivial` | 1 | Single-mount OT-2 pipette with the four core commands: `pickup_tip → aspirate → dispense → drop_tip`. |
| `pcr_setup` | 2 | OT-2 single-channel pipette, `mix(repetitions=3)`, `protocol.delay(seconds=30)`, `protocol.pause("...")`, and the `Protocol` track for protocol-level steps. |
| `serial_dilution` | 2 | Two-mount protocol: 8-channel `distribute` + per-column transfers on the left, single-channel `transfer` on the right. Demonstrates the simulator's helper-method expansion. |
| `elisa` | 3 | Four module families on one program: heater-shaker, magnetic, temperature, absorbance. Each gets its own track and a `maxConcurrent: 1` resource constraint. |
| `cell_culture_passage` | 4 | Flex 96-channel pipette (own track + shared gantry constraint), Flex 1-channel on the right mount, and `gripper.move_labware(..., use_gripper=True)` driving a dedicated `Flex Gripper` track. |

## Troubleshooting (error catalog)

Every error string the importer can emit, with the most likely cause
and how to fix it.

| Error | Cause | Fix |
|---|---|---|
| `source does not define run(); not an Opentrons protocol` | The uploaded `.py` is a plain script or library file — Opentrons protocols must define a top-level `def run(protocol):` function. | Wrap the body in `def run(protocol): ...`, or upload a file that actually is a protocol. |
| `syntax error at line N: <msg>` | The protocol's Python source has a `SyntaxError` so neither parser can compile it. | Open the file in your editor / run `python -c "import ast; ast.parse(open('foo.py').read())"` to find the bad line. |
| `protocol module import failed: <reason>` | The simulator could compile the module but executing its top level raised (typically a missing import the stub doesn't fake). | Remove the offending top-level statement, or wrap it in `if __name__ == '__main__':` so it only runs when called as a script — the importer only needs `run()`. |
| `protocol has no callable run(protocol) function` | A `run` symbol exists but isn't callable (e.g. `run = "something"`). | Make sure the protocol defines `def run(protocol):`. |
| `protocol run() raised <Type>: <msg>` | The simulator caught an exception inside `run()`. The AST fallback should fire and emit a `WARNING` instead — if it didn't, the importer surfaces this error so the user knows what went wrong. | Fix the underlying issue (often unsupported labware that the stub couldn't fake), or rely on the AST fallback by ensuring at least one `pipette.method(...)` call is statically visible. |
| `no such file: <path>` | `import_from_url(path)` was given a path that doesn't exist on disk. | Pass a correct path, or use `import_from_source(text)` with the file contents inline. |
| `Invalid file type. Use .json, .yaml, .yml, .pptx, .cook, or .py` | `/api/upload` got a file with an unsupported extension. | Rename your file to `.py` (for Opentrons protocols) or use one of the listed extensions. |
| `Either url/query or text required` | `/api/import` was called for `source='opentrons'` without either `query=<URL>` or `text=<source>`. | Pass one of the two. |

Any of the above errors are *importer-side* — the importer never gets
to the program builder when they fire. Once the import succeeds you
might still see a soft warning surfaced in the web UI:

> ⚠️ This protocol was parsed statically; some steps may be missing.

That means the simulator hit something it couldn't execute and the
AST fallback ran. The visualization is real but conservative — helper
methods (`transfer` / `distribute` / `consolidate`) appear as single
steps instead of expanded into low-level pipette calls. To get full
fidelity, run the protocol against the simulator locally
(`rhylthyme-import-opentrons <file>` will print the simulator error
on stderr) and fix the issue.

## Limitations

- **API version**: Protocol API v2 only. v1 (legacy) and Protocol
  Designer JSON exports are out of scope. See the PRD for rationale.
- **Duration estimates** are static lookups, not volume- or
  distance-aware. Expect ±20% vs. wall-clock for now; the model lives
  behind a one-function seam, so a more accurate model can be swapped
  in without touching the parser or builder.
- **AST fallback** is conservative: helper methods (`transfer`,
  `distribute`, `consolidate`) appear as single events (not expanded),
  and the parser can't tell which mount a pipette is on. Any AST-parsed
  result carries a `WARNING` event the web UI surfaces as a banner.
- **Dynamic control flow** that depends on runtime values (e.g.
  conditionals reading a CSV during `run()`) is handled best-effort by
  the simulator's stubbed labware (which returns sensible defaults so
  the protocol doesn't crash). When that's not enough, the AST fallback
  kicks in.

## Duration model reference (auto-generated)

_Regenerate with `make docs` after editing `DURATION_SECONDS` in `opentrons/duration_model.py`._

<!-- BEGIN AUTOGENERATED: duration-model -->

| Command type | Seconds | Notes |
|---|---:|---|
| `absorbance.close_lid` | 6 |  |
| `absorbance.initialize` | 30 |  |
| `absorbance.open_lid` | 6 |  |
| `absorbance.read` | 45 |  |
| `air_gap` | 3 | Pull a small air buffer into the tip. |
| `aspirate` | 4 | Single-well aspirate; ignores volume in the static model. |
| `blow_out` | 3 | Force remaining liquid out of the tip. |
| `consolidate` | 22 | Helper-method fallback (AST path only). |
| `delay` | 0 | Reads ``event.args["seconds"]`` from the protocol. |
| `dispense` | 4 | Single-well dispense. |
| `distribute` | 22 | Helper-method fallback (AST path only). |
| `drop_tip` | 4 | Drop tip into trash. |
| `gripper.move_labware` | 15 | One labware traverse on the Flex deck. |
| `heater_shaker.close_labware_latch` | 6 |  |
| `heater_shaker.deactivate_heater` | 8 |  |
| `heater_shaker.deactivate_shaker` | 8 |  |
| `heater_shaker.open_labware_latch` | 6 |  |
| `heater_shaker.set_target_temperature` | 8 |  |
| `heater_shaker.shake` | 60 | Default placeholder; protocols typically gate shake duration with a follow-up call. |
| `heater_shaker.wait_for_temperature` | 60 |  |
| `home` | 12 | Send pipette / gantry to home. |
| `magnetic.disengage` | 6 |  |
| `magnetic.engage` | 30 | Settle-time estimate; tune for your bead chemistry. |
| `mix` | 6 | Per repetition; total = base × ``repetitions``. |
| `move_to` | 8 | Move the pipette to a named location. |
| `pause` | 0 | Renders as indefinite duration / manual trigger. |
| `pickup_tip` | 6 | Tip pickup (any pipette). |
| `temperature.await_temperature` | 120 | Block-warm-up estimate; varies by setpoint. |
| `temperature.deactivate` | 6 |  |
| `temperature.set_temperature` | 6 |  |
| `thermocycler.close_lid` | 30 |  |
| `thermocycler.deactivate` | 6 |  |
| `thermocycler.execute_profile` | 0 | Computed at parse time: ``sum(step.hold_time_seconds) * repetitions``. |
| `thermocycler.open_lid` | 30 |  |
| `thermocycler.set_block_temperature` | 60 |  |
| `thermocycler.set_lid_temperature` | 60 |  |
| `touch_tip` | 3 | Brush the tip against the well rim. |
| `transfer` | 18 | Helper-method fallback (AST path only). |

<!-- END AUTOGENERATED: duration-model -->
