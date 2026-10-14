"""Edits on a piano roll, as operations applied in order, all or nothing.

    apply_operations(doc, [{"op": "add", "part": "rh", "at": "1:1", "notes": "D4:8 F4:8 A4:4"},
                           {"op": "mirror", "select": {"measures": "1"}, "kind": "diatonic",
                            "center": "A4", "tonic": "D", "mode": "dorian"}])

returns the edited copy and a report; the document passed in is never changed. A failing operation
raises RollError("operation 2 (mirror): ...") and nothing is applied. Positions, durations, note
streams and selections are the text forms described in roll.py. Edits land on the roll's min grid.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable

import roll
from pitch import Mirror, Pitch, mirror_diatonic
from roll import RollError, Selection, TimeMap, compress, expand

MAX_OPS = 500


@dataclass(frozen=True)
class F:
    schema: dict
    required: bool = False


POS = {"type": "string", "description": "measure:beat, e.g. 3:2, 3:2.5 or 3:2+16"}
DUR = {"type": "string", "description": "1 2 4 8 16 32 64 128, '.' dotted, '~' tied: 4. or 2~8"}
SEL = {"type": "object", "description": "which notes: {parts: [ids or names], measures: '5-6', start: pos, "
                                        "end: pos (excluded), at: pos, pitches: 'C4-G5' or ['E4'], ids}; "
                                        "omitted or {} = the whole score"}
PART = {"type": "string", "description": "part id or name"}
PITCH = {"type": "string", "description": "pitch name, e.g. F#4 or Bb3"}
STR = {"type": "string"}
INT = {"type": "integer"}
KEY = {"type": "string", "description": "e.g. 'D dorian', 'Bb major'; default: the score's key there"}
CENTER = {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2}],
          "description": "the mirror's axis: a note ('D4'), or two notes it swaps (['D4', 'A4'])"}
FACTOR = {"type": "string", "description": "e.g. '2' (twice as long), '1/2', '3/2'; 1/8 .. 8"}


# --- shared pieces ---------------------------------------------------------------------------------------------
def _sel(doc: dict, op: dict) -> tuple[Selection, TimeMap]:
    tm = TimeMap(doc)
    s = roll.select(doc, op.get("select"), tm)
    if not s.notes and not s.rests:
        raise RollError(roll.describe_nothing(doc, op.get("select"), tm))
    return s, tm


def _on_grid(doc: dict, tm: TimeMap, tick: int, what: str) -> None:
    roll.check_grid(doc, tick - tm.start(tm.measure_at(tick)), what)


def set_measures(doc: dict, n: int) -> None:
    if not 1 <= n <= roll.MAX_MEASURES:
        raise RollError(f"a score has 1..{roll.MAX_MEASURES} measures, not {n}")
    old = doc["measures"]
    sigs, keys = expand(doc["time"], max(n, old), "time"), expand(doc["key"], max(n, old), "key")
    doc["measures"] = n
    doc["time"], doc["key"] = compress(sigs[:n], "time"), compress(keys[:n], "key")
    doc["measure_lengths"] = {k: v for k, v in doc["measure_lengths"].items() if int(k) <= n}
    end = TimeMap(doc).end
    doc["tempo"] = [e for e in doc["tempo"] if e["tick"] < end or e["tick"] == 0]


def _grow(doc: dict, tick: int, info: dict) -> None:
    need = TimeMap(doc).measures_to_cover(tick)
    if need > doc["measures"]:
        set_measures(doc, need)
        info["score_grew_to_measures"] = need


def _span(tm: TimeMap, lo: int, hi: int) -> str:
    return f"{tm.format(lo)} to {tm.format(hi)}"


def _key(doc: dict, op: dict, s: Selection, tm: TimeMap):
    if op.get("key") or op.get("tonic"):
        return roll.parse_key(op.get("key"), tonic=op.get("tonic"), mode=op.get("mode"))
    k = roll.key_at(doc, tm.measure_at(s.lo))
    return k if not op.get("mode") else roll.parse_key(tonic=k.tonic_name, mode=op["mode"])


def _set_pitch(note: dict, p: Pitch) -> None:
    if not 0 <= p.c <= 127:
        raise RollError(f"{p.name} is outside MIDI's range")
    note["pitch"], note["name"] = p.c, p.name


def _signed_duration(text: str) -> int:
    s = str(text).strip()
    sign = -1 if s.startswith("-") else 1
    return sign * roll.parse_duration(s.lstrip("+-"))


def _items(s: Selection) -> list[dict]:
    return s.notes + s.rests


def _place(doc: dict, tm: TimeMap, items: list[dict], op: dict, lo: int, info: dict, copy_them: bool) -> None:
    """Move (or copy) items by 'by', to 'to', or by whole 'measures', and/or into 'part'."""
    given = [k for k in ("by", "to", "measures") if op.get(k) is not None]
    if len(given) > 1:
        raise RollError("give one of by, to or measures")
    if not given and not op.get("part"):
        raise RollError("give by, to, measures or part")
    target = roll.find_part(doc, op["part"])["id"] if op.get("part") else None
    starts = {}
    for x in items:
        if "by" in given:
            start = x["start"] + _signed_duration(op["by"])
        elif "to" in given:
            start = x["start"] + tm.parse(op["to"]) - lo
        elif "measures" in given:
            m = tm.measure_at(x["start"])
            dest = m + int(op["measures"])
            offset = x["start"] - tm.start(m)
            if dest < 1 or (dest <= tm.measures and offset >= tm.length(dest)):
                raise RollError(f"the {roll.label(doc, x, tm)} has no place in measure {dest}")
            start = tm.start(dest) + offset
        else:
            start = x["start"]
        if start < 0:
            raise RollError(f"that would move the {roll.label(doc, x, tm)} before the start of the score")
        _on_grid(doc, tm, start, f"the new place of the {roll.label(doc, x, tm)}")
        starts[id(x)] = start
    moved = []
    for x in items:
        y = {**x, "id": roll.new_id(doc)} if copy_them else x
        y["start"] = starts[id(x)]
        if target:
            y["part"] = target
        moved.append(y)
    if copy_them:
        for y in moved:
            (doc["notes"] if "pitch" in y else doc["rests"]).append(y)
    _grow(doc, max(y["start"] + y["dur"] for y in moved), info)
    info["copied" if copy_them else "moved"] = len(moved)


def _mirror(doc: dict, op: dict, s: Selection, tm: TimeMap, info: dict) -> None:
    kind = op.get("kind", "chromatic")
    center = op.get("center")
    if center is None:
        raise RollError("a mirror needs a center, e.g. 'D4', or two notes to swap, ['D4', 'A4']")
    axes = [Pitch.parse(c) for c in (center if isinstance(center, list) else [center])]
    if len(axes) not in (1, 2):
        raise RollError("center is one note or two")
    if kind == "chromatic":
        mirror = Mirror.about(axes[0]) if len(axes) == 1 else Mirror.between(*axes)
        for n in s.notes:
            _set_pitch(n, mirror.apply(roll.spelled(n, roll.key_at(doc, tm.measure_at(n["start"])))))
    elif kind == "diatonic":
        key = _key(doc, op, s, tm)
        for a in axes:
            if not key.in_scale(a):
                scale = " ".join(key.pitch_at(28 + key.tonic_step + i).name[:-1] for i in range(7))
                raise RollError(f"the center {a.name} is not a note of {key} ({scale})")
        axis_d = sum(a.d for a in axes) * (2 if len(axes) == 1 else 1)
        for n in s.notes:
            _set_pitch(n, mirror_diatonic(roll.spelled(n, key), key, axis_d))
        info["key"] = str(key)
    else:
        raise RollError(f"kind is 'chromatic' or 'diatonic', not {kind!r}")
    info["mirrored"] = len(s.notes)
    info["center"] = center


def _retrograde(s: Selection, tm: TimeMap, info: dict) -> None:
    for x in _items(s):
        x["start"] = s.lo + s.hi - (x["start"] + x["dur"])
    info["reversed"] = len(_items(s))
    info["span"] = _span(tm, s.lo, s.hi)


def _factor(value: Any) -> Fraction:
    try:
        f = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        raise RollError(f"factor is a number such as '2', '1/2' or '3/2', not {value!r}") from None
    if not Fraction(1, 8) <= f <= 8:
        raise RollError(f"factor must be between 1/8 and 8, not {value}")
    return f


# --- the operations --------------------------------------------------------------------------------------------
def op_add(doc: dict, op: dict) -> dict:
    part = roll.find_part(doc, op["part"])
    tm = TimeMap(doc)
    at = tm.parse(op.get("at", "1:1"))
    events = roll.parse_stream(op["notes"], at, tm)
    if not events:
        raise RollError("notes is empty; write e.g. 'D4:8 F4+A4:4 r:8'")
    vel = int(op.get("vel", 80))
    if not 1 <= vel <= 127:
        raise RollError("vel is 1..127")
    row = 48 if part["clef"].startswith("F") else 67
    info: dict = {"added_notes": 0, "added_rests": 0}
    for e in events:
        _on_grid(doc, tm, e.start, f"the event at {tm.format(e.start)}")
        roll.check_grid(doc, e.dur, f"the {roll.format_duration(e.dur)} at {tm.format(e.start)}")
        if not e.pitches:
            doc["rests"].append({"id": roll.new_id(doc), "part": part["id"], "start": e.start, "dur": e.dur,
                                 "row": row})
            info["added_rests"] += 1
        for midi, name in e.pitches:
            note = {"id": roll.new_id(doc), "part": part["id"], "pitch": midi, "start": e.start, "dur": e.dur,
                    "vel": vel}
            if name:
                note["name"] = name
            doc["notes"].append(note)
            row = midi
            info["added_notes"] += 1
    end = max(e.start + e.dur for e in events)
    _grow(doc, end, info)
    info["span"] = _span(TimeMap(doc), min(e.start for e in events), end)
    return info


def op_add_rest(doc: dict, op: dict) -> dict:
    part = roll.find_part(doc, op["part"])
    tm = TimeMap(doc)
    at, dur = tm.parse(op["at"]), roll.parse_duration(op["dur"])
    _on_grid(doc, tm, at, f"the rest at {op['at']}")
    roll.check_grid(doc, dur, f"a rest of {op['dur']}")
    row = roll.parse_pitch(op["row"])[0] if op.get("row") else (48 if part["clef"].startswith("F") else 67)
    doc["rests"].append({"id": roll.new_id(doc), "part": part["id"], "start": at, "dur": dur, "row": row})
    info: dict = {"added_rests": 1}
    _grow(doc, at + dur, info)
    return info


def op_delete(doc: dict, op: dict) -> dict:
    s, _ = _sel(doc, op)
    gone = {x["id"] for x in _items(s)}
    doc["notes"] = [n for n in doc["notes"] if n["id"] not in gone]
    doc["rests"] = [r for r in doc["rests"] if r["id"] not in gone]
    return {"deleted_notes": len(s.notes), "deleted_rests": len(s.rests)}


def op_change(doc: dict, op: dict) -> dict:
    if not any(op.get(k) is not None for k in ("pitch", "dur", "vel")):
        raise RollError("change needs pitch, dur or vel")
    s, tm = _sel(doc, op)
    info: dict = {"changed": len(_items(s))}
    if op.get("pitch") is not None:
        if len({n["pitch"] for n in s.notes}) > 1:
            raise RollError(f"pitch would give {len(s.notes)} different notes one pitch; select one note "
                            "(e.g. with at and pitches), or use transpose")
        midi, name = roll.parse_pitch(op["pitch"])
        for n in s.notes:
            n["pitch"] = midi
            n.pop("name", None)
            if name:
                n["name"] = name
    if op.get("dur") is not None:
        dur = roll.parse_duration(op["dur"])
        roll.check_grid(doc, dur, f"a duration of {op['dur']}")
        for x in _items(s):
            x["dur"] = dur
        _grow(doc, max(x["start"] + dur for x in _items(s)), info)
    if op.get("vel") is not None:
        vel = int(op["vel"])
        if not 1 <= vel <= 127:
            raise RollError("vel is 1..127")
        for n in s.notes:
            n["vel"] = vel
    return info


def op_move(doc: dict, op: dict) -> dict:
    s, tm = _sel(doc, op)
    info: dict = {}
    _place(doc, tm, _items(s), op, s.lo, info, copy_them=False)
    return info


def op_copy(doc: dict, op: dict) -> dict:
    s, tm = _sel(doc, op)
    info: dict = {}
    _place(doc, tm, _items(s), op, s.lo, info, copy_them=True)
    return info


def op_transpose(doc: dict, op: dict) -> dict:
    s, tm = _sel(doc, op)
    if (op.get("semitones") is None) == (op.get("steps") is None):
        raise RollError("give semitones (chromatic) or steps (diatonic, in a key)")
    if op.get("semitones") is not None:
        k = int(op["semitones"])
        for n in s.notes:
            if not 0 <= n["pitch"] + k <= 127:
                raise RollError(f"the {roll.label(doc, n, tm)} would leave MIDI's range")
            n["pitch"] += k
            n.pop("name", None)
        return {"transposed": len(s.notes), "semitones": k}
    key, steps = _key(doc, op, s, tm), int(op["steps"])
    for n in s.notes:
        p = roll.spelled(n, key)
        _set_pitch(n, key.pitch_at(p.d + steps, key.degree_of(p)[1]))
    return {"transposed": len(s.notes), "steps": steps, "key": str(key)}


def op_mirror(doc: dict, op: dict) -> dict:
    s, tm = _sel(doc, op)
    info: dict = {}
    _mirror(doc, op, s, tm, info)
    return info


def op_retrograde(doc: dict, op: dict) -> dict:
    s, tm = _sel(doc, op)
    info: dict = {}
    _retrograde(s, tm, info)
    return info


def op_retrograde_inversion(doc: dict, op: dict) -> dict:
    s, tm = _sel(doc, op)
    info: dict = {}
    _mirror(doc, op, s, tm, info)
    _retrograde(s, tm, info)
    return info


def op_scale_time(doc: dict, op: dict) -> dict:
    f = _factor(op["factor"])
    s, tm = _sel(doc, op)
    lo, hi = s.lo, s.hi
    chosen = {id(x) for x in _items(s)}
    new = {}
    for x in _items(s):
        start, dur = lo + (x["start"] - lo) * f, x["dur"] * f
        if start.denominator != 1 or dur.denominator != 1 or dur % roll.QUANTUM or start % roll.QUANTUM:
            raise RollError(f"x{f} would put the {roll.label(doc, x, tm)} between 128ths")
        new[id(x)] = (int(start), int(dur))
    delta = (hi - lo) * (f - 1)
    if delta.denominator != 1:
        raise RollError(f"x{f} doesn't divide the span {_span(tm, lo, hi)} into whole ticks")
    for x in _items(s):
        x["start"], x["dur"] = new[id(x)]
    for x in _items(s):
        _on_grid(doc, tm, x["start"], f"after x{f}, the {roll.label(doc, x, tm)}")
        roll.check_grid(doc, x["dur"], f"after x{f}, the {roll.label(doc, x, tm)}'s length")
    pushed = 0
    if op.get("ripple", True) and delta:
        for x in doc["notes"] + doc["rests"]:
            if x["part"] in s.parts and id(x) not in chosen and x["start"] >= hi:
                x["start"] += int(delta)
                pushed += 1
    info: dict = {"factor": str(f), "scaled": len(chosen), "pushed_later_notes": pushed,
                  "span": _span(tm, lo, hi), "becomes": _span(tm, lo, int(lo + (hi - lo) * f))}
    end = max([x["start"] + x["dur"] for x in doc["notes"] + doc["rests"]], default=0)
    if not op.get("select"):  # the whole score: its length scales too, trailing rests and all
        end = max(end, int(lo + (hi - lo) * f))
        set_measures(doc, max(1, TimeMap(doc).measure_at(end - 1)))
        info["measures"] = doc["measures"]
    _grow(doc, end, info)
    return info


def op_insert_measures(doc: dict, op: dict) -> dict:
    tm = TimeMap(doc)
    at, count = int(op["at"]), int(op.get("count", 1))
    if not 1 <= at <= tm.measures + 1 or count < 1:
        raise RollError(f"at is a measure 1..{tm.measures + 1} and count at least 1")
    if tm.measures + count > roll.MAX_MEASURES:
        raise RollError(f"a score has at most {roll.MAX_MEASURES} measures")
    sigs, keys = expand(doc["time"], tm.measures, "time"), expand(doc["key"], tm.measures, "key")
    i = min(at, tm.measures) - 1
    t0, length = tm.start(at), count * roll.capacity(sigs[i])
    for x in doc["notes"] + doc["rests"]:
        if x["start"] >= t0:
            x["start"] += length
    for e in doc["tempo"]:
        if e["tick"] >= t0 and e["tick"]:
            e["tick"] += length
    sigs[at - 1:at - 1] = [sigs[i]] * count
    keys[at - 1:at - 1] = [keys[i]] * count
    doc["measures"] += count
    doc["time"], doc["key"] = compress(sigs, "time"), compress(keys, "key")
    doc["measure_lengths"] = {str(int(k) + count if int(k) >= at else int(k)): v
                              for k, v in doc["measure_lengths"].items()}
    return {"inserted": count, "at_measure": at}


def op_delete_measures(doc: dict, op: dict) -> dict:
    tm = TimeMap(doc)
    lo, hi, t0, t1 = tm.measure_range(op["measures"])
    if hi > tm.measures:
        raise RollError(f"the score has {tm.measures} measures")
    count = hi - lo + 1
    if count >= tm.measures:
        raise RollError("can't delete every measure")
    length, info = t1 - t0, {"deleted_measures": count, "trimmed": 0}
    for kind in ("notes", "rests"):
        kept = []
        for x in doc[kind]:
            if t0 <= x["start"] < t1:
                continue
            if x["start"] < t0 < x["start"] + x["dur"]:
                x["dur"] = t0 - x["start"]
                info["trimmed"] += 1
            elif x["start"] >= t1:
                x["start"] -= length
            kept.append(x)
        doc[kind] = kept
    tempo = []
    for e in doc["tempo"]:
        if t0 <= e["tick"] < t1 and e["tick"]:
            continue
        tempo.append({**e, "tick": e["tick"] - length if e["tick"] >= t1 else e["tick"]})
    doc["tempo"] = tempo
    sigs, keys = expand(doc["time"], tm.measures, "time"), expand(doc["key"], tm.measures, "key")
    del sigs[lo - 1:hi], keys[lo - 1:hi]
    doc["measures"] -= count
    doc["time"], doc["key"] = compress(sigs, "time"), compress(keys, "key")
    doc["measure_lengths"] = {str(int(k) - count if int(k) > hi else int(k)): v
                              for k, v in doc["measure_lengths"].items() if not lo <= int(k) <= hi}
    return info


def op_set_length(doc: dict, op: dict) -> dict:
    set_measures(doc, int(op["measures"]))
    return {"measures": doc["measures"]}


def op_set_time(doc: dict, op: dict) -> dict:
    """Notes stay in their measures, at the same distance from the barline; a note that would fall past
    the end of its (shorter) measure stops the change."""
    beats, beat_type = roll.parse_time(op["time"])
    m = int(op.get("measure", 1))
    if not 1 <= m <= doc["measures"]:
        raise RollError(f"measure is 1..{doc['measures']}")
    old = TimeMap(doc)
    where = []
    for x in doc["notes"] + doc["rests"]:
        mm = old.measure_at(x["start"])
        where.append((x, mm, x["start"] - old.start(mm)))
    entries = [e for e in doc["time"] if e["measure"] != m] + [{"measure": m, "beats": beats, "beat_type": beat_type}]
    doc["time"] = compress(expand(entries, doc["measures"], "time"), "time")
    new = TimeMap(doc)
    for x, mm, offset in where:
        if offset >= new.length(mm):
            raise RollError(f"the {roll.label(doc, x, old)} would fall outside measure {mm} in {beats}/{beat_type}; "
                            "move or delete it first")
        x["start"] = new.start(mm) + offset
    for e in doc["tempo"]:
        mm = old.measure_at(e["tick"])
        e["tick"] = new.start(mm) + min(e["tick"] - old.start(mm), new.length(mm) - roll.QUANTUM)
    return {"time": f"{beats}/{beat_type}", "from_measure": m}


def op_set_key(doc: dict, op: dict) -> dict:
    key = roll.parse_key(op.get("key"), tonic=op.get("tonic"), mode=op.get("mode"), fifths=op.get("fifths"))
    m = int(op.get("measure", 1))
    if not 1 <= m <= doc["measures"]:
        raise RollError(f"measure is 1..{doc['measures']}")
    entries = [e for e in doc["key"] if e["measure"] != m] + [{"measure": m, "tonic": key.tonic_name, "mode": key.mode}]
    doc["key"] = compress(expand(entries, doc["measures"], "key"), "key")
    return {"key": str(key), "from_measure": m, "fifths": key.fifths}


def op_set_tempo(doc: dict, op: dict) -> dict:
    bpm = float(op["bpm"])
    if not 10 <= bpm <= 400:
        raise RollError("bpm is 10..400")
    tick = TimeMap(doc).parse(op.get("at", "1:1"))
    doc["tempo"] = sorted([e for e in doc["tempo"] if e["tick"] != tick] + [{"tick": tick, "bpm": bpm}],
                          key=lambda e: e["tick"])
    return {"bpm": bpm}


def op_set_grid(doc: dict, op: dict) -> dict:
    grid = dict(doc["grid"])
    for k in ("min", "default", "max"):
        if op.get(k) is not None:
            roll.grid_ticks(str(op[k]))
            grid[k] = str(op[k])
    if not roll.VALUES[grid["min"]] <= roll.VALUES[grid["default"]] <= roll.VALUES[grid["max"]]:
        raise RollError("the grid needs min <= default <= max, e.g. min 16, default 8, max 4")
    doc["grid"] = grid
    tm = TimeMap(doc)
    off = [n for n in doc["notes"] if (n["start"] - tm.start(tm.measure_at(n["start"]))) % roll.VALUES[grid["min"]]
           or n["dur"] % roll.VALUES[grid["min"]]]
    info: dict = {"grid": grid}
    if off:
        info["notes_off_the_min_grid"] = len(off)
    return info


def op_set_title(doc: dict, op: dict) -> dict:
    doc["title"] = str(op["title"])[:200]
    return {"title": doc["title"]}


def _slug(doc: dict, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "part"
    taken = {p["id"] for p in doc["parts"]}
    return next(c for c in (base if i == 1 else f"{base}-{i}" for i in range(1, 1000)) if c not in taken)


def op_add_part(doc: dict, op: dict) -> dict:
    name = str(op["name"]).strip()
    if not name:
        raise RollError("a part needs a name")
    group = roll.find_group(doc, op["instrument"]) if op.get("instrument") else None
    if group is None:
        gid = next(f"P{i}" for i in range(1, 1000) if f"P{i}" not in {g["id"] for g in doc["groups"]})
        group = {"id": gid, "name": str(op.get("instrument") or name), "program": int(op.get("program", 0))}
        doc["groups"].append(group)
    pid = str(op.get("id") or _slug(doc, name))
    if any(p["id"] == pid for p in doc["parts"]):
        raise RollError(f"there is already a part {pid}")
    used = {p["color"].lower() for p in doc["parts"]}
    color = op.get("color") or next((c for c in roll.COLORS if c not in used), roll.COLORS[len(doc["parts"]) % 8])
    doc["parts"].append({"id": pid, "name": name, "color": color, "group": group["id"], "staff": 99,
                         "clef": str(op.get("clef") or "G2")})
    return {"part": pid, "instrument": group["name"]}


def op_update_part(doc: dict, op: dict) -> dict:
    part = roll.find_part(doc, op["part"])
    for k in ("name", "color", "clef"):
        if op.get(k) is not None:
            part[k] = str(op[k])
    if op.get("program") is not None:
        next(g for g in doc["groups"] if g["id"] == part["group"])["program"] = int(op["program"])
    return {"part": part["id"]}


def op_remove_part(doc: dict, op: dict) -> dict:
    part = roll.find_part(doc, op["part"])
    if len(doc["parts"]) == 1:
        raise RollError("can't remove the only part")
    gone = sum(1 for n in doc["notes"] if n["part"] == part["id"])
    doc["notes"] = [n for n in doc["notes"] if n["part"] != part["id"]]
    doc["rests"] = [r for r in doc["rests"] if r["part"] != part["id"]]
    doc["parts"].remove(part)
    doc["groups"] = [g for g in doc["groups"] if any(p["group"] == g["id"] for p in doc["parts"])]
    return {"removed": part["id"], "notes_removed": gone}


def op_quantize(doc: dict, op: dict) -> dict:
    q = roll.grid_ticks(str(op["grid"]))
    s, tm = _sel(doc, op)
    for x in _items(s):
        m = tm.measure_at(x["start"])
        offset = x["start"] - tm.start(m)
        end = x["start"] + x["dur"]
        x["start"] = tm.start(m) + round(offset / q) * q
        x["dur"] = max(q, round((end - x["start"]) / q) * q)
    return {"quantized": len(_items(s)), "grid": str(op["grid"])}


OPS: dict[str, tuple[Callable[[dict, dict], dict], dict[str, F], str]] = {
    "add": (op_add, {"part": F(PART, True), "at": F(POS), "notes": F({"type": "string", "description":
            "a note stream: D4:8 F4+A4:4 r:8; @2 or @3:2 moves the cursor (a second voice), | checks a barline"}, True),
            "vel": F(INT)}, "add notes, chords and rest markers from a position (default 1:1)"),
    "add_rest": (op_add_rest, {"part": F(PART, True), "at": F(POS, True), "dur": F(DUR, True), "row": F(PITCH)},
                 "add a rest marker"),
    "delete": (op_delete, {"select": F(SEL)}, "delete the selected notes and rest markers"),
    "change": (op_change, {"select": F(SEL), "pitch": F(PITCH), "dur": F(DUR), "vel": F(INT)},
               "set the pitch, length or velocity of the selected notes"),
    "move": (op_move, {"select": F(SEL), "by": F({"type": "string", "description": "signed duration, e.g. +8 or -4."}),
                       "to": F(POS), "measures": F(INT), "part": F(PART)},
             "move the selection in time (by a duration, to a position, or by whole measures) and/or to a part"),
    "copy": (op_copy, {"select": F(SEL), "by": F(STR), "to": F(POS), "measures": F(INT), "part": F(PART)},
             "copy the selection elsewhere (to a position, by a duration or by measures) and/or to a part"),
    "transpose": (op_transpose, {"select": F(SEL), "semitones": F(INT), "steps": F(INT), "key": F(KEY),
                                 "tonic": F(STR), "mode": F(STR)},
                  "transpose by semitones, or by scale steps in a key (diatonic)"),
    "mirror": (op_mirror, {"select": F(SEL), "kind": F({"type": "string", "enum": ["chromatic", "diatonic"]}),
                           "center": F(CENTER, True), "key": F(KEY), "tonic": F(STR), "mode": F(STR)},
               "invert pitches about a center: chromatic (semitones) or diatonic (scale steps in tonic + mode)"),
    "retrograde": (op_retrograde, {"select": F(SEL)}, "reverse the selection in time within its span"),
    "retrograde_inversion": (op_retrograde_inversion, {"select": F(SEL), "kind": F({"type": "string", "enum": [
        "chromatic", "diatonic"]}), "center": F(CENTER, True), "key": F(KEY), "tonic": F(STR), "mode": F(STR)},
        "mirror, then reverse"),
    "scale_time": (op_scale_time, {"select": F(SEL), "factor": F(FACTOR, True), "ripple": F({"type": "boolean"})},
                   "multiply lengths and distances by a factor from the span's start; later music in the same "
                   "parts moves (ripple, default true) and the score grows if needed"),
    "insert_measures": (op_insert_measures, {"at": F(INT, True), "count": F(INT)},
                        "insert empty measures before measure 'at' (measures+1 appends)"),
    "delete_measures": (op_delete_measures, {"measures": F(STR, True)}, "delete measures, e.g. '5-6'; later music moves up"),
    "set_length": (op_set_length, {"measures": F(INT, True)}, "set the number of measures (cuts notes past the end)"),
    "set_time": (op_set_time, {"time": F(STR, True), "measure": F(INT)}, "time signature from a measure (default 1)"),
    "set_key": (op_set_key, {"key": F(KEY), "tonic": F(STR), "mode": F(STR), "fifths": F(INT), "measure": F(INT)},
                "key from a measure (default 1)"),
    "set_tempo": (op_set_tempo, {"bpm": F({"type": "number"}, True), "at": F(POS)}, "tempo in quarter notes per minute"),
    "set_grid": (op_set_grid, {"min": F(STR), "default": F(STR), "max": F(STR)},
                 "the editor's grid: min (resize step and finest position), default (a click's length), max "
                 "(longest drag); 1 2 4 8 16 32 64 128"),
    "set_title": (op_set_title, {"title": F(STR, True)}, "the score's title"),
    "add_part": (op_add_part, {"name": F(STR, True), "id": F(STR), "instrument": F({"type": "string", "description":
                 "an existing instrument (group) to add a staff to, or a new instrument's name"}), "program": F(INT),
                 "color": F(STR), "clef": F({"type": "string", "description": "G2 treble, F4 bass, C3 alto, G2-1 tenor 8vb"})},
                "add a part (one staff, one colour)"),
    "update_part": (op_update_part, {"part": F(PART, True), "name": F(STR), "color": F(STR), "clef": F(STR),
                                     "program": F(INT)}, "rename or recolour a part, or change its clef or sound"),
    "remove_part": (op_remove_part, {"part": F(PART, True)}, "remove a part and its notes"),
    "quantize": (op_quantize, {"select": F(SEL), "grid": F(STR, True)}, "snap starts and lengths to a grid value"),
}


def ops_schema() -> dict:
    """JSON schema for one item of an operations list."""
    variants = []
    for name, (_, fields, doc) in OPS.items():
        props = {"op": {"type": "string", "enum": [name]}, **{k: f.schema for k, f in fields.items()}}
        variants.append({"type": "object", "description": doc, "properties": props,
                         "required": ["op", *(k for k, f in fields.items() if f.required)],
                         "additionalProperties": False})
    return {"anyOf": variants}


def apply_one(doc: dict, op: dict) -> dict:
    if not isinstance(op, dict) or "op" not in op:
        raise RollError('each operation is an object with an "op" field')
    name = op["op"]
    if name not in OPS:
        raise RollError(f"unknown op {name!r}; ops: {', '.join(OPS)}")
    handler, fields, _ = OPS[name]
    unknown = set(op) - set(fields) - {"op"}
    if unknown:
        raise RollError(f"unknown field(s) {', '.join(sorted(unknown))}; {name} takes {', '.join(fields) or 'nothing'}")
    missing = [k for k, f in fields.items() if f.required and op.get(k) is None]
    if missing:
        raise RollError(f"{name} needs {', '.join(missing)}")
    return handler(doc, op)


def _fingerprint(doc: dict) -> dict[str, tuple]:
    return {x["id"]: (x["part"], x.get("pitch"), x["start"], x["dur"], x.get("vel")) for x in doc["notes"] + doc["rests"]}


def changed_measures(old: dict, new: dict) -> list[int]:
    if (old["time"], old["key"], old["measure_lengths"]) != (new["time"], new["key"], new["measure_lengths"]):
        return list(range(1, new["measures"] + 1))
    a, b = _fingerprint(old), _fingerprint(new)
    tm_a, tm_b = TimeMap(old), TimeMap(new)
    out = set()
    for i in set(a) | set(b):
        if a.get(i) != b.get(i):
            if i in a:
                out.add(tm_a.measure_at(a[i][2]))
            if i in b:
                out.add(tm_b.measure_at(b[i][2]))
    return sorted(m for m in out if m <= new["measures"])


def apply_operations(doc: dict, ops: list[dict]) -> tuple[dict, dict]:
    if not isinstance(ops, list) or not ops:
        raise RollError("operations is a non-empty list")
    if len(ops) > MAX_OPS:
        raise RollError(f"at most {MAX_OPS} operations at once")
    work = copy.deepcopy(doc)
    applied, warnings = [], []
    for i, op in enumerate(ops, 1):
        name = op.get("op", "?") if isinstance(op, dict) else "?"
        try:
            info = apply_one(work, op)
            roll.validate(work)
            warns = roll.normalize(work)
        except (ValueError, KeyError, TypeError) as e:
            msg = f"missing {e}" if isinstance(e, KeyError) else str(e)
            raise RollError(f"operation {i} ({name}): {msg}") from None
        applied.append({"op": name, **info})
        warnings += [f"operation {i} ({name}): {w}" for w in warns]
    return work, {"applied": applied, "warnings": warnings, "changed_measures": changed_measures(doc, work)}
