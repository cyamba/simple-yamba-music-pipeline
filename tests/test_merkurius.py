"""Merkurius, four phone photos (inputs/merkurius-*.jpg), against the hand-checked truth.

Runs the current postprocess.py on HOMR's committed output (work/<page>/<page>.homr.musicxml),
so it needs no HOMR run. The thresholds are the results of the last run: raise them when a
change does better; a drop is a regression.
"""
import json
from pathlib import Path

from postprocess import fix_file
from run_batch import postprocess_options
from score_check import compare, load_truth, read_musicxml

ROOT = Path(__file__).resolve().parent.parent
TRUTH = ROOT / "review/truth/merkurius.json"
PAGES = list(json.loads(TRUTH.read_text())["pages"])  # follows renames made in the UI


def scores(tmp_path, fix: bool):
    reports = []
    for page in PAGES:
        xml = ROOT / "work" / page / f"{page}.homr.musicxml"
        if fix:
            fix_file(xml, tmp_path / xml.name, postprocess_options(page).get("treble_8va"))
            xml = tmp_path / xml.name
        reports.append(compare(read_musicxml(xml), load_truth(TRUTH, page)))
    return reports, lambda attr: sum(getattr(r, attr) for r in reports)


def test_fixed_output_holds_its_scores(tmp_path):
    reports, total = scores(tmp_path, fix=True)
    assert all(r.measures_found == r.measures_expected for r in reports)
    assert all(r.clefs_ok and not r.overfull for r in reports)
    assert total("time_ok") >= 39  # of 40 measures
    assert total("rhythm_ok") >= 68  # of 78 transcribed staff-measures
    assert total("pitches_correct") >= 92  # of 93 checked pitches (page 1)


def test_homr_alone_for_comparison(tmp_path):
    reports, total = scores(tmp_path, fix=False)
    assert total("time_ok") == 14
    assert total("rhythm_ok") == 64
    assert sum(len(r.overfull) for r in reports) == 24
