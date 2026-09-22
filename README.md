# Rhylthyme Importers

Import plugins for converting external data sources into Rhylthyme programs.

## Installation

```bash
pip install rhylthyme-importers              # TheMealDB, Spoonacular, protocols.io, Opentrons, Benchling, recipe sites
pip install "rhylthyme-importers[cooklang]"   # + CookLang .cook files
```

Python 3.12 or newer. From a checkout: `pip install -e ./rhylthyme-importers`.

Importers read a URL, an id, or text you pass them. `import_from_url()`
refuses a local path unless the importer was built with
`allow_local_files=True`; the command-line tools do that, a server should not.

## Quickstart: a recipe page to a picture and a live timeline

```bash
pip install rhylthyme            # this package plus the rhylthyme command and the renderer

rhylthyme import https://www.bbcgoodfood.com/recipes/classic-lasagne
#   Importing with recipe-scrapers…
#   Saved easy_classic_lasagne.json

rhylthyme-render easy_classic_lasagne.json -o lasagne.png --style web --palette vivid --color-by task   # needs Node.js
rhylthyme publish easy_classic_lasagne.json --open                                                    # live timeline with timers
```

The same from Python:

```python
from rhylthyme_importers import ImporterRegistry
from rhylthyme_timeline import render

url = "https://www.bbcgoodfood.com/recipes/classic-lasagne"
result = ImporterRegistry.find_for_url(url).import_from_url(url)
assert result.success, result.error
program = result.program                       # Rhylthyme program JSON

svg = render(program, style="web", colorBy="task")
open("lasagne.svg", "w").write(svg)
```

Run `rhylthyme validate` (or the `validate_program` MCP tool) on anything an
importer produces before you rely on the timings: an importer reads what the
source says, and sources are vague about time.

## What is here

Each importer turns one kind of source into a Rhylthyme program: parallel
tracks of timed steps with shared-equipment limits, ready for
`rhylthyme validate`, `rhylthyme publish` or the web app. An importer reads
structure, not meaning, so what it produces is faithful to the source and
mostly sequential; see [Enrichment](#enrichment-is-not-part-of-baseimporter)
for how tracks and cross-track triggers are added afterwards.

| Importer | Source | Input | Needs |
|---|---|---|---|
| [`themealdb`](#themealdb) | TheMealDB, a free recipe API | meal id or URL | nothing |
| [`spoonacular`](#spoonacular) | Spoonacular recipe API | recipe id or URL | `SPOONACULAR_API_KEY` |
| [`recipe-scrapers`](#recipe-sites-recipe-scrapers) | several hundred recipe websites | recipe page URL | nothing |
| [`cooklang`](#cooklang) | CookLang `.cook` files | raw or GitHub URL, or a file | `[cooklang]` extra |
| [`protocolsio`](#protocolsio) | protocols.io | protocol URL or id | `PROTOCOLS_IO_TOKEN` |
| [`opentrons`](#opentrons) | Opentrons Protocol API v2 `.py` | file or pasted source | nothing |
| [`benchling`](#benchling) | Benchling protocols, workflow tasks, notebook entries | protocol URL or id | a Benchling API token |
| [`slidedeck`](#slide-decks) | PowerPoint `.pptx` | file | nothing |

`rhylthyme-import list` prints the registered importers;
`rhylthyme-import import <url>` picks one by URL. With the `rhylthyme`
command installed (`pip install rhylthyme`), the same importers are
`rhylthyme import <url>` and `rhylthyme search <query>`, with validation
and `--publish` on the end.

### TheMealDB

Recipes from [TheMealDB](https://www.themealdb.com/). No key needed.

```bash
rhylthyme-import search "chicken curry" -i themealdb
rhylthyme-import import "https://www.themealdb.com/meal/52772" -o curry.json --pretty
rhylthyme-import mealdb random -o random_meal.json --pretty
rhylthyme-import mealdb categories
```

```python
from rhylthyme_importers import TheMealDBImporter
result = TheMealDBImporter().import_from_url("52772")
```

### Spoonacular

Recipes from [Spoonacular](https://spoonacular.com/food-api), with
structured ingredients, equipment and nutrition. Set `SPOONACULAR_API_KEY`
(free tier available); without it, search returns nothing and imports fail
with a message saying so.

```bash
export SPOONACULAR_API_KEY=...
rhylthyme-import search "pad thai" -i spoonacular
rhylthyme-import import "https://spoonacular.com/recipes/pad-thai-716429" -o pad_thai.json --pretty
```

```python
from rhylthyme_importers import SpoonacularImporter
SpoonacularImporter(api_key="...").import_from_url("716429")
```

### Recipe sites (recipe-scrapers)

Any of the several hundred recipe websites that
[recipe-scrapers](https://github.com/hhursev/recipe-scrapers) understands
(Serious Eats, BBC Good Food, AllRecipes, NYT Cooking, ...). Pass the page
URL; the importer is chosen automatically when the host is supported.

```bash
rhylthyme-import import "https://www.seriouseats.com/the-best-chili-recipe" -o chili.json --pretty
```

`rhylthyme-sweep-supported-sites` checks which of those sites still parse
(for catalogue maintenance).

### CookLang

Individual [CookLang](https://cooklang.org/) `.cook` files, from a raw URL,
a GitHub blob URL (converted to raw automatically) or a local file. Needs the
`cooklang` extra: `pip install "rhylthyme-importers[cooklang]"`.

```bash
rhylthyme-import import "https://github.com/cooklang/cookcli/blob/main/seed/Neapolitan%20Pizza.cook" \
    -o pizza.json --pretty
```

```python
from rhylthyme_importers import CooklangImporter
CooklangImporter().import_from_url("https://raw.githubusercontent.com/cooklang/cookcli/main/seed/Neapolitan%20Pizza.cook")
CooklangImporter(allow_local_files=True).import_from_url("Pizza.cook")
CooklangImporter().import_from_content(open("Pizza.cook").read(), source_name="Pizza")
```

Timers (`~{10%minutes}`) become step durations; ingredients and cookware
land in `metadata`.

#### CookLang Federation (bulk)

Scrape every recipe in the [Cooklang Federation](https://recipes.cooklang.org/),
about 76 GitHub repositories, and filter out placeholders. Reproducible and
idempotent on re-run.

```bash
rhylthyme-import-cooklang-federation --discover                       # 1. list source repos
rhylthyme-import-cooklang-federation --import-all --staged-out /tmp/staged.json   # 2. scrape + filter
rhylthyme-import-cooklang-federation --review /tmp/staged.json        # 3. what passed, what was dropped
rhylthyme-import-cooklang-federation --refilter /tmp/staged.json      # 4. re-apply edited rules, no re-download
```

The staged file is a list of programs; `--upload` sends it to a Rhylthyme
catalogue.

The filter rejects placeholder names (`recipe`, `untitled`, `template`,
`test`, `Copy of ...`), hash-like or all-digit names, and programs with no
ingredients or no steps. Short real names (Roux, Filo, Dahl, Toum) are kept.

### protocols.io

Laboratory protocols from [protocols.io](https://www.protocols.io/). Needs
`PROTOCOLS_IO_TOKEN` (from https://www.protocols.io/developers). Each
protocol step keeps its source sentence on the step, so enrichment can say
which words a step came from.

```bash
export PROTOCOLS_IO_TOKEN=...
rhylthyme-import search "western blot" -i protocolsio
rhylthyme-import import "https://www.protocols.io/view/western-blot-..." -o blot.json --pretty
```

```python
from rhylthyme_importers import ProtocolsIOImporter
ProtocolsIOImporter(access_token="...").import_from_url("https://www.protocols.io/view/...")
```

### Opentrons

An Opentrons Protocol API v2 `.py` file (OT-2 or Flex) becomes a program with
one track per pipette mount or module; durations come from a static lookup
table. Nothing to configure. Full feature matrix in
[`docs/opentrons.md`](docs/opentrons.md).

```bash
rhylthyme-import-opentrons path/to/protocol.py                 # program JSON on stdout
rhylthyme-import-opentrons path/to/protocol.py -o program.json
cat protocol.py | rhylthyme-import-opentrons -                 # from stdin
```

```python
from rhylthyme_importers import OpentronsImporter
OpentronsImporter().import_from_source(open("protocol.py").read())
```

Covers OT-2 and Flex pipettes (1-, 8- and 96-channel), the Flex gripper, and
the heater-shaker, magnetic, temperature, thermocycler and absorbance
modules. `transfer`, `distribute` and `consolidate` expand into the same
low-level events the Opentrons run log emits. A protocol the stubbed
simulator cannot execute falls back to a static AST parse and carries a
`WARNING` event, which the web app shows as a banner.

### Benchling

A Benchling Protocol, Workflow Task or Notebook Entry becomes a program with
an instrument-aware track split. Live import needs a Benchling API token for
the tenant; the web app stores one per user (encrypted) after a one-time
connect, and the MCP server's `import_from_source` with `source: "benchling"`
searches your library and imports by id or URL. Details, field mapping and
the roadmap: [`docs/benchling.md`](docs/benchling.md),
[`docs/benchling-field-mapping.md`](docs/benchling-field-mapping.md).

```python
from rhylthyme_importers.benchling import BenchlingImporter
importer = BenchlingImporter()
importer.search_live(tenant="acme", token="...", query="PCR")
importer.import_from_url("https://acme.benchling.com/acme/protocols/prt_abc123", token="...")
```

The command line works offline on a saved API response, which is how the
importer is tested:

```bash
rhylthyme-import-benchling --from-file protocol_response.json --tenant acme -o program.json
```

### Slide decks

A PowerPoint `.pptx` becomes a presentation timeline: one step per slide,
timed from speaker-notes cues where present.

```python
from rhylthyme_importers import SlideDeckImporter
SlideDeckImporter().import_from_url("talk.pptx")
```

## Other commands

The remaining `rhylthyme-*` commands (`rhylthyme-backfill-taxonomy`,
`rhylthyme-clean-titles`, `rhylthyme-backfill-language`,
`rhylthyme-embed-recipes`, `rhylthyme-detect-text-thumbs`,
`rhylthyme-replace-broken-thumbs`, `rhylthyme-import-recipe-scrapers`,
`rhylthyme-sweep-supported-sites`) maintain the public catalogue on
rhylthyme.com and are not needed to import anything. Their extra
dependencies are the `catalog` extra.

## Enrichment is not part of `BaseImporter`

An importer reads *structure*, not meaning, so it produces one track of
steps chained head-to-tail. Splitting that into parallel tracks with
cross-track triggers takes a language model, and that lives in
**rhylthyme-server**, not here:
`rhylthyme_server._prompting.enrich_program` runs turn 4 of the
`plan_schedule` prompt over the program an importer has already returned,
exposed as `enrich: true` on `POST /api/import` and on the MCP
`import_from_source` tool.

Nothing in this package changes for it. `BaseImporter` has no `enrich`
hook, no Anthropic dependency and no network requirement beyond the
source API it already talks to; importers stay deterministic, offline-
testable and free to run without an `ANTHROPIC_API_KEY`. Enrichment is a
post-processor over `ImportResult.program`, opt-in per request and
rate-limited, and a failed enrichment returns the importer's program
unchanged.

Two conventions make an importer enrich well:

- **Keep the source sentence on the step.** `enrich` reads
  `step["description"]` (falling back to `step["notes"]`) as the step's
  `metadata.sourceSpan.quote`, so the enriched program can say which
  words each step came from. protocols.io's importer already does this.
- **Keep `stepId`s stable.** Enrichment preserves every imported
  `stepId`, and marks any step it adds with `metadata.inferred = true`;
  the diff between the two is how the UI tells read-from-source apart
  from inferred.

## Python API

```python
from rhylthyme_importers import TheMealDBImporter, ProtocolsIOImporter, ImporterRegistry

# TheMealDB
mealdb = TheMealDBImporter()
results = mealdb.search("pasta")
result = mealdb.import_from_url("52771")

if result.success:
    program = result.program
    print(f"Imported: {program['name']}")

# Protocols.io
protocolsio = ProtocolsIOImporter(access_token="your_token")
result = protocolsio.import_from_url("https://www.protocols.io/view/...")

# Auto-detect importer from URL
importer = ImporterRegistry.find_for_url("https://www.themealdb.com/meal/52771")
if importer:
    result = importer.import_from_url("https://www.themealdb.com/meal/52771")
```

## Creating Custom Importers

Extend the `BaseImporter` class to create new importers:

```python
from rhylthyme_importers import BaseImporter, ImportResult, ImporterRegistry

class MyCustomImporter(BaseImporter):
    name = "myimporter"
    description = "Import from my custom source"
    supported_domains = ["example.com"]

    def can_import(self, url_or_query: str) -> bool:
        return "example.com" in url_or_query

    def search(self, query: str) -> list:
        # Return list of {name, url, description}
        return []

    def import_from_url(self, url: str) -> ImportResult:
        # Fetch data and convert to Rhylthyme program
        program = self.create_base_program(
            name="My Program",
            description="Description",
            environment_type="kitchen",
            source_url=url,
            source_type="myimporter"
        )
        # Add tracks, steps, etc.
        return ImportResult(success=True, program=program)

# Register the importer
ImporterRegistry.register(MyCustomImporter())
```

## License

Apache License 2.0
