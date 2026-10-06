from score_check import compare, compare_staff, parse_tokens, read_musicxml, to_tokens
from xmlbuild import backup, note, rest, score, write


def test_reads_both_staves_with_chords_and_rests(tmp_path):
    measure = (note("C5", 2) + note("E5", 2, chord=True) + rest(2) + note("Bb4", 4)
               + backup(8) + note("C4", 8, staff=2))
    s = read_musicxml(write(tmp_path, score(measure, time=(2, 4))))
    assert to_tokens(s.measures[0].staves[1]) == "C5+E5:8 r:8 Bb4:4"
    assert to_tokens(s.measures[0].staves[2]) == "C4:2"


def test_compare_scores_rhythm_and_known_pitches_only():
    got = parse_tokens("C5:8 D5:8 r:4")
    assert compare_staff("C5:8 ?:8 r:8 r:8", got).rhythm_ok  # rests compared as silence
    assert compare_staff("C5:8 ?:8 r:4", got).pitches_known == 1
    assert compare_staff("C#5:8 D5:8 r:4", got).pitches_correct == 1  # spelling matters
    assert not compare_staff("C5:16 D5:16 r:8 r:4", got).rhythm_ok


def test_page_report_matches_measures_by_position(tmp_path):
    m = note("C5", 8) + backup(8) + note("C4", 8, staff=2)
    s = read_musicxml(write(tmp_path, score(m, m, time=(2, 4))))
    truth = {"clefs": {"1": "G2", "2": "G2"}, "measures": [
        {"number": 21, "time": "2/4", "rh": "C5:2", "lh": None},
        {"number": 22, "time": "3/4", "rh": "D5:2", "lh": "R"}]}
    r = compare(s, truth)
    assert (r.time_ok, r.rhythm_ok, r.staff_measures, r.pitches_correct, r.pitches_known) == (1, 2, 3, 1, 2)
