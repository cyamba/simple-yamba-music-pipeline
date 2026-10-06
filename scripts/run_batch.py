"""Run HOMR over every score in inputs/ and record what happened.

Usage:  uv run python scripts/run_batch.py [--timeout SECONDS]

inputs/<name>.(png|jpg|jpeg|pdf)
  -> work/<name>/<output-name>.homr.musicxml  (HOMR's own output, kept for comparison)
  -> outputs/<name>.musicxml            (images; HOMR's output fixed by postprocess.py)
  -> outputs/<name>-pNN.musicxml        (one per PDF page)
  -> logs/<output-name>.log             (homr stdout/stderr)
inputs/<name>.(musicxml|mxl)            (a score you already have, e.g. exported from MuseScore)
  -> outputs/<name>.musicxml            (copied as is, uncompressed; no HOMR, no postprocess)
all -> review/run-results.csv, review/environment.md, new rows in review/review.md

Per-input options for postprocess.py go in inputs/postprocess.json, keyed by a glob on the
output name, e.g. {"merkurius-*": {"treble_8va": 1}}.

Failures are recorded, never retried or hidden.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fnmatch
import json
import platform
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from importlib.metadata import version
from pathlib import Path

import postprocess

ROOT = Path(__file__).resolve().parent.parent
INPUTS, OUTPUTS, LOGS, WORK, REVIEW = (ROOT / d for d in ("inputs", "outputs", "logs", "work", "review"))
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}  # what homr accepts
MUSICXML_EXTS = {".musicxml", ".mxl"}  # imported as they are
SOURCE_EXTS = IMAGE_EXTS | {".pdf"} | MUSICXML_EXTS
RESULT_FIELDS = ["source", "output_name", "status", "seconds", "output", "musescore", "log", "fixes"]
MSCORE_CANDIDATES = [
    "mscore",
    "musescore",
    "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore Studio 4.app/Contents/MacOS/mscore",
]
MSCORE_LOGS = Path.home() / "Library/Application Support/MuseScore/MuseScore4/logs"
TABLE_HEADER = "| sample | converted | opens in MuseScore | major errors (notes / rhythms / voices / measures / layout) | correction effort | notes |"


def homr_bin() -> str:
    local = Path(sys.executable).parent / "homr"
    return str(local) if local.exists() else (shutil.which("homr") or "homr")


def mscore_bin() -> str | None:
    for c in MSCORE_CANDIDATES:
        found = shutil.which(c) or (c if Path(c).exists() else None)
        if found:
            return found
    return None


def tool_version(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (p.stdout + p.stderr).strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001 - version lookup is best effort
        return f"unavailable ({e.__class__.__name__})"


def prepare_pages(src: Path) -> list[tuple[str, Path]]:
    """Return (output_name, page_image) pairs, converting PDFs to PNG pages."""
    work = WORK / src.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if src.suffix.lower() == ".pdf":
        subprocess.run(["pdftoppm", "-r", "300", "-png", str(src), str(work / "p")], check=True)
        pages = sorted(work.glob("p-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        return [(f"{src.stem}-p{i:02d}", p) for i, p in enumerate(pages, 1)]
    dst = work / src.name
    shutil.copy2(src, dst)
    return [(src.stem, dst)]


def run_homr(name: str, image: Path, timeout: int) -> tuple[str, float, Path | None]:
    log = LOGS / f"{name}.log"
    start = time.monotonic()
    try:
        p = subprocess.run([homr_bin(), str(image)], capture_output=True, text=True, timeout=timeout)
        log.write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
        status = "ok" if p.returncode == 0 else f"failed (exit {p.returncode})"
    except subprocess.TimeoutExpired:
        log.write_text(f"timeout after {timeout}s\n")
        status = "timeout"
    seconds = round(time.monotonic() - start, 1)

    produced = image.with_suffix(".musicxml")
    if not produced.exists():
        return (status if status != "ok" else "no-output"), seconds, None
    raw = image.parent / f"{name}.homr.musicxml"
    shutil.move(produced, raw)
    return status, seconds, raw


def postprocess_options(name: str, rules: Path = INPUTS / "postprocess.json") -> dict:
    if not rules.exists():
        return {}
    return next((opts for pattern, opts in json.loads(rules.read_text()).items()
                 if fnmatch.fnmatch(name, pattern)), {})


def fix_output(name: str, raw: Path) -> tuple[str, str]:
    """HOMR's MusicXML -> outputs/<name>.musicxml, fixed by postprocess.py. Returns (path, fixes)."""
    out = OUTPUTS / f"{name}.musicxml"
    try:
        fixes = str(postprocess.fix_file(raw, out, postprocess_options(name).get("treble_8va")))
    except Exception as e:  # noqa: BLE001 - keep HOMR's file rather than lose the page
        shutil.copy2(raw, out)
        fixes = f"postprocess failed ({e.__class__.__name__}: {e}); HOMR output as is"
    return str(out.relative_to(ROOT)), fixes


def load_musicxml(src: Path) -> bytes:
    """The score in a .musicxml or compressed .mxl file, checked to be a partwise MusicXML score."""
    if src.suffix.lower() == ".mxl":
        try:
            with zipfile.ZipFile(src) as z:
                rootfile = ET.fromstring(z.read("META-INF/container.xml")).find(".//rootfile")
                if rootfile is None or not rootfile.get("full-path"):
                    raise ValueError(f"{src.name}: no rootfile in META-INF/container.xml")
                data = z.read(rootfile.get("full-path"))
        except (zipfile.BadZipFile, KeyError) as e:
            raise ValueError(f"{src.name} is not a compressed MusicXML file ({e})") from e
    else:
        data = src.read_bytes()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"{src.name} is not valid XML ({e})") from e
    if root.tag != "score-partwise":
        raise ValueError(f"{src.name} is not a partwise MusicXML score (its root is <{root.tag}>)")
    return data


def import_musicxml(src: Path, mscore: str | None) -> dict:
    """inputs/<name>.(musicxml|mxl) -> outputs/<name>.musicxml, rendered like a HOMR result."""
    data = load_musicxml(src)
    OUTPUTS.mkdir(exist_ok=True)
    out = OUTPUTS / f"{src.stem}.musicxml"
    out.write_bytes(data)
    xml = str(out.relative_to(ROOT))
    return dict(source=src.name, output_name=src.stem, status="imported", seconds=0, output=xml,
                musescore=check_musescore(mscore, xml, src.stem), log="", fixes="")


def write_results(rows: list[dict]) -> None:
    with (REVIEW / "run-results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        w.writeheader()
        w.writerows(rows)


def upsert_result(row: dict) -> None:
    """Replace the row with the same output name in run-results.csv, or add it at the end."""
    path = REVIEW / "run-results.csv"
    nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
    rows = list(csv.DictReader(path.open())) if path.exists() else []
    write_results([r for r in rows if nfc(r["output_name"]) != nfc(row["output_name"])] + [row])


def musescore_convert(mscore: str, src: Path, dst: Path) -> str:
    """Have MuseScore write src as dst, in the format of dst's extension. Returns "yes" or "no (reason)"."""
    started = time.time() - 1
    try:
        p = subprocess.run([mscore, "-o", str(dst), str(src)], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "no (timeout)"
    if p.returncode == 0:
        return "yes"
    logged = str(src.relative_to(ROOT)) if src.is_relative_to(ROOT) else str(src)
    return f"no (exit {p.returncode}{': ' + reason if (reason := musescore_load_error(logged, started)) else ''})"


def check_musescore(mscore: str | None, xml: str, name: str, renders: Path = REVIEW / "renders") -> str:
    if not mscore or not xml:
        return "skipped"
    renders.mkdir(exist_ok=True)
    return musescore_convert(mscore, ROOT / xml, renders / f"{name}.pdf")


def musescore_load_error(xml: str, since: float) -> str:
    """MuseScore's CLI prints nothing on failure; the reason is in a log file it wrote.

    Several logs can be active (e.g. the MuseScore app is open), so search all touched since the run.
    """
    nfc = lambda s: unicodedata.normalize("NFC", s)  # "å" may be stored as a + combining ring  # noqa: E731
    for log in (p for p in MSCORE_LOGS.glob("*.log") if p.stat().st_mtime >= since):
        # the error text can span several lines; the path comes last
        for err, path in re.findall(r"failed load notation, err: \[\d+\] (.*?), path: ([^\n]*)", log.read_text(errors="replace"), re.S):
            if nfc(path.strip()).endswith(nfc(xml)):
                return re.sub(r"<[^>]+>", "", err).replace("\n", "; ")
    return ""


def write_environment(mscore: str | None, timeout: int) -> None:
    lines = [
        "# Environment of the last run",
        "",
        f"- Date: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- OS: {platform.platform()}",
        f"- Python: {platform.python_version()}",
        f"- uv: {tool_version(['uv', '--version'])}",
        f"- homr: {version('homr')}",
        f"- pdftoppm: {tool_version(['pdftoppm', '-v'])}",
        f"- MuseScore CLI: {tool_version([mscore, '--version']) if mscore else 'not found (manual review)'}",
        "",
        "## Commands to repeat the run",
        "",
        "```bash",
        "uv sync",
        f"uv run python scripts/run_batch.py --timeout {timeout}",
        "# per page, the script runs:  pdftoppm -r 300 -png <pdf> work/<name>/p   (PDFs only)",
        "#                             homr <page image>",
        "#                             postprocess.py fixes -> outputs/<name>.musicxml",
        "```",
        "",
    ]
    (REVIEW / "environment.md").write_text("\n".join(lines))


def append_review_rows(rows: list[dict]) -> None:
    review = REVIEW / "review.md"
    text = unicodedata.normalize("NFC", review.read_text())
    new = [
        unicodedata.normalize("NFC", f"| {r['output_name']} | {r['status']} | {r['musescore'] if r['musescore'] != 'skipped' else ''} |  |  |  |")
        for r in rows
        if unicodedata.normalize("NFC", f"| {r['output_name']} |") not in text
    ]
    if not new:
        return
    lines = text.splitlines()
    if TABLE_HEADER not in lines:  # table removed by hand; just append
        review.write_text(text.rstrip() + "\n" + "\n".join(new) + "\n")
        return
    end = lines.index(TABLE_HEADER) + 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    review.write_text("\n".join(lines[:end] + new + lines[end:]) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=int, default=900, help="seconds per page (default 900)")
    args = ap.parse_args()

    for d in (OUTPUTS, LOGS, WORK, REVIEW):
        d.mkdir(exist_ok=True)
    sources = sorted(p for p in INPUTS.iterdir() if p.suffix.lower() in SOURCE_EXTS)
    if not sources:
        sys.exit("No images, PDFs or MusicXML files in inputs/")
    mscore = mscore_bin()

    rows: list[dict] = []
    for src in sources:
        if src.suffix.lower() in MUSICXML_EXTS:
            print(f"[import] {src.stem} ...", flush=True)
            try:
                rows.append(import_musicxml(src, mscore))
            except (ValueError, OSError) as e:
                rows.append(dict(source=src.name, output_name=src.stem, status=f"import failed ({e})",
                                 seconds=0, output="", musescore="skipped", log="", fixes=""))
            continue
        try:
            pages = prepare_pages(src)
        except Exception as e:  # noqa: BLE001 - record and move on
            rows.append(dict(source=src.name, output_name=src.stem, status=f"prep failed ({e})",
                             seconds=0, output="", musescore="skipped", log="", fixes=""))
            continue
        for name, image in pages:
            print(f"[homr] {name} ...", flush=True)
            status, seconds, raw = run_homr(name, image, args.timeout)
            xml, fixes = fix_output(name, raw) if raw else ("", "")
            ms = check_musescore(mscore, xml, name)
            print(f"        {status} in {seconds}s  musescore={ms}  fixes: {fixes or '-'}", flush=True)
            rows.append(dict(source=src.name, output_name=name, status=status, seconds=seconds,
                             output=xml, musescore=ms, log=f"logs/{name}.log", fixes=fixes))

    write_results(rows)
    write_environment(mscore, args.timeout)
    append_review_rows(rows)

    ok = sum(r["status"] in ("ok", "imported") for r in rows)
    print(f"\n{ok}/{len(rows)} pages converted or imported. See review/run-results.csv and fill in review/review.md.")


if __name__ == "__main__":
    main()
