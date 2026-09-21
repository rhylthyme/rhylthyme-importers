"""
``rhylthyme-import-opentrons`` — CLI front for :class:`OpentronsImporter`.

Reads a single Opentrons Protocol API v2 ``.py`` file (or stdin via ``-``)
and writes the resulting Rhylthyme program JSON to stdout, or to a file
when ``--output`` is given.

Tracer-bullet implementation. Phase 2+ extends the underlying importer;
the CLI surface stays the same.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .opentrons import OpentronsImporter


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        'protocol',
        help='path to an Opentrons Protocol API v2 .py file, or "-" for stdin',
    )
    p.add_argument(
        '-o', '--output',
        help='write JSON to this path instead of stdout',
    )
    p.add_argument(
        '--indent', type=int, default=2,
        help='JSON indent level (default 2; pass 0 for a compact dump)',
    )
    args = p.parse_args()

    if args.protocol == '-':
        source = sys.stdin.read()
        filename = '<stdin>'
    else:
        path = Path(args.protocol)
        if not path.exists():
            sys.exit(f'error: no such file: {args.protocol}')
        source = path.read_text(encoding='utf-8')
        filename = str(path)

    importer = OpentronsImporter(allow_local_files=True)  # a command-line tool reads the file it is given
    result = importer.import_from_source(source, filename=filename)
    if not result.success:
        sys.exit(f'error: {result.error}')

    payload = json.dumps(result.program, indent=args.indent or None)
    if args.output:
        Path(args.output).write_text(payload + '\n', encoding='utf-8')
    else:
        print(payload)


if __name__ == '__main__':
    main()
