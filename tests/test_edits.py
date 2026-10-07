import csv
import os
import xml.etree.ElementTree as ET

import pytest

import edits
import run_batch
from combine import concatenate, sources_of
from run_batch import mscore_bin
from xmlbuild import backup, note, write
from xmlbuild import score as bare_score

pytestmark = pytest.mark.skipif(mscore_bin() is None, reason="MuseScore is not installed")
WHOLE = note("C5", 16) + backup(16) + note("C4", 16, staff=2)


def score(*measures):
    """MuseScore needs a part list to import a score."""
    part_list = '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
    return bare_score(*measures).replace('<part id="P1">', part_list + '<part id="P1">')


@pytest.fixture
def root(tmp_path, monkeypatch):
    for name, sub in (("ROOT", ""), ("OUTPUTS", "outputs"), ("REVIEW", "review")):
        monkeypatch.setattr(run_batch, name, tmp_path / sub)
    monkeypatch.setattr(edits, "EDITS", tmp_path / "edits")
    monkeypatch.setattr(edits, "SCORES", tmp_path / "scores")
    for d in ("outputs", "review", "scores"):
        (tmp_path / d).mkdir()
    return tmp_path


def save_in_musescore(mscore, mscz, xml_text, tmp_path):
    """What saving in MuseScore does: new content, newer mtime."""
    edits.convert(mscore, write(tmp_path, xml_text, "saved.musicxml"), mscz)
    later = mscz.stat().st_mtime_ns + 5_000_000_000
    os.utime(mscz, ns=(later, later))


def measures(path):
    return len(ET.parse(path).getroot().findall("part/measure"))


def test_a_result_saved_in_musescore_comes_back_as_edited(root):
    mscore = mscore_bin()
    xml = write(root, score(WHOLE), "outputs/sec.musicxml")
    run_batch.write_results([dict(source="sec.jpg", output_name="sec", status="ok", seconds=1,
                                  output="outputs/sec.musicxml", musescore="yes", log="logs/sec.log", fixes="")])
    mscz = edits.ensure_mscz(xml, mscore)
    assert mscz == root / "edits/sec.mscz"
    assert edits.sync(mscore) == []  # opened but not saved yet: nothing to bring back
    assert edits.ensure_mscz(xml, mscore).stat().st_mtime_ns == mscz.stat().st_mtime_ns  # not made again

    save_in_musescore(mscore, mscz, score(WHOLE, WHOLE, WHOLE), root)
    assert edits.sync(mscore) == ["sec"]
    assert measures(xml) == 3
    row, = csv.DictReader((root / "review/run-results.csv").open())
    assert (row["status"], row["log"]) == ("edited", "logs/sec.log")
    assert edits.sync(mscore) == []


def test_a_replaced_musicxml_makes_its_musescore_file_again(root):
    mscore = mscore_bin()
    xml = write(root, score(WHOLE), "outputs/sec.musicxml")
    mscz = edits.ensure_mscz(xml, mscore)
    xml.write_text(score(WHOLE, WHOLE))  # e.g. uploaded again
    os.utime(xml, ns=(mscz.stat().st_mtime_ns + 1, mscz.stat().st_mtime_ns + 1))
    edits.ensure_mscz(xml, mscore)
    edits.convert(mscore, mscz, root / "back.musicxml")
    assert measures(root / "back.musicxml") == 2


def test_a_full_score_edited_in_musescore_keeps_its_sources_and_name(root):
    mscore = mscore_bin()
    a, b = write(root, score(WHOLE), "a.musicxml"), write(root, score(WHOLE), "b.musicxml")
    xml = root / "scores/Full.musicxml"
    concatenate([a, b], "Full", xml)
    mscz = edits.ensure_mscz(xml, mscore)
    assert mscz == root / "scores/Full.mscz"
    save_in_musescore(mscore, mscz, score(WHOLE, WHOLE, WHOLE), root)
    assert edits.sync(mscore) == ["Full"]
    assert measures(xml) == 3
    assert sources_of(xml) == ["a.musicxml", "b.musicxml"]
    assert ET.parse(xml).getroot().findtext("work/work-title") == "Full"
    assert (root / "scores/Full.pdf").exists()
