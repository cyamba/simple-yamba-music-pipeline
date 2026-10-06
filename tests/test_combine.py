import xml.etree.ElementTree as ET

import pytest

from combine import concatenate, sources_of
from score_check import read_musicxml
from xmlbuild import backup, note, score, write

WHOLE = note("C5", 16) + backup(16) + note("C4", 16, staff=2)


def test_measures_follow_each_other_and_the_join_repeats_nothing(tmp_path):
    a = write(tmp_path, score(WHOLE, WHOLE), "a.musicxml")
    b = write(tmp_path, score(WHOLE), "b.musicxml")
    out = tmp_path / "full.musicxml"
    assert concatenate([a, b], "Full", out) == 3
    root = ET.parse(out).getroot()
    measures = root.findall("part/measure")
    assert [m.get("number") for m in measures] == ["1", "2", "3"]
    assert measures[2].find("attributes") is None  # same divisions, key, time, staves and clefs
    assert measures[2].find("print").get("new-system") == "yes"
    assert root.findtext("work/work-title") == "Full"
    assert sources_of(out) == ["a.musicxml", "b.musicxml"]


def test_a_change_at_the_join_is_kept(tmp_path):
    a = write(tmp_path, score(WHOLE), "a.musicxml")
    b = write(tmp_path, score(note("C5", 8) + backup(8) + note("C4", 8, staff=2), time=(2, 4)), "b.musicxml")
    out = tmp_path / "full.musicxml"
    concatenate([a, b], "Full", out)
    assert [m.time for m in read_musicxml(out).measures] == [(4, 4), (2, 4)]
    assert [el.tag for el in ET.parse(out).getroot().find("part/measure[2]/attributes")] == ["time"]


def test_scores_with_different_staves_are_refused(tmp_path):
    a = write(tmp_path, score(WHOLE), "a.musicxml")
    b = write(tmp_path, score(note("C5", 16)).replace("<staves>2</staves>", "<staves>1</staves>"), "b.musicxml")
    with pytest.raises(ValueError, match="staves"):
        concatenate([a, b], "Full", tmp_path / "full.musicxml")
