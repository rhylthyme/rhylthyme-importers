# Releasing to PyPI

Bump `version` in `pyproject.toml`, then build from a clean export so stray
local files do not ship, check, upload (the token lives in `~/.pypirc`), tag:

```bash
git archive HEAD | tar -x -C /tmp/rhylthyme-importers-release
python -m build /tmp/rhylthyme-importers-release
python -m twine check /tmp/rhylthyme-importers-release/dist/*
python -m twine upload /tmp/rhylthyme-importers-release/dist/*
git tag v<version> && git push origin v<version>
```

rhylthyme-server installs this package from PyPI, so a fix here reaches
production only after a release and a server redeploy.
