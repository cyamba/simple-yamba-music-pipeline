from fractions import Fraction

from postprocess import fix_file
from score_check import read_musicxml, to_tokens
from xmlbuild import backup, note, rest, score, write


def fixed(tmp_path, xml, **kwargs):
    out = tmp_path / "out.musicxml"
    report = fix_file(write(tmp_path, xml), out, **kwargs)
    return read_musicxml(out), report


def test_each_measure_gets_the_time_signature_it_fills(tmp_path):
    # HOMR wrote 4/4 once for the whole page; the measures hold 4/4, 5/4 and 5/8.
    four = "".join(note("C5", 4) for _ in range(4)) + backup(16) + "".join(note("C4", 4, staff=2) for _ in range(4))
    five = "".join(note("C5", 4) for _ in range(5)) + backup(20) + "".join(note("C4", 4, staff=2) for _ in range(5))
    five_eighths = "".join(note("C5", 2) for _ in range(5)) + backup(10) + "".join(note("C4", 2, staff=2) for _ in range(5))
    s, _ = fixed(tmp_path, score(four, five, five_eighths))
    assert [m.time for m in s.measures] == [(4, 4), (5, 4), (5, 8)]


def test_staff_timed_by_the_other_staffs_cursor_is_laid_out_again(tmp_path):
    # Like HOMR on m34: the right hand's second rest is placed among the left hand's sixteenths
    # (onset 1/4 instead of 1) and overlaps the first one, in another voice.
    measure = (rest(4) + backup(4) + note("C4", 1, staff=2) + rest(4, voice=2) + backup(4)
               + "".join(note("C4", 1, staff=2) for _ in range(7)))
    s, report = fixed(tmp_path, score(measure, time=(2, 4)))
    assert [(e.onset, e.duration) for e in s.measures[0].staves[1]] == [(0, 1), (1, 1)]
    assert report.retimed == ["1/1"]


def test_real_polyphony_is_left_alone(tmp_path):
    # A half note held under two quarters in the same staff is two voices, not a timing error.
    measure = (note("C5", 8) + backup(8) + note("E4", 4, voice=2) + note("F4", 4, voice=2)
               + backup(8) + note("C4", 8, staff=2))
    s, report = fixed(tmp_path, score(measure, time=(2, 4)))
    assert report.retimed == []
    assert [(e.onset, e.pitches) for e in s.measures[0].staves[1]] == [(0, ("E4",)), (0, ("C5",)), (1, ("F4",))]


def test_short_and_empty_staves_are_filled_with_rests(tmp_path):
    short = note("C5", 4) + note("D5", 4) + note("E5", 4) + backup(12) + "".join(note("C4", 4, staff=2) for _ in range(4))
    empty_rh = "".join(note("C4", 2, staff=2) for _ in range(5))
    s, report = fixed(tmp_path, score(short, empty_rh))
    assert to_tokens(s.measures[0].staves[1]) == "C5:4 D5:4 E5:4 r:4"
    assert s.measures[1].time == (5, 8)
    assert [(e.duration, e.is_rest) for e in s.measures[1].staves[1]] == [(Fraction(5, 2), True)]  # measure rest
    assert report.padded == ["1/1", "2/1"]


def test_treble_8va_keeps_the_written_notes(tmp_path):
    measure = note("A6", 16) + backup(16) + note("C4", 16, staff=2)
    out = tmp_path / "out.musicxml"
    fix_file(write(tmp_path, score(measure)), out, treble_8va=1)
    assert "<clef-octave-change>1</clef-octave-change>" in out.read_text()
    assert "<octave>7</octave>" in out.read_text()  # MusicXML stores the sounding pitch
    s = read_musicxml(out)
    assert s.clefs == {1: "G2+1", 2: "G2"}
    assert to_tokens(s.measures[0].staves[1]) == "A6:1" and to_tokens(s.measures[0].staves[2]) == "C4:1"
