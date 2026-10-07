"""Export MusicXML scores to PDF or MIDI with MuseScore's command line.

Usage:  uv run python scripts/export.py pdf|midi <file.musicxml> ...

Each file is written to exports/<name>.pdf or exports/<name>.mid, replacing an earlier export.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from run_batch import ROOT, mscore_bin, musescore_convert

EXPORTS = ROOT / "exports"
FORMATS = {"pdf": ".pdf", "midi": ".mid"}


def export(xml: Path, fmt: str, dst_dir: Path = EXPORTS) -> Path:
    """Write xml as exports/<name>.<ext> and return that path."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; use {' or '.join(FORMATS)}")
    if not xml.is_file():
        raise ValueError(f"no such file: {xml.name}")
    mscore = mscore_bin()
    if not mscore:
        raise ValueError("MuseScore's command line (mscore) was not found")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{xml.stem}{FORMATS[fmt]}"
    dst.unlink(missing_ok=True)
    result = musescore_convert(mscore, xml, dst)
    if result != "yes" or not dst.exists():
        raise ValueError(f"MuseScore could not export {xml.name}: {result}")
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("format", choices=sorted(FORMATS))
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    failed = False
    for f in args.files:
        try:
            print(export(Path(f), args.format))
        except ValueError as e:
            print(e, file=sys.stderr)
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
