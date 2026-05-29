# Rhylthyme Importers

Import plugins for converting external data sources into Rhylthyme programs.

## Installation

```bash
pip install -e ./rhylthyme-importers
```

## Available Importers

### TheMealDB Importer

Import recipes from [TheMealDB](https://www.themealdb.com/) free recipe API.

```bash
# Search for recipes
rhylthyme-import search "chicken curry" -i themealdb

# Import by URL or meal ID
rhylthyme-import import "https://www.themealdb.com/meal/52772" -o curry.json --pretty

# Import a random meal
rhylthyme-import mealdb random -o random_meal.json --pretty

# List available categories
rhylthyme-import mealdb categories
```

### CookLang Importer

Import individual `.cook` recipe files (local paths, raw URLs, or GitHub blob URLs).

```bash
rhylthyme-import import "https://github.com/cooklang/recipes/blob/main/Beef%20Stew.cook" \
    -o beef_stew.json --pretty
```

### CookLang Federation (bulk recipe import)

Bulk-scrape all recipes from the [Cooklang Federation](https://recipes.cooklang.org/),
which aggregates ~76 GitHub recipe repos. Reproducible, idempotent on re-run.

```bash
# 1. List source repos
rhylthyme-import-cooklang-federation --discover

# 2. Scrape + filter, write staged JSON
rhylthyme-import-cooklang-federation --import-all --staged-out /tmp/staged.json

# 3. Spot-check what passed and what was filtered out
rhylthyme-import-cooklang-federation --review /tmp/staged.json

# 4. Re-apply filters after editing rules in the source (no re-download)
rhylthyme-import-cooklang-federation --refilter /tmp/staged.json

# 5. Upload to Supabase. --idempotent skips entries already uploaded
#    (matched by metadata.source.url for the target user).
export SUPABASE_URL=https://xxx.supabase.co
export SUPABASE_SERVICE_ROLE_KEY=...
rhylthyme-import-cooklang-federation --upload /tmp/staged.json --idempotent
```

The filter rejects:
- Generic placeholder names: `recipe`, `untitled`, `template`, `example`,
  `test`, `draft`, `wip`, `Smoke Test Recipe`, `Copy of …`, etc.
- Hash-like or all-digit names; consonant-mash garbage.
- Programs with **zero ingredients** (almost always TODO placeholders) or
  zero steps.

Short real food names (Roux, Filo, Dahl, Toum, Garlic Salt) are kept.

### Opentrons Importer

Import an Opentrons Protocol API v2 `.py` file (OT-2 or Flex) and emit a
Rhylthyme program JSON. Each pipette / module command becomes a step on
a per-mount or per-module track; durations come from a static lookup
table. Full supported-features matrix at
[`docs/opentrons.md`](docs/opentrons.md).

**Quickstart:**

```bash
# Convert a protocol file to a Rhylthyme program (stdout)
rhylthyme-import-opentrons path/to/protocol.py

# Write to a file instead
rhylthyme-import-opentrons path/to/protocol.py -o program.json

# Pipe from stdin
cat path/to/protocol.py | rhylthyme-import-opentrons -
```

Covers OT-2 and Flex pipettes (1ch / 8ch / 96ch), the Flex gripper,
heater-shaker, magnetic, temperature, thermocycler, and absorbance
modules. Helper methods (`transfer`, `distribute`, `consolidate`)
expand into the same low-level event stream the Opentrons run log
emits. Anything the simulator can't execute falls through to a static
AST parse and ships with a `WARNING` event the web upload modal
surfaces as a banner.

### Protocols.io Importer

Import laboratory protocols from [protocols.io](https://www.protocols.io/).

**Requires API token:** Set the `PROTOCOLS_IO_TOKEN` environment variable with your
protocols.io API token (get one from https://www.protocols.io/developers).

```bash
export PROTOCOLS_IO_TOKEN="your_token_here"

# Search for protocols
rhylthyme-import search "western blot" -i protocolsio

# Import a protocol
rhylthyme-import import "https://www.protocols.io/view/western-blot-..." -o protocol.json --pretty
```

## CLI Commands

```bash
# List all available importers
rhylthyme-import list

# Import from any supported URL (auto-detects importer)
rhylthyme-import import <url> [-o output.json] [--pretty]

# Search using a specific importer
rhylthyme-import search <query> -i <importer>
```

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

## Publishing to PyPI

To publish this package to PyPI:

```bash
# Install build tools
pip install build twine

# Build the package
cd rhylthyme-importers
python -m build

# Upload to TestPyPI first (optional)
python -m twine upload --repository testpypi dist/*

# Upload to PyPI
python -m twine upload dist/*
```

You'll need PyPI credentials configured in `~/.pypirc` or use environment variables:
- `TWINE_USERNAME` / `TWINE_PASSWORD` or
- `TWINE_API_TOKEN` (recommended)

## License

Apache License 2.0
