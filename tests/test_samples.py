import csv
import json

import pytest

from samples import rename_sample, rename_score

FIELDS = ["source", "output_name", "status", "seconds", "output", "musescore", "log", "fixes"]


def make_root(root):
    for name in ("merk-p1", "other"):
        for path in (f"inputs/{name}.jpg", f"work/{name}/{name}.jpg", f"work/{name}/{name}_teaser.png",
                     f"work/{name}/{name}.homr.musicxml", f"outputs/{name}.musicxml", f"logs/{name}.log",
                     f"review/renders/{name}.pdf"):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(name)
    with (root / "review/run-results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for name in ("merk-p1", "other"):
            w.writerow(dict(source=f"{name}.jpg", output_name=name, status="ok", seconds=1,
                            output=f"outputs/{name}.musicxml", musescore="yes", log=f"logs/{name}.log", fixes=""))
    (root / "review/review.md").write_text("| sample | notes |\n|---|---|\n| merk-p1 | fine |\n| other | |\n")
    (root / "review/truth").mkdir()
    (root / "review/truth/t.json").write_text(json.dumps({"clefs": {}, "pages": {"merk-p1": {"measures": []}}}))
    (root / "inputs/postprocess.json").write_text(json.dumps({"merk-*": {"treble_8va": 1}}))


def test_rename_takes_everything_named_after_the_sample_along(tmp_path):
    make_root(tmp_path)
    assert rename_sample(tmp_path, "merk-p1", "Page one") == {"merk-p1": "Page one"}
    for path in ("inputs/Page one.jpg", "work/Page one/Page one.jpg", "work/Page one/Page one_teaser.png",
                 "work/Page one/Page one.homr.musicxml", "outputs/Page one.musicxml", "logs/Page one.log",
                 "review/renders/Page one.pdf"):
        assert (tmp_path / path).read_text() == "merk-p1", path
    assert not (tmp_path / "work/merk-p1").exists() and not (tmp_path / "inputs/merk-p1.jpg").exists()
    assert (tmp_path / "outputs/other.musicxml").exists()
    rows = list(csv.DictReader((tmp_path / "review/run-results.csv").open()))
    assert [(r["source"], r["output_name"], r["output"]) for r in rows] == [
        ("Page one.jpg", "Page one", "outputs/Page one.musicxml"), ("other.jpg", "other", "outputs/other.musicxml")]
    assert "| Page one | fine |" in (tmp_path / "review/review.md").read_text()
    assert list(json.loads((tmp_path / "review/truth/t.json").read_text())["pages"]) == ["Page one"]
    # "Page one" no longer matches "merk-*", so it keeps its 8va clef under its own name
    assert json.loads((tmp_path / "inputs/postprocess.json").read_text())["Page one"] == {"treble_8va": 1}


@pytest.mark.parametrize("new", ["other", "../escape", "", ".hidden"])
def test_rename_refuses_taken_or_unsafe_names_and_moves_nothing(tmp_path, new):
    make_root(tmp_path)
    with pytest.raises(ValueError):
        rename_sample(tmp_path, "merk-p1", new)
    assert (tmp_path / "inputs/merk-p1.jpg").exists() and (tmp_path / "outputs/merk-p1.musicxml").exists()


def test_rename_score_only_renames_existing_full_scores(tmp_path):
    scores = tmp_path / "scores"
    scores.mkdir()
    (scores / "Merk.musicxml").write_text('<?xml version="1.0"?><score-partwise><work><work-title>Merk</work-title></work></score-partwise>')
    assert rename_score(scores, "Merk", "Merkurius") == "Merkurius"
    assert "<work-title>Merkurius</work-title>" in (scores / "Merkurius.musicxml").read_text()
    with pytest.raises(ValueError):
        rename_score(scores, "../outputs/x", "y")
