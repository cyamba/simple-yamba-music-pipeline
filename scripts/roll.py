"""The piano roll document (rolls/<name>.roll.json): what the editor shows and the agent tools edit.

Time is in integer ticks, 480 to the quarter note; every tick value is a multiple of 15 (a 128th). A roll
has groups (instruments, one MusicXML part each) and parts (one staff each, with its own colour); notes
belong to a part, so a piano is one group with a right-hand and a left-hand part. Rests are markers the
user placed: on a timeline a gap already is a rest, so they only show intent and can extend the score.

Text forms shared by the tools and get_piano_roll:
  duration   1 2 4 8 16 32 64 128 (whole .. 128th), '.' or '..' for dots, '~' ties: 4. 8 4~16
  position   measure:beat, beats counted in the time signature's beat unit (in 6/8 a beat is an
             eighth): 3:2, 3:2.5, 3:2+16 (a 16th after beat 2), or 3 for the start of measure 3
  stream     D4:8 F4+A4:4 r:8 -- notes, chords and rests one after another; @3:2 (or @2, a beat in the
             stream's first measure) moves the cursor, which is how a second voice is written; | checks
             that the cursor is on a barline
  selection  {parts, measures: "5-6", start, end, at, pitches: "C4-G5" | ["E4"], ids}; conditions are
             ANDed, a note is in it when it starts inside the range, and an empty selection is the score
"""
from __future__ import annotations

import bisect
import copy
import json
import re
from dataclasses import dataclass
from fractions import Fraction

from pitch import Key, Pitch

FORMAT = "yamba-roll"
TPQ = 480
QUANTUM = 15  # a 128th
WHOLE = 4 * TPQ
VALUES = {"1": 1920, "2": 960, "4": 480, "8": 240, "16": 120, "32": 60, "64": 30, "128": 15}
GRID_NAMES = {v: k for k, v in VALUES.items()}
COLORS = ["#6fe3cf", "#b9a6ff", "#f3c47a", "#ff8fa3", "#7cc4ff", "#a5e07a", "#ffb37a", "#d39bff"]
DEFAULT_GRID = {"min": "16", "default": "8", "max": "4"}
MAX_MEASURES = 2000
MAX_NOTES = 50000
SELECT_KEYS = {"parts", "measures", "start", "end", "at", "pitches", "ids"}


class RollError(ValueError):
    pass


# --- durations -------------------------------------------------------------------------------------------------
def parse_duration(text: str | int) -> int:
    """'4' -> 480, '4.' -> 720, '8..' -> 420, '4~16' -> 600."""
    total = 0
    for piece in str(text).strip().split("~"):
        m = re.fullmatch(r"(128|64|32|16|8|4|2|1)(\.{0,2})", piece.strip())
        base = VALUES.get(m[1]) if m else None
        if base is None or base % (1 << len(m[2])) or (base >> len(m[2])) % QUANTUM:
            raise RollError(f"not a duration: {text!r} (use 1 2 4 8 16 32 64 128, '.' for dotted, '~' to tie, "
                            "e.g. 4. or 2~8)")
        total += sum(base >> k for k in range(len(m[2]) + 1))
    return total


DURATION_NAMES = sorted(((sum(v >> k for k in range(dots + 1)), name + "." * dots)
                         for name, v in VALUES.items() for dots in range(3)
                         if v % (1 << dots) == 0 and (v >> dots) % QUANTUM == 0), reverse=True)


def format_duration(ticks: int) -> str:
    """The inverse of parse_duration: a single value if there is one, else a tie (2~8)."""
    if ticks <= 0 or ticks % QUANTUM:
        raise RollError(f"{ticks} ticks is not a duration")
    parts = []
    while ticks:
        ticks_, name = next((v, n) for v, n in DURATION_NAMES if v <= ticks)
        parts.append(name)
        ticks -= ticks_
    return "~".join(parts)


def grid_ticks(name: str) -> int:
    if str(name) not in VALUES:
        raise RollError(f"grid values are {', '.join(VALUES)}, not {name!r}")
    return VALUES[str(name)]


def finest_grid(ticks: int) -> str:
    """The coarsest grid value that ticks lies on, e.g. 600 -> '16'."""
    return next(GRID_NAMES[v] for v in sorted(GRID_NAMES, reverse=True) if ticks % v == 0)


def split_value(offset: int, length: int, beat: int) -> list[int]:
    """Notatable pieces for `length` ticks starting `offset` ticks into a measure. Off the beat a piece is an
    undotted value aligned to itself that stops at the next beat; on a beat any value (one dot at most)
    may start, a shorter-than-a-beat one only where it is aligned to itself."""
    out = []
    while length > 0:
        on_beat = offset % beat == 0
        room = length if on_beat else min(length, beat - offset % beat)
        for value, name in DURATION_NAMES:
            if value > room or name.endswith("..") or (not on_beat and name.endswith(".")):
                continue
            base = VALUES[name.rstrip(".")]
            if offset % base == 0 or (on_beat and base >= beat):
                break
        else:
            raise RollError(f"can't notate {length} ticks at offset {offset}")
        out.append(value)
        offset += value
        length -= value
    return out


# --- pitches ---------------------------------------------------------------------------------------------------
def parse_pitch(text: str | int) -> tuple[int, str | None]:
    """'Bb4' -> (70, 'Bb4'); 70 -> (70, None)."""
    if isinstance(text, int) or str(text).strip().isdigit():
        midi = int(text)
        name = None
    else:
        p = Pitch.parse(str(text))
        midi, name = p.c, p.name
    if not 0 <= midi <= 127:
        raise RollError(f"{text} is outside MIDI's range (C-1 .. G9)")
    return midi, name


def spelled(note: dict, key: Key) -> Pitch:
    """The note's pitch with its stored spelling, or as the key spells it."""
    if note.get("name"):
        p = Pitch.parse(note["name"])
        if p.c == note["pitch"]:
            return p
    return key.spell(note["pitch"])


def pitch_name(midi: int, key: Key | None = None) -> str:
    return (key or Key.parse("C")).spell(midi).name


# --- the time map ----------------------------------------------------------------------------------------------
def parse_time(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(text))
    if not m or int(m[1]) < 1 or int(m[1]) > 64 or int(m[2]) not in (1, 2, 4, 8, 16, 32, 64):
        raise RollError(f"not a time signature: {text!r} (e.g. 4/4, 3/4, 6/8)")
    return int(m[1]), int(m[2])


class TimeMap:
    """Measures and beats over ticks. Past the last measure it carries on with the last time signature,
    so a position after the end can be named (an edit there grows the score)."""

    def __init__(self, doc: dict):
        self.measures = doc["measures"]
        self.sigs = expand(doc["time"], self.measures, "time")
        lengths = {int(k): v for k, v in doc.get("measure_lengths", {}).items()}
        self.starts, t = [], 0
        for m, sig in enumerate(self.sigs, 1):
            self.starts.append(t)
            t += lengths.get(m, capacity(sig))
        self.end = t

    def sig(self, m: int) -> tuple[int, int]:
        return self.sigs[min(m, self.measures) - 1]

    def start(self, m: int) -> int:
        if m <= self.measures:
            return self.starts[m - 1]
        return self.end + (m - 1 - self.measures) * capacity(self.sigs[-1])

    def length(self, m: int) -> int:
        return self.start(m + 1) - self.start(m)

    def beat(self, m: int) -> int:
        return WHOLE // self.sig(m)[1]

    def measure_at(self, tick: int) -> int:
        if tick < self.end:
            return bisect.bisect_right(self.starts, tick)
        return self.measures + 1 + (tick - self.end) // capacity(self.sigs[-1])

    def measures_to_cover(self, tick: int) -> int:
        """The measure count that makes the score at least `tick` long."""
        if tick <= self.end:
            return self.measures
        return self.measures + -(-(tick - self.end) // capacity(self.sigs[-1]))

    def parse(self, text: str | int) -> int:
        """'3:2', '3:2.5', '3:2+16', '3' -> ticks."""
        s = str(text).strip()
        m = re.fullmatch(r"(\d+)(?::(\d+(?:\.\d+)?|\d+/\d+))?(?:\+(.+))?", s)
        if not m:
            raise RollError(f"not a position: {text!r} (use measure:beat, e.g. 3:2, 3:2.5 or 3:2+16)")
        measure = int(m[1])
        if measure < 1:
            raise RollError(f"measures count from 1, not {text!r}")
        beat = Fraction(m[2]) if m[2] else Fraction(1)
        offset = (beat - 1) * self.beat(measure)
        if beat < 1 or offset.denominator != 1:
            raise RollError(f"beat {m[2]} is not a beat position in {text!r}")
        offset = int(offset) + (parse_duration(m[3]) if m[3] else 0)
        if offset >= self.length(measure) and not (offset == 0):
            beats = self.length(measure) / self.beat(measure)
            raise RollError(f"{text!r} is past the end of measure {measure}, which has {beats:g} beats")
        return self.start(measure) + offset

    def format(self, tick: int) -> str:
        m = self.measure_at(tick)
        offset = tick - self.start(m)
        beat, rest = divmod(offset, self.beat(m))
        return f"{m}:{beat + 1}" + (f"+{format_duration(rest)}" if rest else "")

    def measure_range(self, spec: str | int) -> tuple[int, int, int, int]:
        """'5-6' or 5 -> (first, last, start tick, end tick)."""
        text = str(spec).replace(" ", "")
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", text)
        if not m:
            raise RollError(f"measures look like '5' or '5-6', not {spec!r}")
        lo, hi = int(m[1]), int(m[2] or m[1])
        if not 1 <= lo <= hi:
            raise RollError(f"bad measure range {spec!r}")
        return lo, hi, self.start(lo), self.start(hi + 1)


def capacity(sig: tuple[int, int]) -> int:
    beats, beat_type = sig
    return beats * WHOLE // beat_type


def expand(entries: list[dict], measures: int, what: str) -> list:
    """Per-measure values from change entries [{measure, ...}] (time: (beats, beat_type); key: Key)."""
    out, current = [], None
    by_measure = {e["measure"]: e for e in entries}
    for m in range(1, measures + 1):
        if m in by_measure:
            e = by_measure[m]
            current = (e["beats"], e["beat_type"]) if what == "time" else key_of(e)
        elif current is None:
            current = (4, 4) if what == "time" else Key.parse("C")
        out.append(current)
    return out


def compress(values: list, what: str) -> list[dict]:
    out, last = [], None
    for m, v in enumerate(values, 1):
        if v != last:
            out.append({"measure": m, "beats": v[0], "beat_type": v[1]} if what == "time"
                       else {"measure": m, "tonic": v.tonic_name, "mode": v.mode})
            last = v
    return out


def key_of(entry: dict) -> Key:
    if "fifths" in entry and "tonic" not in entry:
        return Key.from_fifths(int(entry["fifths"]), entry.get("mode", "major"))
    return Key.parse(entry["tonic"], entry.get("mode", "major"))


def parse_key(text: str | None = None, *, tonic: str | None = None, mode: str | None = None,
              fifths: int | None = None) -> Key:
    if fifths is not None:
        return Key.from_fifths(int(fifths), mode or "major")
    if tonic:
        return Key.parse(tonic, mode or "major")
    if text:
        try:
            return Key.from_text(text)
        except ValueError as e:
            raise RollError(f"not a key: {text!r} (e.g. 'D dorian', 'Bb major', 'F# minor'): {e}") from None
    raise RollError("give a key, e.g. 'D dorian'")


def key_at(doc: dict, measure: int) -> Key:
    keys = expand(doc["key"], max(doc["measures"], 1), "key")
    return keys[min(max(measure, 1), len(keys)) - 1]


# --- the document ----------------------------------------------------------------------------------------------
def new_doc(title: str = "", time: str = "4/4", key: str = "C major", tempo: float = 120, measures: int = 4,
            preset: str = "piano", grid: dict | None = None) -> dict:
    beats, beat_type = parse_time(time)
    k = parse_key(key)
    if preset == "piano":
        groups = [{"id": "P1", "name": "Piano", "program": 0}]
        parts = [{"id": "rh", "name": "Right hand", "color": COLORS[0], "group": "P1", "staff": 1, "clef": "G2"},
                 {"id": "lh", "name": "Left hand", "color": COLORS[1], "group": "P1", "staff": 2, "clef": "F4"}]
    elif preset == "single":
        groups = [{"id": "P1", "name": "Melody", "program": 0}]
        parts = [{"id": "melody", "name": "Melody", "color": COLORS[0], "group": "P1", "staff": 1, "clef": "G2"}]
    else:
        raise RollError(f"preset is 'piano' or 'single', not {preset!r}")
    doc = {"format": FORMAT, "version": 1, "revision": 0, "next_id": 1, "title": title, "tpq": TPQ,
           "tempo": [{"tick": 0, "bpm": tempo}], "time": [{"measure": 1, "beats": beats, "beat_type": beat_type}],
           "measure_lengths": {}, "key": [{"measure": 1, "tonic": k.tonic_name, "mode": k.mode}],
           "measures": measures, "grid": dict(DEFAULT_GRID, **(grid or {})), "groups": groups, "parts": parts,
           "notes": [], "rests": [], "source": {"kind": "new"}, "results": []}
    return validate(doc)


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise RollError(f"not a valid roll: {msg}")


def _tick(v, what: str) -> int:
    _need(isinstance(v, int) and not isinstance(v, bool) and v >= 0 and v % QUANTUM == 0,
          f"{what} must be a whole number of 128ths (a multiple of {QUANTUM} ticks), not {v!r}")
    return v


def validate(doc: dict) -> dict:
    """Check a document (from disk, the editor or an op) and fill in defaults. Raises RollError."""
    _need(isinstance(doc, dict) and doc.get("format") == FORMAT, f"format must be {FORMAT!r}")
    _need(doc.get("tpq", TPQ) == TPQ, f"tpq must be {TPQ}")
    doc.setdefault("version", 1)
    for k in ("revision", "next_id"):
        doc[k] = int(doc.get(k) or (1 if k == "next_id" else 0))
    doc["title"] = str(doc.get("title") or "")
    measures = doc.get("measures")
    _need(isinstance(measures, int) and 1 <= measures <= MAX_MEASURES, f"measures must be 1..{MAX_MEASURES}")
    for what in ("time", "key"):
        entries = doc.get(what) or []
        _need(isinstance(entries, list), f"{what} is a list of changes")
        for e in entries:
            _need(isinstance(e, dict) and isinstance(e.get("measure"), int) and e["measure"] >= 1,
                  f"a {what} change needs a measure")
            try:
                parse_time(f"{e.get('beats')}/{e.get('beat_type')}") if what == "time" else key_of(e)
            except (ValueError, KeyError) as err:
                raise RollError(f"not a valid roll: {what} change at measure {e['measure']}: {err}") from None
        doc[what] = compress(expand(entries, measures, what), what)  # one entry per change, from measure 1
    lengths = doc.setdefault("measure_lengths", {})
    for k, v in list(lengths.items()):
        _need(str(k).isdigit() and 1 <= int(k) <= measures, f"measure_lengths key {k!r}")
        _tick(v, "a measure length")
        _need(v > 0, "a measure length must be positive")
    doc["measure_lengths"] = {str(int(k)): v for k, v in lengths.items()}
    tempo = doc.setdefault("tempo", [{"tick": 0, "bpm": 120}])
    for e in tempo:
        _tick(e.get("tick"), "a tempo's tick")
        _need(isinstance(e.get("bpm"), (int, float)) and 10 <= e["bpm"] <= 400, "tempo bpm must be 10..400")
    tempo.sort(key=lambda e: e["tick"])
    if not tempo or tempo[0]["tick"] != 0:
        tempo.insert(0, {"tick": 0, "bpm": tempo[0]["bpm"] if tempo else 120})
    grid = doc.setdefault("grid", dict(DEFAULT_GRID))
    for k in ("min", "default", "max"):
        grid[k] = str(grid.get(k, DEFAULT_GRID[k]))
        grid_ticks(grid[k])
    _need(VALUES[grid["min"]] <= VALUES[grid["default"]] <= VALUES[grid["max"]],
          "the grid needs min <= default <= max (e.g. min 16, default 8, max 4)")

    groups = doc.get("groups") or []
    _need(isinstance(groups, list) and groups, "there must be at least one group")
    group_ids = set()
    for g in groups:
        _need(isinstance(g.get("id"), str) and g["id"] and g["id"] not in group_ids, "group ids must be unique")
        group_ids.add(g["id"])
        g["name"] = str(g.get("name") or g["id"])
        g["program"] = int(g.get("program", 0))
        _need(0 <= g["program"] <= 127, "a program must be 0..127")
    parts = doc.get("parts") or []
    _need(isinstance(parts, list) and parts, "there must be at least one part")
    part_ids = set()
    for i, p in enumerate(parts):
        _need(isinstance(p.get("id"), str) and p["id"] and p["id"] not in part_ids, "part ids must be unique")
        part_ids.add(p["id"])
        _need(p.get("group") in group_ids, f"part {p['id']} is in no known group")
        p["name"] = str(p.get("name") or p["id"])
        p["color"] = str(p.get("color") or COLORS[i % len(COLORS)])
        _need(re.fullmatch(r"#[0-9a-fA-F]{6}", p["color"]) is not None, f"part {p['id']}'s colour must be #rrggbb")
        p["clef"] = str(p.get("clef") or "G2")
        _need(re.fullmatch(r"[GFC][1-5]([+-][12])?|percussion", p["clef"]) is not None, f"clef {p['clef']!r}")
    for g in groups:  # staves numbered 1.. within each group, in the order the parts are listed
        members = [p for p in parts if p["group"] == g["id"]]
        _need(bool(members), f"group {g['id']} has no parts")
        for n, p in enumerate(sorted(members, key=lambda p: (p.get("staff", 99), parts.index(p))), 1):
            p["staff"] = n

    ids: set[str] = set()
    notes = doc.setdefault("notes", [])
    rests = doc.setdefault("rests", [])
    _need(isinstance(notes, list) and isinstance(rests, list), "notes and rests are lists")
    _need(len(notes) + len(rests) <= MAX_NOTES, f"at most {MAX_NOTES} notes")
    for n in notes:
        _need(isinstance(n.get("id"), str) and n["id"] not in ids, "note ids must be unique")
        ids.add(n["id"])
        _need(n.get("part") in part_ids, f"note {n['id']} is in no known part")
        _need(isinstance(n.get("pitch"), int) and 0 <= n["pitch"] <= 127, f"note {n['id']}'s pitch must be 0..127")
        _tick(n.get("start"), f"note {n['id']}'s start")
        _need(_tick(n.get("dur"), f"note {n['id']}'s duration") > 0, f"note {n['id']} has no duration")
        n["vel"] = int(n.get("vel", 80))
        _need(1 <= n["vel"] <= 127, f"note {n['id']}'s velocity must be 1..127")
        if n.get("name"):
            try:
                if Pitch.parse(n["name"]).c != n["pitch"]:
                    del n["name"]
            except ValueError:
                del n["name"]
        else:
            n.pop("name", None)
    for r in rests:
        _need(isinstance(r.get("id"), str) and r["id"] not in ids, "rest ids must be unique")
        ids.add(r["id"])
        _need(r.get("part") in part_ids, f"rest {r['id']} is in no known part")
        _tick(r.get("start"), f"rest {r['id']}'s start")
        _need(_tick(r.get("dur"), f"rest {r['id']}'s duration") > 0, f"rest {r['id']} has no duration")
        r["row"] = int(r.get("row", 60))
    numbers = [int(i[1:]) for i in ids if re.fullmatch(r"n\d+", i)]
    doc["next_id"] = max(doc["next_id"], max(numbers, default=0) + 1)
    doc.setdefault("source", {"kind": "new"})
    doc.setdefault("results", [])
    return doc


def dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


# --- editing helpers -------------------------------------------------------------------------------------------
GRID_LABELS = {"1": "whole-note", "2": "half-note", "4": "quarter-note", "8": "eighth-note", "16": "16th",
               "32": "32nd", "64": "64th", "128": "128th"}


def new_id(doc: dict) -> str:
    n = doc["next_id"]
    doc["next_id"] = n + 1
    return f"n{n}"


def find_part(doc: dict, ref: str) -> dict:
    """A part by id or (case-insensitive) name."""
    ref = str(ref).strip()
    hit = next((p for p in doc["parts"] if p["id"] == ref), None)
    hits = [hit] if hit else [p for p in doc["parts"] if p["name"].lower() == ref.lower()]
    if len(hits) != 1:
        raise RollError(f"no part {ref!r}; parts: " + ", ".join(f"{p['id']} ({p['name']})" for p in doc["parts"]))
    return hits[0]


def find_group(doc: dict, ref: str) -> dict | None:
    ref = str(ref).strip()
    return next((g for g in doc["groups"] if g["id"] == ref or g["name"].lower() == ref.lower()), None)


def check_grid(doc: dict, ticks: int, what: str) -> None:
    """Edits land on the min grid, like the editor's; finer values need a finer grid first."""
    g = doc["grid"]["min"]
    if ticks % VALUES[g]:
        finer = finest_grid(ticks) if ticks % QUANTUM == 0 else "128"
        raise RollError(f"{what} is off the {GRID_LABELS[g]} grid; set_grid min={finer} first")


def label(doc: dict, note: dict, tm: TimeMap | None = None) -> str:
    tm = tm or TimeMap(doc)
    part = next((p["name"] for p in doc["parts"] if p["id"] == note["part"]), note["part"])
    what = pitch_name(note["pitch"], key_at(doc, tm.measure_at(note["start"]))) if "pitch" in note else "rest"
    return f"{what} at {tm.format(note['start'])} ({part})"


def _union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def normalize(doc: dict) -> list[str]:
    """Make the document consistent, the same way the editor does, and say what changed: notes end at the
    score's end, a part never holds the same pitch twice at once (the later note wins; of two that start
    together the longer stays), and rest markers give way to their part's notes."""
    tm = TimeMap(doc)
    end, warnings = tm.end, []
    order = {p["id"]: i for i, p in enumerate(doc["parts"])}
    kept = []
    for n in doc["notes"]:
        if n["start"] >= end:
            warnings.append(f"removed the {label(doc, n, tm)}: it is past the end of the score")
            continue
        if n["start"] + n["dur"] > end:
            n["dur"] = end - n["start"]
            warnings.append(f"shortened the {label(doc, n, tm)} to end with the score")
        kept.append(n)
    kept.sort(key=lambda n: (n["part"], n["pitch"], n["start"], -n["dur"]))
    notes, last = [], {}
    for n in kept:
        prev = last.get((n["part"], n["pitch"]))
        if prev and n["start"] < prev["start"] + prev["dur"]:
            if n["start"] == prev["start"]:
                warnings.append(f"dropped a second {label(doc, n, tm)}: the same note was already there")
                continue
            prev["dur"] = n["start"] - prev["start"]
            warnings.append(f"shortened the {label(doc, prev, tm)}: the same pitch starts again before it ends")
        notes.append(n)
        last[(n["part"], n["pitch"])] = n
    notes.sort(key=lambda n: (n["start"], order[n["part"]], n["pitch"]))
    doc["notes"] = notes

    sounding: dict[str, list[tuple[int, int]]] = {}
    for n in notes:
        sounding.setdefault(n["part"], []).append((n["start"], n["start"] + n["dur"]))
    rests = []
    for part in doc["parts"]:
        mine = sorted((r for r in doc["rests"] if r["part"] == part["id"] and r["start"] < end),
                      key=lambda r: r["start"])
        merged: list[dict] = []
        for r in mine:
            r["dur"] = min(r["dur"], end - r["start"])
            if merged and r["start"] <= merged[-1]["start"] + merged[-1]["dur"]:
                last_r = merged[-1]
                last_r["dur"] = max(last_r["start"] + last_r["dur"], r["start"] + r["dur"]) - last_r["start"]
            else:
                merged.append(r)
        busy = _union(sounding.get(part["id"], []))
        for r in merged:
            pieces, a, b = [], r["start"], r["start"] + r["dur"]
            for s, e in busy:
                if e <= a or s >= b:
                    continue
                if s > a:
                    pieces.append((a, s))
                a = max(a, e)
            if a < b:
                pieces.append((a, b))
            for i, (s, e) in enumerate(pieces):
                rests.append({**r, "id": r["id"] if i == 0 else new_id(doc), "start": s, "dur": e - s})
    rests.sort(key=lambda r: (r["start"], order[r["part"]]))
    doc["rests"] = rests
    return warnings


def voice_groups(notes: list[dict]) -> list[list[tuple[int, int, list[dict]]]]:
    """One part's notes as voices: notes with the same start and length are a chord; each voice is a list of
    (start, dur, notes) that don't overlap. Of chords starting together the higher one takes the lower
    voice, so a melody on top stays in voice 1."""
    chords: dict[tuple[int, int], list[dict]] = {}
    for n in notes:
        chords.setdefault((n["start"], n["dur"]), []).append(n)
    voices: list[list[tuple[int, int, list[dict]]]] = []
    ends: list[int] = []
    for (start, dur), ns in sorted(chords.items(), key=lambda kv: (kv[0][0], -max(n["pitch"] for n in kv[1]))):
        v = next((i for i, e in enumerate(ends) if e <= start), len(ends))
        if v == len(ends):
            voices.append([])
            ends.append(0)
        voices[v].append((start, dur, sorted(ns, key=lambda n: n["pitch"])))
        ends[v] = start + dur
    return voices


# --- selections ------------------------------------------------------------------------------------------------
@dataclass
class Selection:
    notes: list[dict]
    rests: list[dict]
    lo: int  # the span retrograde and scale_time work in
    hi: int
    parts: list[str]  # the parts the selection is about (all when it names none)
    named_parts: bool


def pitch_filter(spec) -> set[int] | tuple[int, int] | None:
    if spec is None:
        return None
    if isinstance(spec, list):
        return {parse_pitch(p)[0] for p in spec}
    text = str(spec).strip()
    if re.fullmatch(r"[^-]+\d-[A-Ga-g].+|\d+-\d+", text):
        a, b = text.split("-", 1) if not text[0].isdigit() else text.split("-")
        return parse_pitch(a)[0], parse_pitch(b)[0]
    return {parse_pitch(text)[0]}


def select(doc: dict, sel: dict | None, tm: TimeMap | None = None) -> Selection:
    sel = sel or {}
    if not isinstance(sel, dict):
        raise RollError("a selection is an object like {\"measures\": \"5-6\", \"parts\": [\"rh\"]}")
    unknown = set(sel) - SELECT_KEYS
    if unknown:
        raise RollError(f"unknown selection field(s) {', '.join(sorted(unknown))}; use {', '.join(sorted(SELECT_KEYS))}")
    tm = tm or TimeMap(doc)
    parts = sel.get("parts")
    parts = [parts] if isinstance(parts, str) else parts
    part_ids = [find_part(doc, p)["id"] for p in parts] if parts else [p["id"] for p in doc["parts"]]
    lo, hi, ranged = 0, None, False
    if sel.get("measures") is not None:
        _, _, a, b = tm.measure_range(sel["measures"])
        lo, hi, ranged = a, b, True
    if sel.get("start") is not None:
        lo, ranged = max(lo, tm.parse(sel["start"])), True
    if sel.get("end") is not None:
        e = tm.parse(sel["end"])
        hi, ranged = e if hi is None else min(hi, e), True
    at = tm.parse(sel["at"]) if sel.get("at") is not None else None
    pitches = pitch_filter(sel.get("pitches"))
    ids = set(sel["ids"]) if sel.get("ids") is not None else None

    def chosen(x: dict, is_note: bool) -> bool:
        if x["part"] not in part_ids or x["start"] < lo or (hi is not None and x["start"] >= hi):
            return False
        if (at is not None and x["start"] != at) or (ids is not None and x["id"] not in ids):
            return False
        if pitches is not None:
            if not is_note:
                return False
            p = x["pitch"]
            return p in pitches if isinstance(pitches, set) else min(pitches) <= p <= max(pitches)
        return True

    notes = [n for n in doc["notes"] if chosen(n, True)]
    rests = [r for r in doc["rests"] if chosen(r, False)]
    ends = [x["start"] + x["dur"] for x in notes + rests]
    starts = [x["start"] for x in notes + rests]
    if ranged:
        span = (lo, max([hi if hi is not None else tm.end, *ends]))
    elif at is not None or ids is not None or pitches is not None:
        span = (min(starts, default=0), max(ends, default=0))
    else:
        span = (0, max([tm.end, *ends]))
    return Selection(notes, rests, span[0], span[1], part_ids, bool(parts))


def describe_nothing(doc: dict, sel: dict | None, tm: TimeMap) -> str:
    """What is there instead, for an error about a selection that matched nothing."""
    s = select(doc, {k: v for k, v in (sel or {}).items() if k in ("parts", "measures", "start", "end", "at")}, tm)
    near = ", ".join(label(doc, n, tm) for n in s.notes[:8])
    return f"nothing matches {json.dumps(sel or {})}" + (f"; there: {near}" if near else "")


# --- note streams ----------------------------------------------------------------------------------------------
@dataclass
class Event:
    start: int
    dur: int
    pitches: list[tuple[int, str | None]]  # [] for a rest


def parse_stream(text: str, at: int, tm: TimeMap) -> list[Event]:
    """'D4:8 F4+A4:4 r:8 @1 A3:2' starting at tick `at` -> events."""
    first = tm.measure_at(at)
    cursor, events = at, []
    for i, tok in enumerate(str(text).replace(",", " ").split(), 1):
        try:
            if tok == "|":
                if tm.start(tm.measure_at(cursor)) != cursor:
                    raise RollError(f"bar check failed: the cursor is at {tm.format(cursor)}")
                continue
            if tok.startswith("@"):
                where = tok[1:]
                cursor = tm.parse(where if ":" in where else f"{first}:{where}")
                continue
            what, sep, dur = tok.rpartition(":")
            if not sep or not what:
                raise RollError("a note looks like C4:8, a chord C4+E4:4, a rest r:8")
            ticks = parse_duration(dur)
            pitches = [] if what.lower() == "r" else [parse_pitch(p) for p in what.split("+")]
            events.append(Event(cursor, ticks, pitches))
            cursor += ticks
        except ValueError as e:
            raise RollError(f"token {i} {tok!r}: {e}") from None
    return events


def _gap_rests(offset: int, length: int, beat: int) -> list[str]:
    return [f"r:{format_duration(v)}" for v in split_value(offset, length, beat)]


def part_streams(doc: dict, part_id: str, tm: TimeMap) -> dict[int, str]:
    """{measure: stream} for one part, as add would read it back: voice 1 with its rests, then each further
    voice after @beat. A note is listed in the measure where it starts, with its whole length."""
    voices = voice_groups([n for n in doc["notes"] if n["part"] == part_id])
    out: dict[int, list[str]] = {m: [] for m in range(1, tm.measures + 1)}
    for v, groups in enumerate(voices):
        by_measure: dict[int, list] = {}
        for g in groups:
            by_measure.setdefault(tm.measure_at(g[0]), []).append(g)
        held_until = 0  # end of the voice's last chord, which may run into the next measures
        for m in range(1, tm.measures + 1):
            ms, me, beat = tm.start(m), tm.start(m + 1), tm.beat(m)
            gs = by_measure.get(m, [])
            if v > 0 and not gs:
                continue
            cursor, tokens = max(ms, min(held_until, me)), []
            key = key_at(doc, m)
            for start, dur, ns in gs:
                if start > cursor:
                    if v == 0 and tokens or v == 0 and cursor == ms:
                        tokens += _gap_rests(cursor - ms, start - cursor, beat)
                    else:
                        tokens.append("@" + tm.format(start).split(":", 1)[1])
                elif not tokens and (v > 0 or start > ms):
                    tokens.append("@" + tm.format(start).split(":", 1)[1])
                tokens.append("+".join(spelled(n, key).name for n in ns) + ":" + format_duration(dur))
                cursor = start + dur
                held_until = cursor
            if v == 0 and cursor < me:
                if not tokens and cursor > ms:
                    tokens.append("@" + tm.format(cursor).split(":", 1)[1])
                tokens += _gap_rests(cursor - ms, me - cursor, beat)
            out[m].append(" ".join(tokens))
    if not voices:
        for m in out:
            out[m].append(" ".join(_gap_rests(0, tm.length(m), tm.beat(m))))
    return {m: " ".join(t for t in lines if t) for m, lines in out.items()}


def streams(doc: dict, measures: range, parts: list[str] | None = None) -> list[dict]:
    tm = TimeMap(doc)
    ids = [find_part(doc, p)["id"] for p in parts] if parts else [p["id"] for p in doc["parts"]]
    per_part = {pid: part_streams(doc, pid, tm) for pid in ids}
    out = []
    for m in measures:
        if 1 <= m <= tm.measures:
            beats, beat_type = tm.sig(m)
            out.append({"measure": m, "time": f"{beats}/{beat_type}", "start": tm.format(tm.start(m)),
                        "parts": {pid: per_part[pid][m] for pid in ids}})
    return out


def summary(doc: dict) -> dict:
    tm = TimeMap(doc)
    parts = []
    for p in doc["parts"]:
        ps = [n["pitch"] for n in doc["notes"] if n["part"] == p["id"]]
        group = next(g for g in doc["groups"] if g["id"] == p["group"])
        parts.append({"id": p["id"], "name": p["name"], "color": p["color"], "instrument": group["name"],
                      "staff": p["staff"], "clef": p["clef"], "notes": len(ps),
                      "range": f"{pitch_name(min(ps))}-{pitch_name(max(ps))}" if ps else None})
    return {
        "title": doc["title"], "revision": doc["revision"], "measures": doc["measures"],
        "time": [f"{e['beats']}/{e['beat_type']} from m{e['measure']}" for e in doc["time"]],
        "key": [f"{e['tonic']} {e['mode']} from m{e['measure']}" for e in doc["key"]],
        "tempo": [f"{e['bpm']:g} bpm at {tm.format(e['tick'])}" for e in doc["tempo"]],
        "grid": doc["grid"], "parts": parts, "notes": len(doc["notes"]), "rest_markers": len(doc["rests"]),
        "irregular_measures": {k: format_duration(v) for k, v in doc["measure_lengths"].items()},
        "source": doc.get("source"), "results": doc.get("results", []),
    }


def copy_doc(doc: dict) -> dict:
    return copy.deepcopy(doc)
