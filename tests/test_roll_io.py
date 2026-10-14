import struct

import pytest

import combine
import roll
import roll_midi
import roll_ops
import roll_xml
import score_check
from run_batch import mscore_bin, musescore_convert
from test_roll import waltz
from xmlbuild import backup, note, rest, score, write


def notes_of(doc):
    return sorted((n["part"], n["start"], n["pitch"], n["dur"]) for n in doc["notes"])


def busy_roll():
    """Two voices on the right hand, a tie over a barline, a dotted value, a chord and a time change."""
    doc = waltz()
    doc, _ = roll_ops.apply_operations(doc, [
        {"op": "add", "part": "rh", "at": "1:1", "notes": "A4:2."},
        {"op": "set_time", "measure": 4, "time": "2/4"},
        {"op": "add", "part": "lh", "at": "3:2", "notes": "F3:2 | @4:2 E3:16 D3:8."},
        {"op": "change", "select": {"at": "1:1", "pitches": "A4"}, "vel": 64},
    ])
    return doc


# --- MIDI ----------------------------------------------------------------------------------------------------
def test_midi_round_trip_keeps_notes_parts_and_time():
    doc = busy_roll()
    data = roll_midi.write_midi(doc)
    assert data[:4] == b"MThd" and struct.unpack(">HHH", data[8:14]) == (1, 3, 480)
    back, info = roll_midi.read_midi(data)
    assert info == {"snapped": 0, "dropped": {}, "grid": "16", "warnings": []}
    assert [(p["name"], p["group"], p["staff"], p["clef"]) for p in back["parts"]] == [
        ("Right hand", "P1", 1, "G2"), ("Left hand", "P1", 2, "F4")]
    assert [g["name"] for g in back["groups"]] == ["Piano"] and back["title"] == "Waltz"
    ids = {"t2": "rh", "t3": "lh"}
    assert sorted((ids[p], s, pi, d) for p, s, pi, d in notes_of(back)) == notes_of(doc)
    assert (back["time"], back["measures"], back["tempo"]) == (doc["time"], doc["measures"], [{"tick": 0, "bpm": 120.0}])
    assert next(n["vel"] for n in back["notes"] if n["pitch"] == 69) == 64


def test_midi_from_elsewhere_is_snapped_to_the_grid_and_drums_are_left_out():
    def track(events):
        body = b"".join(bytes([0]) + e if i == 0 else e for i, e in enumerate(events)) + b"\x00\xff\x2f\x00"
        return b"MTrk" + struct.pack(">I", len(body)) + body
    # 96 ticks to the quarter: a note 2 ticks late lasting about an eighth, a drum hit, and one never released
    played = track([b"\x90\x3c\x50", b"\x02\x90\x40\x50", b"\x30\x80\x40\x00", b"\x00\x99\x24\x64",
                    b"\x10\x80\x3c\x00"])
    data = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 96) + played
    doc, info = roll_midi.read_midi(data, grid="16")
    assert info["dropped"] == {"drums": 1} and info["snapped"] == 2
    assert [(n["pitch"], n["start"], n["dur"]) for n in doc["notes"]] == [(60, 0, 360), (64, 0, 240)]
    split, _ = roll_midi.read_midi(data, grid="16", split="E4")
    assert [(p["name"], p["staff"]) for p in split["parts"]] == [("Right hand", 1), ("Left hand", 2)]
    with pytest.raises(roll.RollError, match="not a MIDI file"):
        roll_midi.read_midi(b"RIFF")


# --- MusicXML ------------------------------------------------------------------------------------------------
def test_musicxml_writes_staves_voices_ties_and_reads_back_the_same(tmp_path):
    doc = busy_roll()
    xml = roll_xml.write_musicxml(doc)
    path = write(tmp_path, xml.decode())
    read = score_check.read_musicxml(path)
    assert [(m.time, score_check.to_tokens(m.staves[1]), score_check.to_tokens(m.staves[2])) for m in read.measures] == [
        ((3, 4), "D5:8 A4:2. E5:8 F5:4 A5:4", "D3:2."),
        ((3, 4), "G5:2.", "G2:2."),
        ((3, 4), "r:4 C5+E5:2", "r:4 F3:2"),
        ((2, 4), "r:2", "r:4 E3:16 D3:16 D3:8"),  # a dotted 8th off the beat: 16th tied to an 8th
    ]
    assert xml.count(b'<tie type="start" />') == 1
    back, info = roll_xml.read_musicxml(xml)
    assert info["snapped"] == 0 and notes_of(back) == notes_of(doc)
    assert sorted(n.get("name") for n in back["notes"] if n["start"] == 0) == ["A4", "D3", "D5"]
    assert (back["time"], back["key"], back["measures"]) == (doc["time"], doc["key"], doc["measures"])
    assert next(n["vel"] for n in back["notes"] if n["pitch"] == 69) == 64


def test_a_note_over_the_barline_is_tied_and_comes_back_whole():
    doc = roll.new_doc(time="4/4", measures=2, preset="single")
    doc, _ = roll_ops.apply_operations(doc, [{"op": "add", "part": "melody", "at": "1:4", "notes": "C5:2."}])
    xml = roll_xml.write_musicxml(doc)
    assert xml.count(b'<tie type="start" />') == 1 and xml.count(b'<tie type="stop" />') == 1  # 4 over the bar to 2
    back, _ = roll_xml.read_musicxml(xml)
    assert [(n["start"], n["dur"]) for n in back["notes"]] == [(1440, 1440)]


def test_a_homr_result_opens_as_two_hands_of_one_piano():
    xml = score(note("E5", 8) + note("C5", 8, chord=True) + rest(8) + backup(16) + note("C3", 16, staff=2),
                note("D5", 4) + note("F5", 4) + note("D5", 8) + backup(16) + note("G3", 16, staff=2), time=(4, 4))
    doc, info = roll_xml.read_musicxml(xml.encode())
    assert [(p["id"], p["group"], p["staff"]) for p in doc["parts"]] == [("rh", "P1", 1), ("lh", "P1", 2)]
    assert roll.streams(doc, range(1, 3)) == [
        {"measure": 1, "time": "4/4", "start": "1:1", "parts": {"rh": "C5+E5:2 r:2", "lh": "C3:1"}},
        {"measure": 2, "time": "4/4", "start": "2:1", "parts": {"rh": "D5:4 F5:4 D5:2", "lh": "G3:1"}}]


def test_a_roll_made_score_combines_with_homr_results(tmp_path):
    homr = write(tmp_path, score(note("C5", 16) + backup(16) + note("C4", 16, staff=2)), "homr.musicxml")
    made = write(tmp_path, roll_xml.write_musicxml(waltz()).decode(), "made.musicxml")
    assert combine.concatenate([homr, made], "Suite", tmp_path / "suite.musicxml") == 5


def test_tuplets_are_snapped_and_counted():
    triplet = ("<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>")
    body = "".join(f"<note><pitch><step>{s}</step><octave>4</octave></pitch><duration>2</duration><voice>1</voice>"
                   f"<type>eighth</type>{triplet}</note>" for s in "CDE")
    xml = ('<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Flute</part-name></score-part>'
           '</part-list><part id="P1"><measure number="1"><attributes><divisions>6</divisions><time><beats>1</beats>'
           f'<beat-type>4</beat-type></time></attributes>{body}</measure></part></score-partwise>')
    doc, info = roll_xml.read_musicxml(xml.encode())
    assert info["grid"] == "32" and info["snapped"] == 3
    assert [(n["start"], n["dur"]) for n in doc["notes"]] == [(0, 180), (180, 120), (300, 180)]
    assert [(p["name"], p["clef"]) for p in doc["parts"]] == [("Flute", "G2")]


@pytest.mark.skipif(mscore_bin() is None, reason="MuseScore is not installed")
def test_musescore_reads_what_the_roll_writes(tmp_path):
    src = write(tmp_path, roll_xml.write_musicxml(busy_roll()).decode(), "roll.musicxml")
    dst = tmp_path / "out.musicxml"
    assert musescore_convert(mscore_bin(), src, dst) == "yes"
    again, _ = roll_xml.read_musicxml(dst.read_bytes())
    assert notes_of(again) == notes_of(roll_xml.read_musicxml(src.read_bytes())[0])
