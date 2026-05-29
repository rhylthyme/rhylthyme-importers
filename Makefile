PY := python

.PHONY: docs docs-check test

docs:
	$(PY) -m rhylthyme_importers.opentrons.docs_gen
	$(PY) -m rhylthyme_importers.benchling.docs_gen

docs-check:
	$(PY) -m rhylthyme_importers.opentrons.docs_gen --check
	$(PY) -m rhylthyme_importers.benchling.docs_gen --check

test:
	pytest tests/
