"""A piano roll as partwise MusicXML, and MusicXML as a piano roll.

write_musicxml: each instrument (group) is a MusicXML part and each of its roll parts a staff, so a piano
roll (right hand, left hand) is one part with two staves, like HOMR's results, and combines with them.
In a staff, notes that start together and last as long are a chord; overlapping ones go to further
voices (1-4 on staff 1, 5-8 on staff 2, as MuseScore numbers them). Notes are split at barlines and into
notatable values, tied; voice 1 fills its gaps with rests, other voices skip them with <forward>.

read_musicxml: every part, staff and voice; tied notes become one note; tuplets and other times off the
grid are snapped to it and counted; grace and unpitched notes are left out and counted. Pitches are what
the <pitch> says (sounding); an octave clef is kept as the part's clef (G2+1, G2-1).
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from fractions import Fraction

import roll
from pitch import Key, Pitch, mode_name
from roll import TPQ, TimeMap

TYPES = {1920: "whole", 960: "half", 480: "quarter", 240: "eighth", 120: "16th", 60: "32nd", 30: "64th", 15: "128th"}
SOFTWARE = "Yamba Music piano roll"


def _sub(parent: ET.Element, tag: str, text=None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k.replace("_", "-"): str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _clef(attrs: ET.Element, number: int | None, clef: str) -> None:
    el = _sub(attrs, "clef", **({"number": number} if number else {}))
    if clef == "percussion":
        _sub(el, "sign", "percussion")
        return
    m = re.fullmatch(r"([GFC])([1-5])([+-][12])?", clef)
    _sub(el, "sign", m[1])
    _sub(el, "line", m[2])
    if m[3]:
        _sub(el, "clef-octave-change", int(m[3]))


def _value(ticks: int) -> tuple[str, int]:
    """(type, dots) of a single (possibly dotted) value."""
    for dots in range(3):
        base = Fraction(ticks * (1 << dots), (1 << (dots + 1)) - 1)
        if base.denominator == 1 and int(base) in TYPES:
            return TYPES[int(base)], dots
    raise roll.RollError(f"{ticks} ticks is not a single note value")


def write_musicxml(doc: dict) -> bytes:
    tm = TimeMap(doc)
    values = [TPQ, *(tm.start(m) for m in range(1, tm.measures + 2)), *(e["tick"] for e in doc["tempo"])]
    values += [x for n in doc["notes"] for x in (n["start"], n["dur"])]
    unit = math.gcd(*values)
    div = lambda t: t // unit  # noqa: E731
    keys = roll.expand(doc["key"], tm.measures, "key")

    root = ET.Element("score-partwise", version="4.0")
    work = _sub(root, "work")
    _sub(work, "work-title", doc["title"] or "Untitled")
    ident = _sub(root, "identification")
    _sub(_sub(ident, "encoding"), "software", SOFTWARE)
    part_list = _sub(root, "part-list")
    channels = {g["id"]: c for g, c in zip(doc["groups"], _channels(len(doc["groups"])))}
    for g in doc["groups"]:
        sp = _sub(part_list, "score-part", id=g["id"])
        _sub(sp, "part-name", g["name"])
        _sub(_sub(sp, "score-instrument", id=f"{g['id']}-I1"), "instrument-name", g["name"])
        mi = _sub(sp, "midi-instrument", id=f"{g['id']}-I1")
        _sub(mi, "midi-channel", channels[g["id"]] + 1)
        _sub(mi, "midi-program", g["program"] + 1)

    for gi, g in enumerate(doc["groups"]):
        staves = sorted((p for p in doc["parts"] if p["group"] == g["id"]), key=lambda p: p["staff"])
        voices = [roll.voice_groups([n for n in doc["notes"] if n["part"] == p["id"]]) for p in staves]
        part = _sub(root, "part", id=g["id"])
        for m in range(1, tm.measures + 1):
            ms, me = tm.start(m), tm.start(m + 1)
            measure = _sub(part, "measure", number=m)
            if m == 1 and me - ms < roll.capacity(tm.sig(1)):
                measure.set("implicit", "yes")
            new_key = m == 1 or keys[m - 1] != keys[m - 2]
            new_time = m == 1 or tm.sig(m) != tm.sig(m - 1)
            if new_key or new_time:
                attrs = _sub(measure, "attributes")
                if m == 1:
                    _sub(attrs, "divisions", div(TPQ))
                if new_key:
                    key = _sub(attrs, "key")
                    _sub(key, "fifths", keys[m - 1].fifths)
                    _sub(key, "mode", keys[m - 1].mode)
                if new_time:
                    time = _sub(attrs, "time")
                    _sub(time, "beats", tm.sig(m)[0])
                    _sub(time, "beat-type", tm.sig(m)[1])
                if m == 1:
                    if len(staves) > 1:
                        _sub(attrs, "staves", len(staves))
                    for i, p in enumerate(staves, 1):
                        _clef(attrs, i if len(staves) > 1 else None, p["clef"])
            if gi == 0:
                for e in doc["tempo"]:
                    if ms <= e["tick"] < me:
                        d = _sub(measure, "direction", placement="above")
                        metro = _sub(_sub(d, "direction-type"), "metronome")
                        _sub(metro, "beat-unit", "quarter")
                        _sub(metro, "per-minute", f"{e['bpm']:g}")
                        if e["tick"] > ms:
                            _sub(d, "offset", div(e["tick"] - ms))
                        _sub(d, "sound", tempo=f"{e['bpm']:g}")
            pos = ms
            for si, (p, staff_voices) in enumerate(zip(staves, voices), 1):
                key = keys[m - 1]
                for v, groups in enumerate(staff_voices or [[]]):
                    inside = [gr for gr in groups if gr[0] < me and gr[0] + gr[1] > ms]
                    if v > 0 and not inside:
                        continue
                    if pos > ms:
                        _sub(_sub(measure, "backup"), "duration", div(pos - ms))
                    number = (si - 1) * 4 + 1 + v
                    staff = si if len(staves) > 1 else None
                    pos = _write_voice(measure, inside, ms, me, tm.beat(m), number, staff, v == 0, key, div)
    ET.indent(root, space="  ")
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 '
            b'Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">\n' + ET.tostring(root, encoding="utf-8"))


def _channels(n: int) -> list[int]:
    free = [c for c in range(16) if c != 9]
    return [free[i % len(free)] for i in range(n)]


def _write_voice(measure: ET.Element, groups, ms: int, me: int, beat: int, voice: int, staff: int | None,
                 fill: bool, key: Key, div) -> int:
    cursor = ms

    def gap(until: int) -> None:
        if fill:
            if cursor == ms and until == me:
                rest = _sub(measure, "note")
                _sub(rest, "rest", measure="yes")
                _sub(rest, "duration", div(me - ms))
                _sub(rest, "voice", voice)
                if staff:
                    _sub(rest, "staff", staff)
                return
            at = cursor
            for piece in roll.split_value(at - ms, until - at, beat):
                rest = _sub(measure, "note")
                _sub(rest, "rest")
                _note_tail(rest, piece, voice, staff, div)
                at += piece
        else:
            fwd = _sub(measure, "forward")
            _sub(fwd, "duration", div(until - cursor))
            _sub(fwd, "voice", voice)
            if staff:
                _sub(fwd, "staff", staff)

    for start, dur, notes in groups:
        seg_start, seg_end = max(start, ms), min(start + dur, me)
        if seg_start > cursor:
            gap(seg_start)
        at = seg_start
        for piece in roll.split_value(seg_start - ms, seg_end - seg_start, beat):
            tie_stop, tie_start = at > start, at + piece < start + dur
            for i, n in enumerate(notes):
                el = _sub(measure, "note")
                if n["vel"] != 80:
                    el.set("dynamics", f"{n['vel'] * 100 / 90:.2f}")
                if i:
                    _sub(el, "chord")
                p = roll.spelled(n, key)
                pitch = _sub(el, "pitch")
                _sub(pitch, "step", p.step)
                if p.alter:
                    _sub(pitch, "alter", p.alter)
                _sub(pitch, "octave", p.octave)
                _note_tail(el, piece, voice, staff, div, tie_stop, tie_start)
            at += piece
        cursor = seg_end
    if cursor < me and fill:
        gap(me)
        cursor = me
    return cursor


def _note_tail(el: ET.Element, ticks: int, voice: int, staff: int | None, div, tie_stop: bool = False,
               tie_start: bool = False) -> None:
    _sub(el, "duration", div(ticks))
    if tie_stop:
        _sub(el, "tie", type="stop")
    if tie_start:
        _sub(el, "tie", type="start")
    _sub(el, "voice", voice)
    kind, dots = _value(ticks)
    _sub(el, "type", kind)
    for _ in range(dots):
        _sub(el, "dot")
    if staff:
        _sub(el, "staff", staff)
    if tie_stop or tie_start:
        notations = _sub(el, "notations")
        if tie_stop:
            _sub(notations, "tied", type="stop")
        if tie_start:
            _sub(notations, "tied", type="start")


# --- reading ---------------------------------------------------------------------------------------------------
def _clef_text(el: ET.Element) -> str:
    sign = el.findtext("sign", "G")
    if sign == "percussion":
        return "percussion"
    line = el.findtext("line") or {"G": "2", "F": "4", "C": "3"}.get(sign, "2")
    change = int(el.findtext("clef-octave-change", "0") or 0)
    clef = f"{sign}{line}" + (f"{change:+d}" if change in (-2, -1, 1, 2) else "")
    return clef if re.fullmatch(r"[GFC][1-5]([+-][12])?", clef) else "G2"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24] or "part"


def read_musicxml(data: bytes, title: str = "") -> tuple[dict, dict]:
    """A roll from MusicXML bytes, and what happened: {'snapped': notes moved to the grid, 'dropped':
    {'grace': n, 'unpitched': n}, 'grid': the grid}."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise roll.RollError(f"not valid XML ({e})") from None
    if root.tag != "score-partwise":
        raise roll.RollError(f"not a partwise MusicXML score (its root is <{root.tag}>)")
    xml_parts = root.findall("part")
    if not xml_parts:
        raise roll.RollError("the score has no parts")
    info_names = {sp.get("id"): (sp.findtext("part-name") or "", sp.findtext("midi-instrument/midi-program"))
                  for sp in root.findall("part-list/score-part")}
    dropped = {"grace": 0, "unpitched": 0}
    lengths: list[Fraction] = []  # from the first part
    times: dict[int, tuple[int, int]] = {}
    keys: dict[int, Key] = {}
    tempos: list[tuple[Fraction, float]] = []
    raw_parts = []
    for pi, xp in enumerate(xml_parts):
        divisions, staves, clefs, time = 1, 1, {}, (4, 4)
        raw, start = [], Fraction(0)
        for mi, measure in enumerate(xp.findall("measure"), 1):
            pos = max_pos = last_onset = Fraction(0)
            for el in measure:
                if el.tag == "attributes":
                    divisions = int(el.findtext("divisions") or divisions)
                    staves = int(el.findtext("staves") or staves)
                    if (t := el.find("time")) is not None and t.findtext("beats"):
                        try:
                            time = roll.parse_time(f"{t.findtext('beats')}/{t.findtext('beat-type')}")
                        except roll.RollError:
                            pass
                        if pi == 0:
                            times[mi] = time
                    if pi == 0 and (k := el.find("key")) is not None and k.findtext("fifths"):
                        try:
                            mode = mode_name(k.findtext("mode") or "major")
                        except ValueError:
                            mode = "major"
                        keys[mi] = Key.from_fifths(max(-7, min(7, int(k.findtext("fifths")))), mode)
                    for c in el.findall("clef"):
                        clefs.setdefault(int(c.get("number", "1")), _clef_text(c))
                elif el.tag in ("backup", "forward"):
                    d = Fraction(int(el.findtext("duration") or 0) * TPQ, divisions)
                    pos += -d if el.tag == "backup" else d
                    max_pos = max(max_pos, pos)
                elif el.tag in ("direction", "sound") and pi == 0:
                    sound = el if el.tag == "sound" else el.find("sound")
                    if sound is not None and sound.get("tempo"):
                        offset = Fraction(int(el.findtext("offset") or 0) * TPQ, divisions)
                        tempos.append((start + pos + offset, float(sound.get("tempo"))))
                elif el.tag == "note":
                    if el.find("grace") is not None or el.find("cue") is not None:
                        dropped["grace"] += el.find("grace") is not None
                        continue
                    d = Fraction(int(el.findtext("duration") or 0) * TPQ, divisions)
                    chord = el.find("chord") is not None
                    onset = last_onset if chord else pos
                    if not chord:
                        last_onset, pos = onset, onset + d
                        max_pos = max(max_pos, pos)
                    if el.find("unpitched") is not None:
                        dropped["unpitched"] += 1
                        continue
                    p = el.find("pitch")
                    if p is None or d <= 0:
                        continue
                    alter = round(float(p.findtext("alter") or 0))
                    pitch = Pitch.from_parts(p.findtext("step"), alter, int(p.findtext("octave")))
                    ties = {t.get("type") for t in el.findall("tie")} | {t.get("type") for t in el.findall("notations/tied")}
                    dyn = el.get("dynamics")
                    raw.append({"staff": int(el.findtext("staff") or 1), "pitch": pitch, "start": start + onset,
                                "dur": d, "stop": "stop" in ties, "tie": "start" in ties,
                                "vel": max(1, min(127, round(float(dyn) * 90 / 100))) if dyn else 80})
            nominal = Fraction(roll.capacity(time))
            if pi == 0:
                content = max_pos
                length = content if measure.get("implicit") == "yes" and 0 < content else max(nominal, content)
                lengths.append(length)
            start += lengths[mi - 1] if mi - 1 < len(lengths) else nominal
        raw_parts.append((xp.get("id"), staves, clefs, raw))

    measure_starts = [Fraction(0)]
    for length in lengths:
        measure_starts.append(measure_starts[-1] + length)
    exact = [x for _, _, _, raw in raw_parts for n in raw for x in (n["start"], n["start"] + n["dur"])]
    grid = next((g for g in ("16", "32", "64") if all((x / roll.VALUES[g]).denominator == 1
                                                     for x in exact + measure_starts)), "32")
    q = roll.VALUES[grid]
    snap = lambda x: int(round(x / q)) * q  # noqa: E731

    doc = roll.new_doc(title=title, preset="single", grid={"min": grid, "default": "8" if q <= 240 else grid,
                                                          "max": "4" if q <= 480 else grid})
    doc["title"] = root.findtext("work/work-title") or root.findtext("movement-title") or title
    doc["groups"], doc["parts"], doc["notes"] = [], [], []
    single = len(raw_parts) == 1
    snapped = 0
    for gid, staves, clefs, raw in raw_parts:
        name, program = info_names.get(gid, ("", None))
        group = {"id": gid or f"P{len(doc['groups']) + 1}", "name": name or gid or "Part",
                 "program": max(0, min(127, int(program) - 1)) if program and program.isdigit() else 0}
        doc["groups"].append(group)
        part_ids = {}
        for s in range(1, staves + 1):
            if single and staves == 2:
                pid, pname = ("rh", "Right hand") if s == 1 else ("lh", "Left hand")
            elif staves == 1:
                pid, pname = (_slug(group["name"]) if not single else "melody"), group["name"]
            else:
                pid, pname = f"{_slug(group['name'])}-{s}", f"{group['name']} {s}"
            while pid in {p["id"] for p in doc["parts"]}:
                pid += "-2"
            part_ids[s] = pid
            doc["parts"].append({"id": pid, "name": pname, "color": roll.COLORS[len(doc["parts"]) % len(roll.COLORS)],
                                 "group": group["id"], "staff": s,
                                 "clef": clefs.get(s, "G2" if s == 1 else "F4")})
        open_ties: dict[tuple[int, int], dict] = {}
        for n in sorted(raw, key=lambda n: n["start"]):
            k = (n["staff"], n["pitch"].c)
            held = open_ties.get(k)
            if n["stop"] and held and held["end"] == n["start"]:
                held["end"] = n["start"] + n["dur"]
                if not n["tie"]:
                    del open_ties[k]
                continue
            note = {"staff": n["staff"], "pitch": n["pitch"], "start": n["start"], "end": n["start"] + n["dur"],
                    "vel": n["vel"]}
            n["note"] = note
            if n["tie"]:
                open_ties[k] = note
        for n in raw:
            note = n.get("note")
            if not note:
                continue
            s, e = snap(note["start"]), snap(note["end"])
            e = max(e, s + q)
            snapped += s != note["start"] or e != note["end"]
            out = {"id": roll.new_id(doc), "part": part_ids.get(note["staff"], part_ids[1]),
                   "pitch": note["pitch"].c, "start": s, "dur": e - s, "vel": note["vel"]}
            if -2 <= note["pitch"].alter <= 2:
                out["name"] = note["pitch"].name
            doc["notes"].append(out)

    measures = max(1, len(lengths))
    doc["measures"] = measures
    sig, entries, lengths_out = (4, 4), [], {}
    for m in range(1, measures + 1):
        if m in times and times[m] != sig or m == 1:
            sig = times.get(m, sig)
            entries.append({"measure": m, "beats": sig[0], "beat_type": sig[1]})
        length = snap(lengths[m - 1]) if lengths else roll.capacity(sig)
        if length != roll.capacity(sig) and length > 0:
            lengths_out[str(m)] = length
    doc["time"], doc["measure_lengths"] = entries, lengths_out
    doc["key"] = [{"measure": m, "tonic": k.tonic_name, "mode": k.mode} for m, k in sorted(keys.items())] or \
        [{"measure": 1, "tonic": "C", "mode": "major"}]
    if doc["key"][0]["measure"] != 1:
        doc["key"].insert(0, {"measure": 1, "tonic": "C", "mode": "major"})
    doc["tempo"] = [{"tick": snap(t) // roll.QUANTUM * roll.QUANTUM, "bpm": bpm} for t, bpm in tempos[:50]
                    if 10 <= bpm <= 400] or [{"tick": 0, "bpm": 120}]
    doc["tempo"] = list({e["tick"]: e for e in doc["tempo"]}.values())
    roll.validate(doc)
    warnings = roll.normalize(doc)
    return doc, {"snapped": snapped, "dropped": {k: v for k, v in dropped.items() if v}, "grid": grid,
                 "warnings": warnings}
