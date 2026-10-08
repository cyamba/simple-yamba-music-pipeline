import zipfile

import pytest

import run_batch
import ui
from xmlbuild import backup, note, score

WHOLE = note("C5", 16) + backup(16) + note("C4", 16, staff=2)
CONTAINER = ('<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
             '<rootfile full-path="score.xml"/></rootfiles></container>')


class Found:
    """Stands in for the computer-wide search: these paths were found."""

    def __init__(self, *paths):
        self.paths = {str(p) for p in paths}

    def found(self, path):
        return path in self.paths


@pytest.fixture
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    for d in ("inputs", "outputs", "review", "scores"):
        (project / d).mkdir(parents=True)
    (project / "review/review.md").write_text("| sample | converted | opens in MuseScore |\n|---|---|---|\n")
    for name in ("ROOT", "OUTPUTS", "REVIEW", "INPUTS"):
        monkeypatch.setattr(run_batch, name, project / ("" if name == "ROOT" else name.lower()))
    for name, path in (("ROOT", project), ("INPUTS", project / "inputs"), ("REVIEW", project / "review"),
                       ("STATE", project / "review/full-score.json"), ("SCORES", project / "scores")):
        monkeypatch.setattr(ui, name, path)
    monkeypatch.setattr(ui, "mscore_bin", lambda: None)  # no MuseScore render in tests
    return project


def elsewhere(tmp_path, rel, text):
    path = tmp_path / "elsewhere" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_found_files_become_results_and_the_originals_stay_put(root, tmp_path, monkeypatch):
    plain = elsewhere(tmp_path, "Downloads/merk-m01-10.musicxml", score(WHOLE))
    sniffed = elsewhere(tmp_path, "Scores/merk-m11-21.xml", score(WHOLE))
    packed = tmp_path / "elsewhere/merk-m22-33.mxl"
    with zipfile.ZipFile(packed, "w") as z:
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("score.xml", score(WHOLE))
    before = {p: p.read_bytes() for p in (plain, sniffed, packed)}
    monkeypatch.setattr(ui, "SEARCH", Found(plain, sniffed, packed))

    out = ui.add_found([str(plain), str(sniffed), str(packed)])

    assert out == {"added": ["merk-m01-10", "merk-m11-21", "merk-m22-33"], "already": [], "errors": []}
    assert sorted(p.name for p in (root / "inputs").iterdir()) == [
        "merk-m01-10.musicxml", "merk-m11-21.musicxml", "merk-m22-33.mxl"]  # a MusicXML .xml is kept as .musicxml
    rows = ui.read_results()
    assert [(r["output_name"], r["status"], r["include"]) for r in rows] == [
        ("merk-m01-10", "imported", True), ("merk-m11-21", "imported", True), ("merk-m22-33", "imported", True)]
    assert all(p.read_bytes() == data for p, data in before.items())

    combined = ui.combine("Merk", ["merk-m01-10", "merk-m11-21", "merk-m22-33"])
    assert combined["measures"] == 3 and (root / "scores/Merk.musicxml").exists()


def test_a_taken_name_gets_a_number_and_the_same_file_is_not_added_twice(root, tmp_path, monkeypatch):
    first = elsewhere(tmp_path, "Downloads/merk.musicxml", score(WHOLE))
    other = elsewhere(tmp_path, "Scores/merk.musicxml", score(WHOLE, WHOLE))
    copy = elsewhere(tmp_path, "Downloads/merk (1).musicxml", score(WHOLE))
    monkeypatch.setattr(ui, "SEARCH", Found(first, other, copy))

    assert ui.add_found([str(first), str(other), str(copy)])["added"] == ["merk", "merk-2", "merk-1"]
    assert ui.add_found([str(other), str(first)]) == {"added": [], "already": ["merk-2", "merk"], "errors": []}
    assert len(ui.read_results()) == 3


def test_what_cannot_be_added_is_reported_and_leaves_nothing_behind(root, tmp_path, monkeypatch):
    timewise = elsewhere(tmp_path, "t.xml", '<?xml version="1.0"?><score-timewise version="4.0"/>')
    not_found = elsewhere(tmp_path, "secret.musicxml", score(WHOLE))
    good = elsewhere(tmp_path, "good.musicxml", score(WHOLE))
    monkeypatch.setattr(ui, "SEARCH", Found(timewise, good))

    out = ui.add_found([str(timewise), str(not_found), str(good)])

    assert out["added"] == ["good"]
    assert [e["path"] for e in out["errors"]] == [str(timewise), str(not_found)]
    assert "partwise" in out["errors"][0]["error"] and "search results" in out["errors"][1]["error"]
    assert [p.name for p in (root / "inputs").iterdir()] == ["good.musicxml"]


@pytest.mark.parametrize("stem, expected", [
    ("merkurius-m22-33 (1)", "merkurius-m22-33-1"),
    ("Källvatten", "Källvatten"),
    ("(draft) score!", "draft-score"),
    ("???", "score"),
])
def test_result_stem_makes_names_check_name_accepts(stem, expected):
    assert ui.result_stem(stem) == expected
    ui.check_name(ui.result_stem(stem))
