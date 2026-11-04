PY := python

.PHONY: docs docs-check test release

docs:
	$(PY) -m rhylthyme_importers.opentrons.docs_gen
	$(PY) -m rhylthyme_importers.benchling.docs_gen

docs-check:
	$(PY) -m rhylthyme_importers.opentrons.docs_gen --check
	$(PY) -m rhylthyme_importers.benchling.docs_gen --check

test:
	pytest tests/

# make release, or make release VERSION=0.5.0. See scripts/release.sh.
release:
	./scripts/release.sh $(VERSION)
