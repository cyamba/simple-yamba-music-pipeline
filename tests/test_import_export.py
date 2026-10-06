import csv
import zipfile

import pytest

import run_batch
from export import export
from run_batch import import_musicxml, load_musicxml, mscore_bin, upsert_result
from samples import rename_sample
from xmlbuild import backup, note, score, write

WHOLE = note("C5", 16) + backup(16) + note("C4", 16, staff=2)
CONTAINER = ('<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
             '<rootfile full-path="score.xml"/></rootfiles></container>')


def mxl(path, xml, container=CONTAINER):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("score.xml", xml)
    return path


def test_plain_and_compressed_musicxml_load_the_same(tmp_path):
    xml = score(WHOLE)
    assert load_musicxml(write(tmp_path, xml, "a.musicxml")) == xml.encode()
    assert load_musicxml(mxl(tmp_path / "a.mxl", xml)) == xml.encode()


@pytest.mark.parametrize("make", [
    lambda p: write(p, "<html><body/></html>", "x.musicxml"),
    lambda p: write(p, "not xml at all", "x.musicxml"),
    lambda p: write(p, "not a zip", "x.mxl"),
    lambda p: mxl(p / "x.mxl", score(WHOLE), container="<container/>"),
])
def test_files_that_are_not_scores_are_refused(tmp_path, make):
    with pytest.raises(ValueError):
        load_musicxml(make(tmp_path))


@pytest.fixture
def root(tmp_path, monkeypatch):
    for name in ("ROOT", "OUTPUTS", "REVIEW"):
        monkeypatch.setattr(run_batch, name, tmp_path / ("" if name == "ROOT" else name.lower()))
    for d in ("inputs", "outputs", "review"):
        (tmp_path / d).mkdir()
    return tmp_path


def test_import_writes_the_output_and_upsert_replaces_its_row(root):
    src = mxl(root / "inputs/merk-m11-21.mxl", score(WHOLE))
    row = import_musicxml(src, None)
    assert (row["output_name"], row["status"], row["output"], row["musescore"]) == (
        "merk-m11-21", "imported", "outputs/merk-m11-21.musicxml", "skipped")
    assert (root / "outputs/merk-m11-21.musicxml").read_text() == score(WHOLE)

    run_batch.write_results([dict(row, output_name="first", source="first.jpg", status="ok"), row])
    upsert_result(dict(row, musescore="yes"))
    upsert_result(dict(row, output_name="new", source="new.musicxml"))
    rows = list(csv.DictReader((root / "review/run-results.csv").open()))
    assert [(r["output_name"], r["musescore"]) for r in rows] == [
        ("first", "skipped"), ("merk-m11-21", "yes"), ("new", "skipped")]


def test_an_imported_sample_can_be_renamed(root):
    src = write(root, score(WHOLE), "inputs/merk.musicxml")
    run_batch.write_results([import_musicxml(src, None)])
    (root / "review/review.md").write_text("| merk | |\n")
    assert rename_sample(root, "merk", "Merkurius m11-21") == {"merk": "Merkurius m11-21"}
    assert (root / "inputs/Merkurius m11-21.musicxml").exists()
    assert (root / "outputs/Merkurius m11-21.musicxml").exists()
    rows = list(csv.DictReader((root / "review/run-results.csv").open()))
    assert [(r["source"], r["output"], r["log"]) for r in rows] == [
        ("Merkurius m11-21.musicxml", "outputs/Merkurius m11-21.musicxml", "")]


def test_unknown_export_format_is_refused(tmp_path):
    with pytest.raises(ValueError, match="format"):
        export(write(tmp_path, score(WHOLE)), "mp3", tmp_path)


@pytest.mark.skipif(mscore_bin() is None, reason="MuseScore is not installed")
@pytest.mark.parametrize("fmt, ext, magic", [("pdf", ".pdf", b"%PDF"), ("midi", ".mid", b"MThd")])
def test_export_with_musescore(tmp_path, fmt, ext, magic):
    out = export(write(tmp_path, score(WHOLE, WHOLE), "two.musicxml"), fmt, tmp_path / "exports")
    assert out == tmp_path / "exports" / f"two{ext}"
    assert out.read_bytes().startswith(magic)
