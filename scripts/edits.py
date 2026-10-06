"""Edit results and full scores in MuseScore Studio, and bring the edits back.

A result <name> is edited as edits/<name>.mscz, a full score as scores/<name>.mscz: a MuseScore
file made from its MusicXML when it is first opened, which you save in place in MuseScore (⌘S).
sync() converts every MuseScore file saved since its last sync back into the MusicXML and
re-renders it, so concatenation and export use the edited version. An edited result's row gets
the status "edited", and a batch run keeps the edit rather than HOMR's new output (HOMR's own
file stays in work/).

Modification times tell the states apart: after making or syncing a MuseScore file its MusicXML
gets the same mtime, so a newer .mscz has been saved since, and a newer .musicxml was replaced
since (a new import or concatenation), which makes the MuseScore file stale: it is made again.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import run_batch as rb
from combine import keep_sources, retitle, sources_of

EDITS = rb.ROOT / "edits"
SCORES = rb.ROOT / "scores"
SYNCING = threading.Lock()


def mscz_for(xml: Path) -> Path:
    return (SCORES if xml.parent == SCORES else EDITS) / f"{xml.stem}.mscz"


def same_mtime(xml: Path, mscz: Path) -> None:
    mtime = mscz.stat().st_mtime_ns
    os.utime(xml, ns=(mtime, mtime))


def convert(mscore: str, src: Path, dst: Path) -> None:
    if (result := rb.musescore_convert(mscore, src, dst)) != "yes":
        raise ValueError(f"MuseScore could not convert {src.name}: {result}")


def ensure_mscz(xml: Path, mscore: str) -> Path:
    """xml's MuseScore file, made (again) from xml if missing or stale."""
    if not xml.is_file():
        raise ValueError(f"no such file: {xml.name}")
    mscz = mscz_for(xml)
    if not mscz.exists() or mscz.stat().st_mtime_ns < xml.stat().st_mtime_ns:
        mscz.parent.mkdir(exist_ok=True)
        mscz.unlink(missing_ok=True)
        convert(mscore, xml, mscz)
        same_mtime(xml, mscz)  # nothing saved in MuseScore yet
    return mscz


def open_in_musescore(xml: Path) -> Path:
    """Open xml's MuseScore file in MuseScore Studio, making it first if needed. Returns its path."""
    mscore = rb.mscore_bin()
    if not mscore:
        raise ValueError("MuseScore's command line (mscore) was not found")
    mscz = ensure_mscz(xml, mscore)
    app = next((a for a in rb.MUSESCORE_APPS if Path(a).exists()), None)
    if sys.platform == "darwin" and app:
        p = subprocess.run(["open", "-a", app, str(mscz)], capture_output=True, text=True)
        if p.returncode:
            raise ValueError(f"could not open MuseScore: {p.stderr.strip()}")
    else:
        subprocess.Popen([mscore, str(mscz)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return mscz


def saved_since_sync(mscz: Path, xml: Path) -> bool:
    return mscz.exists() and (not xml.exists() or mscz.stat().st_mtime_ns > xml.stat().st_mtime_ns)


def apply_edit(row: dict, mscore: str) -> dict:
    """The result row with its MusicXML and render made from edits/<name>.mscz."""
    name = row["output_name"]
    mscz, xml = EDITS / f"{name}.mscz", rb.OUTPUTS / f"{name}.musicxml"
    convert(mscore, mscz, xml)
    output = str(xml.relative_to(rb.ROOT))
    musescore = rb.check_musescore(mscore, output, name, rb.REVIEW / "renders")
    same_mtime(xml, mscz)
    return dict(row, status="edited", output=output, musescore=musescore)


def sync_score(mscz: Path, mscore: str) -> None:
    """scores/<name>.mscz -> scores/<name>.musicxml and .pdf, keeping its sources and its name as title."""
    xml, pdf = mscz.with_suffix(".musicxml"), mscz.with_suffix(".pdf")
    sources = sources_of(xml) if xml.exists() else []
    convert(mscore, mscz, xml)
    keep_sources(xml, sources)
    retitle(xml, mscz.stem)
    pdf.unlink(missing_ok=True)
    rb.check_musescore(mscore, str(xml.relative_to(rb.ROOT)), mscz.stem, SCORES)
    same_mtime(xml, mscz)


def sync(mscore: str | None = None) -> list[str]:
    """Bring back every MuseScore file saved since its last sync. Returns the names brought back."""
    with SYNCING:  # the UI's server is threaded
        return sync_all(mscore or rb.mscore_bin())


def sync_all(mscore: str | None) -> list[str]:
    if not mscore:
        return []
    done = []
    for row in rb.read_result_rows():
        name = row["output_name"]
        if saved_since_sync(EDITS / f"{name}.mscz", rb.OUTPUTS / f"{name}.musicxml"):
            rb.upsert_result(apply_edit(row, mscore))
            done.append(name)
    for mscz in sorted(SCORES.glob("*.mscz")) if SCORES.is_dir() else []:
        if saved_since_sync(mscz, mscz.with_suffix(".musicxml")):
            sync_score(mscz, mscore)
            done.append(mscz.stem)
    return done
