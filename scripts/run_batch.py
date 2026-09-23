"""Run HOMR over every score in inputs/ and record what happened.

Usage:  uv run python scripts/run_batch.py [--timeout SECONDS]

inputs/<name>.(png|jpg|jpeg|pdf)
  -> outputs/<name>.musicxml            (images)
  -> outputs/<name>-pNN.musicxml        (one per PDF page)
  -> logs/<output-name>.log             (homr stdout/stderr)
  -> review/run-results.csv, review/environment.md, new rows in review/review.md

Failures are recorded, never retried or hidden.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import platform
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS, OUTPUTS, LOGS, WORK, REVIEW = (ROOT / d for d in ("inputs", "outputs", "logs", "work", "review"))
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}  # what homr accepts
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


def run_homr(name: str, image: Path, timeout: int) -> tuple[str, float, str]:
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
        return (status if status != "ok" else "no-output"), seconds, ""
    out = OUTPUTS / f"{name}.musicxml"
    shutil.move(produced, out)
    return status, seconds, str(out.relative_to(ROOT))


def check_musescore(mscore: str | None, xml: str, name: str) -> str:
    if not mscore or not xml:
        return "skipped"
    renders = REVIEW / "renders"
    renders.mkdir(exist_ok=True)
    started = time.time() - 1
    try:
        p = subprocess.run([mscore, "-o", str(renders / f"{name}.pdf"), str(ROOT / xml)],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "no (timeout)"
    if p.returncode == 0:
        return "yes"
    return f"no (exit {p.returncode}{': ' + reason if (reason := musescore_load_error(xml, started)) else ''})"


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
    sources = sorted(p for p in INPUTS.iterdir() if p.suffix.lower() in IMAGE_EXTS | {".pdf"})
    if not sources:
        sys.exit("No images or PDFs in inputs/")
    mscore = mscore_bin()

    rows: list[dict] = []
    for src in sources:
        try:
            pages = prepare_pages(src)
        except Exception as e:  # noqa: BLE001 - record and move on
            rows.append(dict(source=src.name, output_name=src.stem, status=f"prep failed ({e})",
                             seconds=0, output="", musescore="skipped", log=""))
            continue
        for name, image in pages:
            print(f"[homr] {name} ...", flush=True)
            status, seconds, xml = run_homr(name, image, args.timeout)
            ms = check_musescore(mscore, xml, name)
            print(f"        {status} in {seconds}s  musescore={ms}", flush=True)
            rows.append(dict(source=src.name, output_name=name, status=status, seconds=seconds,
                             output=xml, musescore=ms, log=f"logs/{name}.log"))

    with (REVIEW / "run-results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    write_environment(mscore, args.timeout)
    append_review_rows(rows)

    ok = sum(r["status"] == "ok" for r in rows)
    print(f"\n{ok}/{len(rows)} pages converted. See review/run-results.csv and fill in review/review.md.")


if __name__ == "__main__":
    main()
