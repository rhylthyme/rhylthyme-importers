# Benchling field mapping

> **Auto-generated** from `src/rhylthyme_importers/benchling/normalizer.py`
> and `program_builder.py`. Do not edit by hand — run `make docs` from
> `rhylthyme-importers/`.

This document is the contract between Benchling's API payloads and
Rhylthyme's program schema. When the underlying code changes, this
file changes too; if you see drift in CI, run `make docs` and commit
the regenerated file.


## Payload shapes

Benchling exposes three protocol-shaped resources; the normalizer
dispatches by `object_type` (or by structural heuristic when the
field is missing):

| Shape | URL path | Step source |
| --- | --- | --- |
| Protocol | `/api/v2/protocols/{id}` | `protocol.steps[]` |
| Workflow Task | `/api/v2/workflow-tasks/{id}` | `task.steps[]` or `task.procedure` (or 1-step fallback) |
| Notebook Entry | `/api/v2/entries/{id}` | blocks where `block.type == "step"` (incl. inside `days[]`) |

## Step-level mapping

| Benchling source field | Rhylthyme target |
| --- | --- |
| `step.id` | `step.stepId` |
| `step.name` | `step.name` |
| `step.description` | `step.description` |
| `step.duration_seconds` → fallback `step.duration_minutes × 60` → fallback `step.duration.{seconds,minutes}` → fallback parametric in `description` | `step.duration.seconds` (else `indefinite` with `triggerName: complete_<stepId>`) |
| `step.instruments[].id` (or `step.fields.Instrument.value`) | resource constraint + track assignment |
| `step.temperature_c` | reserved (not yet surfaced — Phase 3) |
| `step.predecessor_id` (else declaration-order previous) | `step.startTrigger = afterStep(stepId)` |

## Duration units recognized

| Unit token | Seconds |
| --- | --- |
| `h` | 3600 |
| `hour` | 3600 |
| `hours` | 3600 |
| `hr` | 3600 |
| `hrs` | 3600 |
| `m` | 60 |
| `min` | 60 |
| `mins` | 60 |
| `minute` | 60 |
| `minutes` | 60 |
| `s` | 1 |
| `sec` | 1 |
| `second` | 1 |
| `seconds` | 1 |
| `secs` | 1 |

## Duration patterns (description fallback)

| Order | Pattern (loose) | Example |
| --- | --- | --- |
| 1 | `\bfor\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b` | "incubate for 30 minutes" |
| 2 | `\b(?:incubate|wait|spin|run|hold|rest|chill|cool|heat)\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b` | "spin 30 seconds" |

## Instrument classifier

Used to pick a `task` for a step driven by a given instrument.

| Instrument name (lowercase) | Internal `kind` |
| --- | --- |
| contains 'thermocycler' or 'PCR' | `thermocycler` |
| contains 'centrifuge' or 'spin' | `centrifuge` |
| contains 'incubator' | `incubator` |
| contains 'plate reader' or 'spectrophotometer' | `plate_reader` |
| contains 'shaker' | `shaker` |
| contains 'vortex' | `vortex` |
| contains 'balance' or 'scale' | `balance` |
| anything else | `other` |

## Kind → Rhylthyme task

| Internal `kind` | Rhylthyme `task` |
| --- | --- |
| `balance` | `measurement` |
| `centrifuge` | `centrifugation` |
| `incubator` | `incubation` |
| `other` | `manual` |
| `plate_reader` | `measurement` |
| `shaker` | `mixing` |
| `thermocycler` | `heating` |
| `vortex` | `mixing` |

## Track assignment

- Steps without any instrument → **Bench** track.
- Steps with one or more instruments → first instrument's track.
- Additional instruments on a step still produce resource
  constraints, so multi-instrument blocking is preserved.

## Resource constraints

One entry per distinct instrument, `maxConcurrent: 1`.
Phase 3 will read the tenant's instrument registry to raise the
per-kind cap when multiple copies of the same instrument exist.
