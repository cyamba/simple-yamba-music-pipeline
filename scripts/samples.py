"""Rename a sample, or a full score, everywhere it is named.

A sample is one file in inputs/ plus everything run_batch.py derived from it and named after it:
work/<name>/, outputs/, logs/, review/renders/, its rows in review/run-results.csv and
review/review.md, its pages in review/truth/*.json and its options in inputs/postprocess.json.
They are all renamed together, so the next batch run doesn't bring back the old name.
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import Path

from combine import retitle
from run_batch import SOURCE_EXTS, postprocess_options

NAME = re.compile(r"^\w[\w .-]*$")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)  # macOS may hand back "å" as a + combining ring


def check_name(name: str) -> str:
    name = nfc(name.strip())
    if not NAME.match(name) or len(name) > 100 or name.endswith("."):
        raise ValueError("Use letters, digits, spaces, '.', '-' or '_', starting with a letter or digit.")
    return name


def move_all(moves: list[tuple[Path, Path]]) -> None:
    """Rename every (src, dst) or none of them."""
    for _, dst in moves:
        if dst.exists():
            raise ValueError(f"{dst.name} already exists")
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            src.rename(dst)
            done.append((src, dst))
    except OSError:
        for src, dst in reversed(done):
            dst.rename(src)
        raise


def rename_sample(root: Path, old: str, new: str) -> dict[str, str]:
    """Rename sample `old` (an input's file name without extension) to `new`.

    Returns {old result name: new result name}; a PDF's pages <old>-pNN become <new>-pNN.
    """
    inputs, review = root / "inputs", root / "review"
    new = check_name(new)
    source = next((p for p in inputs.iterdir() if nfc(p.stem) == nfc(old) and p.suffix.lower() in SOURCE_EXTS), None)
    if source is None:
        raise ValueError(f"no input named {old}")
    if nfc(source.stem) == new:
        return {}
    if any(nfc(p.stem) == new for p in inputs.iterdir() if p.suffix.lower() in SOURCE_EXTS):
        raise ValueError(f"an input named {new} already exists")

    results_csv = review / "run-results.csv"
    rows = list(csv.DictReader(results_csv.open())) if results_csv.exists() else []
    stem = nfc(source.stem)
    names = {nfc(r["output_name"]): new + nfc(r["output_name"])[len(stem):]
             for r in rows if nfc(r["source"]) == nfc(source.name)}
    names.setdefault(stem, new)

    moves = [(source, inputs / f"{new}{source.suffix}")]
    for old_name, new_name in names.items():
        for folder, suffix in (("outputs", ".musicxml"), ("logs", ".log"), ("review/renders", ".pdf")):
            if (path := root / folder / f"{old_name}{suffix}").exists():
                moves.append((path, root / folder / f"{new_name}{suffix}"))
    work = root / "work" / source.stem
    if work.is_dir():
        for path in work.iterdir():  # page image copy, *_teaser.png, *.homr.musicxml
            name = nfc(path.name)
            prefix = next((o for o in sorted(names, key=len, reverse=True)
                           if name.startswith(o) and name[len(o):][:1] in (".", "_", "-")), None)
            if prefix:
                moves.append((path, work / (names[prefix] + name[len(prefix):])))
    move_all(moves)
    if work.is_dir():
        move_all([(work, root / "work" / new)])

    if rows:
        for r in rows:
            if (old_name := nfc(r["output_name"])) in names:
                r["source"] = f"{new}{source.suffix}"
                r["output_name"] = names[old_name]
                r["output"] = f"outputs/{names[old_name]}.musicxml" if r["output"] else ""
                r["log"] = f"logs/{names[old_name]}.log" if r["log"] else ""
        with results_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    rename_in_review(review / "review.md", names)
    for truth in (review / "truth").glob("*.json"):
        data = json.loads(truth.read_text())
        if any(n in data.get("pages", {}) for n in names):
            data["pages"] = {names.get(nfc(k), k): v for k, v in data["pages"].items()}
            truth.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    keep_options(inputs / "postprocess.json", names)
    return names


def rename_in_review(review_md: Path, names: dict[str, str]) -> None:
    """Rename the sample column of the review table rows."""
    if not review_md.exists():
        return
    lines = nfc(review_md.read_text()).splitlines()
    for i, line in enumerate(lines):
        cells = line.split("|")
        if len(cells) > 2 and cells[1].strip() in names:
            cells[1] = f" {names[cells[1].strip()]} "
            lines[i] = "|".join(cells)
    review_md.write_text("\n".join(lines) + "\n")


def keep_options(rules: Path, names: dict[str, str]) -> None:
    """A renamed result that no longer matches its glob in postprocess.json gets its own entry."""
    if not rules.exists():
        return
    data = json.loads(rules.read_text())
    added = {new_name: opts for old_name, new_name in names.items()
             if (opts := postprocess_options(old_name, rules)) and postprocess_options(new_name, rules) != opts}
    if added:
        rules.write_text(json.dumps(data | added, indent=2, ensure_ascii=False) + "\n")


def rename_score(scores: Path, old: str, new: str) -> str:
    """scores/<old>.musicxml (and .pdf) -> <new>, with the new name as its title."""
    new = check_name(new)
    xml = next((p for p in scores.glob("*.musicxml") if nfc(p.stem) == nfc(old)), None)
    if xml is None:
        raise ValueError(f"no full score named {old}")
    old = xml.stem
    if new != nfc(old):
        moves = [(xml, scores / f"{new}.musicxml")]
        if (pdf := scores / f"{old}.pdf").exists():
            moves.append((pdf, scores / f"{new}.pdf"))
        move_all(moves)
    retitle(scores / f"{new}.musicxml", new)
    return new
