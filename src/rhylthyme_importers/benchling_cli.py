"""``rhylthyme-import-benchling`` — CLI front for :class:`BenchlingImporter`.

Phase 1 (tracer bullet): only the ``--from-file`` mode is wired up. It
reads a saved Benchling Protocol API response (JSON) and writes the
resulting Rhylthyme program JSON to stdout, or to a file when
``--output`` is given.

Phase 3 will add live HTTP mode (``--tenant`` + ``--token`` +
``--protocol`` to fetch from a real Benchling tenant). The CLI surface
stays the same — only the data source changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .benchling import BenchlingImporter


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--from-file",
        metavar="PATH",
        help="path to a saved Benchling Protocol API JSON response "
             "(Phase 1 mode — required until live HTTP lands in Phase 3)",
    )
    p.add_argument(
        "--tenant",
        help="Benchling tenant subdomain (e.g. 'acme' for acme.benchling.com). "
             "Used to set the source URL in the emitted program's metadata; "
             "defaults to 'demo' when omitted.",
    )
    p.add_argument(
        "--token",
        help="Benchling API token (Phase 3 — currently ignored)",
    )
    p.add_argument(
        "--protocol",
        help="Benchling protocol id to fetch (Phase 3 — currently ignored)",
    )
    p.add_argument(
        "-o", "--output",
        help="write JSON to this path instead of stdout",
    )
    p.add_argument(
        "--indent", type=int, default=2,
        help="JSON indent level (default 2; pass 0 for a compact dump)",
    )
    args = p.parse_args()

    if not args.from_file:
        sys.exit(
            "error: Phase 1 only supports --from-file. "
            "Live HTTP mode (--tenant + --token + --protocol) lands in Phase 3.",
        )

    importer = BenchlingImporter()
    tenant = args.tenant or "demo"
    result = importer.import_from_file(args.from_file, tenant=tenant)
    if not result.success:
        sys.exit(f"error: {result.error}")

    payload = json.dumps(result.program, indent=args.indent or None)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
