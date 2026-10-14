import json

import pytest

import roll
import roll_ops
from pitch import Key, Mirror, Pitch, mirror_diatonic
from roll import RollError, TimeMap


def P(name):
    return Pitch.parse(name)


def waltz(**kw):
    """3/4 in D dorian: a right-hand tune over a left-hand bass, measures 1-3."""
    doc = roll.new_doc("Waltz", time="3/4", key="D dorian", measures=4, **kw)
    doc, _ = roll_ops.apply_operations(doc, [
        {"op": "add", "part": "rh", "notes": "D5:8 E5:8 F5:4 A5:4 | G5:2. | r:4 C5+E5:2"},
        {"op": "add", "part": "Left hand", "notes": "D3:2. | G2:2."},
    ])
    return doc


def bars(doc, measures="1-4", parts=None):
    lo, hi = (int(x) for x in measures.split("-"))
    return [b["parts"] for b in roll.streams(doc, range(lo, hi + 1), parts)]


# --- pitch ---------------------------------------------------------------------------------------------------
def test_chromatic_mirror_keeps_spelling_and_undoes_itself():
    about_d = Mirror.about(P("D4"))
    assert [about_d.apply(P(n)).name for n in ("F4", "Bb4", "G4", "D4")] == ["B3", "F#3", "A3", "D4"]
    assert about_d.apply(about_d.apply(P("Bb4"))) == P("Bb4")
    assert Mirror.between(P("D4"), P("A4")).apply(P("D4")).name == "A4"


def test_diatonic_mirror_flips_scale_steps_and_negates_chromatic_notes():
    dorian = Key.parse("D", "dorian")
    a4 = 2 * P("A4").d
    assert [mirror_diatonic(P(n), dorian, a4).name for n in ("A4", "B4", "C5", "D5", "G4", "F#4")] == [
        "A4", "G4", "F4", "E4", "B4", "Cb5"]
    lydian = Key.parse("F", "lydian")  # F G A B C D E: B is the scale's fourth
    assert mirror_diatonic(P("B4"), lydian, 2 * P("F4").d).name == "C4"
    phrygian = Key.parse("E", "phrygian")  # E F G A B C D
    assert mirror_diatonic(P("F4"), phrygian, 2 * P("E4").d).name == "D4"
    for name in ("C#5", "Eb4", "G4"):
        once = mirror_diatonic(P(name), dorian, a4)
        assert mirror_diatonic(once, dorian, a4) == P(name)


def test_keys_know_their_signature_and_spell_by_it():
    assert [Key.from_text(t).fifths for t in ("D dorian", "E phrygian", "F lydian", "Bb major", "F# minor")] == [
        0, 0, 0, -2, 3]
    assert str(Key.from_fifths(-2, "dorian")) == "C dorian" and str(Key.from_fifths(7)) == "C# major"
    assert [Key.parse("Bb").spell(m).name for m in (63, 64, 66, 70)] == ["Eb4", "E4", "Gb4", "Bb4"]
    assert [Key.parse("C#").spell(m).name for m in (60, 62, 65)] == ["B#3", "D4", "E#4"]


# --- the document --------------------------------------------------------------------------------------------
def test_durations_and_positions_read_and_write_alike():
    assert [roll.parse_duration(d) for d in ("4", "4.", "8..", "4~16", "64.", "128")] == [480, 720, 420, 600, 45, 15]
    assert [roll.format_duration(t) for t in (600, 720, 1680, 15)] == ["4~16", "4.", "2..", "128"]
    with pytest.raises(RollError, match="not a duration"):
        roll.parse_duration("128.")
    six_eight = roll.new_doc(time="6/8", measures=2)
    tm = TimeMap(six_eight)
    assert (tm.parse("2:1"), tm.parse("1:4"), tm.parse("1:2+16"), tm.parse("1:2.5")) == (1440, 720, 360, 360)
    assert tm.format(360) == "1:2+16" and tm.format(1440) == "2:1" and tm.format(tm.end) == "3:1"
    with pytest.raises(RollError, match="past the end of measure 1, which has 6 beats"):
        tm.parse("1:7")


def test_time_changes_and_a_pickup_measure_shape_the_bars():
    doc = roll.new_doc(time="4/4", measures=4)
    doc["time"].append({"measure": 3, "beats": 3, "beat_type": 4})
    doc["measure_lengths"] = {"1": 480}  # a one-beat pickup
    tm = TimeMap(roll.validate(doc))
    assert [tm.start(m) for m in range(1, 6)] == [0, 480, 2400, 3840, 5280]
    assert tm.format(500 - 20) == "2:1" and tm.format(2400 + 480) == "3:2"


def test_a_stream_reads_chords_rests_cursors_and_bar_checks():
    tm = TimeMap(roll.new_doc(time="3/4"))
    events = roll.parse_stream("D4:8 F4+A4:4 r:8 @1 A3:2 C4:4 |", 0, tm)
    assert [(e.start, e.dur, [p for p, _ in e.pitches]) for e in events] == [
        (0, 240, [62]), (240, 480, [65, 69]), (720, 240, []), (0, 960, [57]), (960, 480, [60])]
    with pytest.raises(RollError, match="token 2 '\\|': bar check failed: the cursor is at 1:2"):
        roll.parse_stream("C4:4 |", 0, tm)


def test_streams_read_back_as_written():
    doc = waltz()
    assert bars(doc) == [{"rh": "D5:8 E5:8 F5:4 A5:4", "lh": "D3:2."}, {"rh": "G5:2.", "lh": "G2:2."},
                         {"rh": "r:4 C5+E5:2", "lh": "r:2."}, {"rh": "r:2.", "lh": "r:2."}]
    again = roll.new_doc("Waltz", time="3/4", key="D dorian", measures=4)
    again, _ = roll_ops.apply_operations(again, [
        {"op": "add", "part": p, "at": f"{b['measure']}:1", "notes": b["parts"][p]}
        for b in roll.streams(doc, range(1, 5)) for p in ("rh", "lh")])
    assert bars(again) == bars(doc)


def test_a_second_voice_and_a_held_note_are_written_with_cursors():
    doc = roll.new_doc(time="4/4", measures=2, preset="single")
    doc, _ = roll_ops.apply_operations(doc, [
        {"op": "add", "part": "melody", "notes": "E5:4 D5:4 C5:2"},
        {"op": "add", "part": "melody", "at": "1:3", "notes": "G4:1"},
    ])
    assert bars(doc, "1-2") == [{"melody": "E5:4 D5:4 C5:2 @3 G4:1"}, {"melody": "r:1"}]


def test_normalize_trims_overlaps_and_rests_and_says_so():
    doc = roll.new_doc(measures=1, preset="single")
    doc["notes"] = [{"id": "n1", "part": "melody", "pitch": 60, "start": 0, "dur": 960, "vel": 80},
                    {"id": "n2", "part": "melody", "pitch": 60, "start": 480, "dur": 480, "vel": 80},
                    {"id": "n3", "part": "melody", "pitch": 64, "start": 1440, "dur": 960, "vel": 80}]
    doc["rests"] = [{"id": "n4", "part": "melody", "start": 0, "dur": 1920, "row": 60}]
    warnings = roll.normalize(roll.validate(doc))
    assert [(n["start"], n["dur"]) for n in doc["notes"]] == [(0, 480), (480, 480), (1440, 480)]
    assert [(r["start"], r["dur"]) for r in doc["rests"]] == [(960, 480)]
    assert len(warnings) == 2 and any("the same pitch starts again" in w for w in warnings)
    assert any("end with the score" in w for w in warnings)


def test_validate_refuses_broken_documents():
    doc = roll.new_doc()
    doc["notes"] = [{"id": "x", "part": "nope", "pitch": 60, "start": 0, "dur": 480}]
    with pytest.raises(RollError, match="note x is in no known part"):
        roll.validate(doc)
    doc = roll.new_doc()
    doc["notes"] = [{"id": "x", "part": "rh", "pitch": 60, "start": 7, "dur": 480}]
    with pytest.raises(RollError, match="multiple of 15"):
        roll.validate(doc)
    with pytest.raises(RollError, match="min <= default <= max"):
        roll.new_doc(grid={"min": "4", "default": "8"})
    with pytest.raises(RollError, match="format must be 'yamba-roll'"):
        roll.validate(json.loads('{"format": "something else"}'))


# --- operations ----------------------------------------------------------------------------------------------
def test_mirrors_retrograde_and_their_combination():
    doc = waltz()
    rh_m1 = {"measures": "1", "parts": ["rh"]}
    chromatic, _ = roll_ops.apply_operations(doc, [{"op": "mirror", "select": rh_m1, "center": "A4"}])
    assert bars(chromatic, "1-1")[0]["rh"] == "E4:8 D4:8 C#4:4 A3:4"  # semitones: F5 is 8 above A4, C#4 8 below
    diatonic, report = roll_ops.apply_operations(doc, [
        {"op": "mirror", "select": rh_m1, "kind": "diatonic", "center": "A4"}])
    assert bars(diatonic, "1-1")[0]["rh"] == "E4:8 D4:8 C4:4 A3:4" and report["applied"][0]["key"] == "D dorian"
    backwards, _ = roll_ops.apply_operations(doc, [{"op": "retrograde", "select": {"measures": "1-2", "parts": "rh"}}])
    assert bars(backwards, "1-2") == [{"rh": "G5:2.", "lh": "D3:2."}, {"rh": "A5:4 F5:4 E5:8 D5:8", "lh": "G2:2."}]
    ri, _ = roll_ops.apply_operations(doc, [{"op": "retrograde_inversion", "select": rh_m1, "kind": "diatonic",
                                             "center": "A4"}])
    assert bars(ri, "1-1")[0]["rh"] == "A3:4 C4:4 D4:8 E4:8"


def test_mirror_and_retrograde_of_the_whole_score():
    doc = waltz()
    flipped, _ = roll_ops.apply_operations(doc, [{"op": "retrograde"}, {"op": "retrograde"}])
    assert bars(flipped) == bars(doc)
    mirrored, _ = roll_ops.apply_operations(doc, [{"op": "mirror", "center": "D4"}])
    assert bars(mirrored, "2-2")[0] == {"rh": "A2:2.", "lh": "A5:2."}  # G5 is 17 above D4, A2 17 below


def test_a_diatonic_center_must_be_in_the_scale():
    with pytest.raises(RollError, match="operation 1 \\(mirror\\): the center Bb4 is not a note of D dorian "
                                        "\\(D E F G A B C\\)"):
        roll_ops.apply_operations(waltz(), [{"op": "mirror", "kind": "diatonic", "center": "Bb4"}])


def test_scale_time_stretches_from_the_start_and_pushes_later_music():
    doc = waltz()
    longer, report = roll_ops.apply_operations(doc, [
        {"op": "scale_time", "select": {"measures": "1", "parts": ["rh"]}, "factor": "2"}])
    assert bars(longer, "1-4") == [{"rh": "D5:4 E5:4 F5:2", "lh": "D3:2."}, {"rh": "@2 A5:2", "lh": "G2:2."},
                                   {"rh": "G5:2.", "lh": "r:2."}, {"rh": "r:4 C5+E5:2", "lh": "r:2."}]
    assert report["applied"][0]["pushed_later_notes"] == 4 and longer["measures"] == 4
    shorter, _ = roll_ops.apply_operations(doc, [{"op": "scale_time", "select": {"measures": "2"}, "factor": "1/3"}])
    assert bars(shorter, "2-2")[0] == {"rh": "G5:4 r:4 C5+E5:2", "lh": "G2:4 r:2"}
    with pytest.raises(RollError, match="off the 16th grid; set_grid min=32 first"):
        roll_ops.apply_operations(doc, [{"op": "scale_time", "select": {"measures": "1"}, "factor": "1/4"}])


def test_scaling_the_whole_score_grows_or_shrinks_it():
    doc = waltz()
    assert roll_ops.apply_operations(doc, [{"op": "scale_time", "factor": "2"}])[0]["measures"] == 8
    assert roll_ops.apply_operations(doc, [{"op": "scale_time", "factor": "1/2"}])[0]["measures"] == 2


def test_change_move_copy_delete_and_transpose():
    doc = waltz()
    changed, _ = roll_ops.apply_operations(doc, [
        {"op": "change", "select": {"at": "1:1.5", "pitches": "E5"}, "pitch": "F#5"},
        {"op": "move", "select": {"measures": "3", "parts": "rh"}, "measures": 1},
        {"op": "copy", "select": {"measures": "1", "parts": "lh"}, "to": "3:1"},
        {"op": "delete", "select": {"measures": "2", "parts": "lh"}},
        {"op": "transpose", "select": {"measures": "1", "parts": "rh"}, "steps": 1},
    ])
    assert bars(changed) == [{"rh": "E5:8 G#5:8 G5:4 B5:4", "lh": "D3:2."}, {"rh": "G5:2.", "lh": "r:2."},
                             {"rh": "r:2.", "lh": "D3:2."}, {"rh": "r:4 C5+E5:2", "lh": "r:2."}]
    with pytest.raises(RollError, match="nothing matches .*there: F5 at 1:2 \\(Right hand\\)"):
        roll_ops.apply_operations(doc, [{"op": "change", "select": {"at": "1:2", "pitches": "E4"}, "pitch": "F4"}])


def test_adding_past_the_end_grows_the_score_and_measures_can_be_inserted_and_deleted():
    doc = waltz()
    grown, report = roll_ops.apply_operations(doc, [{"op": "add", "part": "rh", "at": "6:1", "notes": "D5:2."}])
    assert grown["measures"] == 6 and report["applied"][0]["score_grew_to_measures"] == 6
    edited, _ = roll_ops.apply_operations(doc, [
        {"op": "insert_measures", "at": 2, "count": 1},
        {"op": "set_time", "measure": 2, "time": "2/4"},
        {"op": "set_time", "measure": 3, "time": "3/4"},
        {"op": "delete_measures", "measures": "4"},
    ])
    assert edited["measures"] == 4 and [b["time"] for b in roll.streams(edited, range(1, 5))] == [
        "3/4", "2/4", "3/4", "3/4"]
    assert bars(edited)[1:3] == [{"rh": "r:2", "lh": "r:2"}, {"rh": "G5:2.", "lh": "G2:2."}]
    with pytest.raises(RollError, match="the A5 at 1:3 \\(Right hand\\) would fall outside measure 1 in 2/4"):
        roll_ops.apply_operations(doc, [{"op": "set_time", "time": "2/4"}])


def test_operations_are_all_or_nothing_and_name_the_failing_one():
    doc = waltz()
    before = roll.dump(doc)
    with pytest.raises(RollError, match="operation 2 \\(transpose\\): give semitones"):
        roll_ops.apply_operations(doc, [{"op": "delete", "select": {"measures": "1"}}, {"op": "transpose"}])
    with pytest.raises(RollError, match="unknown op 'invert'"):
        roll_ops.apply_operations(doc, [{"op": "invert"}])
    with pytest.raises(RollError, match="unknown field\\(s\\) colour; add_part takes"):
        roll_ops.apply_operations(doc, [{"op": "add_part", "name": "Flute", "colour": "#ff0000"}])
    assert roll.dump(doc) == before


def test_parts_grid_key_and_tempo_ops():
    doc = waltz()
    doc, report = roll_ops.apply_operations(doc, [
        {"op": "add_part", "name": "Flute", "program": 73},
        {"op": "add", "part": "flute", "at": "1:1", "notes": "A5:2."},
        {"op": "set_grid", "min": "32"},
        {"op": "add", "part": "flute", "at": "2:1+32", "notes": "B5:32"},
        {"op": "set_key", "key": "G mixolydian", "measure": 3},
        {"op": "set_tempo", "bpm": 88},
        {"op": "remove_part", "part": "lh"},
    ])
    assert [p["id"] for p in doc["parts"]] == ["rh", "flute"] and [g["name"] for g in doc["groups"]] == ["Piano", "Flute"]
    assert doc["key"] == [{"measure": 1, "tonic": "D", "mode": "dorian"}, {"measure": 3, "tonic": "G", "mode": "mixolydian"}]
    assert doc["tempo"] == [{"tick": 0, "bpm": 88.0}] and report["applied"][0]["part"] == "flute"
    with pytest.raises(RollError, match="min <= default <= max"):
        roll_ops.apply_operations(doc, [{"op": "set_grid", "min": "4", "default": "8"}])


def test_the_operations_schema_lists_every_op():
    schema = roll_ops.ops_schema()
    assert [v["properties"]["op"]["enum"][0] for v in schema["anyOf"]] == list(roll_ops.OPS)
    mirror = next(v for v in schema["anyOf"] if v["properties"]["op"]["enum"] == ["mirror"])
    assert mirror["required"] == ["op", "center"] and mirror["additionalProperties"] is False
