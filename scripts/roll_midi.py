"""A piano roll as a standard MIDI file, and a MIDI file as a piano roll.

write_midi: format 1, 480 ticks to the quarter. Track 1 is the conductor (title, tempo, time and key
signatures); then one track per part, named after it, with its instrument's name and program on that
instrument's channel (parts of one instrument share a channel; drums' channel 10 is skipped). Rest
markers can't be written; the file lasts until the end of the score.

read_midi: tracks (or, in a format 0 file, channels) become parts, and tracks on one channel one
instrument, so a roll's own MIDI comes back as it was. Times are snapped to a grid; notes on channel 10
(drums) are left out and counted.
"""
from __future__ import annotations

import struct
from fractions import Fraction

import roll
from pitch import Key
from roll import TPQ, TimeMap

DRUMS = 9


def _vlq(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _meta(kind: int, data: bytes) -> bytes:
    return bytes([0xFF, kind]) + _vlq(len(data)) + data


def _track(events: list[tuple[int, int, bytes]], end: int) -> bytes:
    """events: (tick, order, bytes); a lower order goes first at the same tick (meta, offs, then ons)."""
    out, now = bytearray(), 0
    for tick, _, data in sorted(events, key=lambda e: (e[0], e[1])):
        out += _vlq(tick - now) + data
        now = tick
    out += _vlq(max(0, end - now)) + _meta(0x2F, b"")
    return b"MTrk" + struct.pack(">I", len(out)) + bytes(out)


def _effective_sig(tm: TimeMap, m: int) -> tuple[int, int]:
    """The time signature MIDI should state for measure m (an irregular measure gets its own)."""
    sig, length = tm.sig(m), tm.length(m)
    if length == roll.capacity(sig):
        return sig
    for beat_type in (sig[1], 4, 8, 16, 32, 64, 128):
        if (length * beat_type) % roll.WHOLE == 0:
            return length * beat_type // roll.WHOLE, beat_type
    return sig


def channels(doc: dict) -> dict[str, int]:
    free = [c for c in range(16) if c != DRUMS]
    return {g["id"]: free[i % len(free)] for i, g in enumerate(doc["groups"])}


def write_midi(doc: dict) -> bytes:
    tm = TimeMap(doc)
    end = tm.end
    conductor: list[tuple[int, int, bytes]] = []
    if doc["title"]:
        conductor.append((0, 0, _meta(0x03, doc["title"].encode("utf-8"))))
    last = None
    for m in range(1, tm.measures + 1):
        sig = _effective_sig(tm, m)
        if sig != last:
            beats, beat_type = sig
            clocks = max(1, 96 // beat_type)
            conductor.append((tm.start(m), 1, _meta(0x58, bytes([beats, beat_type.bit_length() - 1, clocks, 8]))))
            last = sig
    for e in doc["key"]:
        k = roll.key_of(e)
        conductor.append((tm.start(e["measure"]), 1, _meta(0x59, struct.pack(">bB", max(-7, min(7, k.fifths)),
                                                                             1 if k.mode == "minor" else 0))))
    for e in doc["tempo"]:
        conductor.append((e["tick"], 1, _meta(0x51, round(60_000_000 / e["bpm"]).to_bytes(3, "big"))))
    tracks = [_track(conductor, end)]
    chan = channels(doc)
    for p in doc["parts"]:
        group = next(g for g in doc["groups"] if g["id"] == p["group"])
        c = chan[group["id"]]
        events = [(0, 0, _meta(0x03, p["name"].encode("utf-8"))), (0, 0, _meta(0x04, group["name"].encode("utf-8"))),
                  (0, 1, bytes([0xC0 | c, group["program"]]))]
        for n in doc["notes"]:
            if n["part"] == p["id"]:
                events.append((n["start"], 3, bytes([0x90 | c, n["pitch"], n["vel"]])))
                events.append((n["start"] + n["dur"], 2, bytes([0x80 | c, n["pitch"], 0])))
        tracks.append(_track(events, end))
    return b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), TPQ) + b"".join(tracks)


# --- reading ---------------------------------------------------------------------------------------------------
class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise roll.RollError("the MIDI file ends in the middle of an event")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def vlq(self) -> int:
        n = 0
        for _ in range(4):
            b = self.take(1)[0]
            n = (n << 7) | (b & 0x7F)
            if not b & 0x80:
                return n
        raise roll.RollError("bad variable-length number in the MIDI file")


def _chunks(data: bytes):
    r = _Reader(data)
    while r.pos + 8 <= len(data):
        kind, length = r.take(4), struct.unpack(">I", r.take(4))[0]
        yield kind, r.take(length)


def parse(data: bytes) -> dict:
    """The raw content of a MIDI file: tracks with their notes and metas, in source ticks."""
    chunks = list(_chunks(data))
    if not chunks or chunks[0][0] != b"MThd":
        raise roll.RollError("not a MIDI file (no MThd header)")
    fmt, ntracks, division = struct.unpack(">HHH", chunks[0][1][:6])
    if division & 0x8000:
        raise roll.RollError("SMPTE-timed MIDI files are not supported")
    tracks = []
    for kind, body in chunks[1:]:
        if kind != b"MTrk":
            continue
        r, tick, status = _Reader(body), 0, 0
        t = {"name": "", "instrument": "", "notes": [], "programs": {}, "tempos": [], "times": [], "keys": [],
             "end": 0}
        pending: dict[tuple[int, int], list[tuple[int, int]]] = {}
        while r.pos < len(body):
            tick += r.vlq()
            b = r.take(1)[0]
            if b == 0xFF:
                kind_, data_ = r.take(1)[0], r.take(r.vlq())
                if kind_ == 0x03 and not t["name"]:
                    t["name"] = data_.decode("utf-8", "replace").strip()
                elif kind_ == 0x04 and not t["instrument"]:
                    t["instrument"] = data_.decode("utf-8", "replace").strip()
                elif kind_ == 0x51 and len(data_) == 3:
                    t["tempos"].append((tick, int.from_bytes(data_, "big")))
                elif kind_ == 0x58 and len(data_) >= 2:
                    t["times"].append((tick, data_[0], 2 ** data_[1]))
                elif kind_ == 0x59 and len(data_) == 2:
                    t["keys"].append((tick, struct.unpack(">b", data_[:1])[0], data_[1]))
                elif kind_ == 0x2F:
                    break
                continue
            if b in (0xF0, 0xF7):
                r.take(r.vlq())
                continue
            if b & 0x80:
                status, first = b, r.take(1)[0]
            else:
                if not status:
                    raise roll.RollError("running status without a status byte")
                first = b
            kind_, chan = status & 0xF0, status & 0x0F
            if kind_ in (0xC0, 0xD0):
                if kind_ == 0xC0:
                    t["programs"].setdefault(chan, first)
                continue
            second = r.take(1)[0]
            if kind_ == 0x90 and second:
                pending.setdefault((chan, first), []).append((tick, second))
            elif kind_ in (0x80, 0x90):
                starts = pending.get((chan, first))
                if starts:
                    start, vel = starts.pop(0)
                    t["notes"].append((chan, first, start, tick, vel))
        for (chan, pitch), starts in pending.items():  # never released: they last to the track's end
            t["notes"] += [(chan, pitch, s, tick, v) for s, v in starts]
        t["end"] = tick
        tracks.append(t)
    return {"format": fmt, "division": division, "tracks": tracks}


def _snap(x: Fraction, q: int) -> int:
    return int((x / q).__round__()) * q


def read_midi(data: bytes, grid: str | None = None, split: str | int | None = None, title: str = "") -> tuple[dict, dict]:
    """A roll from MIDI bytes, and what happened on the way: {'snapped': notes moved to the grid,
    'dropped': {'drums': n}}. grid: the grid times are snapped to (default: the finest the file needs,
    down to a 64th, else 16th); split: a pitch (e.g. 'C4') at which a one-part instrument is split into
    an upper and a lower part."""
    raw = parse(data)
    scale = Fraction(TPQ, raw["division"])
    notes = [(ti, chan, pitch, start * scale, end * scale, vel)
             for ti, t in enumerate(raw["tracks"]) for chan, pitch, start, end, vel in t["notes"]]
    drums = sum(1 for n in notes if n[1] == DRUMS)
    notes = [n for n in notes if n[1] != DRUMS]
    if grid is None:
        times = {x for n in notes for x in (n[3], n[4])}
        grid = next((g for g in ("16", "32", "64") if all(x % roll.VALUES[g] == 0 for x in times)), "16")
    q = roll.grid_ticks(grid)
    metas = {k: sorted(x for t in raw["tracks"] for x in t[k]) for k in ("tempos", "times", "keys")}

    lines: dict[tuple[int, int], dict] = {}  # (track, channel) -> part
    for ti, chan, pitch, start, end, vel in notes:
        lines.setdefault((ti, chan), {"notes": []})["notes"].append((pitch, start, end, vel))
    coarse = lambda g: g if roll.VALUES[g] > roll.VALUES[grid] else grid  # noqa: E731 - never finer than min
    doc = roll.new_doc(title=title, preset="single", grid={"min": grid, "default": coarse("8"), "max": coarse("4")})
    doc["groups"], doc["parts"], doc["notes"] = [], [], []
    by_channel: dict[int, dict] = {}
    snapped = 0
    for (ti, chan), line in sorted(lines.items()):
        track = raw["tracks"][ti]
        program = next((t["programs"][chan] for t in raw["tracks"] if chan in t["programs"]), 0)
        group = by_channel.get(chan)
        if group is None or group["program"] != program:
            group = {"id": f"P{len(doc['groups']) + 1}", "name": track["instrument"] or track["name"]
                     or f"Channel {chan + 1}", "program": program}
            doc["groups"].append(group)
            by_channel[chan] = group
        name = track["name"] or group["name"]
        pid = f"t{ti + 1}" + (f"c{chan + 1}" if raw["format"] == 0 else "")
        pitches = sorted(p for p, *_ in line["notes"])
        part = {"id": pid, "name": name, "color": roll.COLORS[len(doc["parts"]) % len(roll.COLORS)],
                "group": group["id"], "staff": len([p for p in doc["parts"] if p["group"] == group["id"]]) + 1,
                "clef": "G2" if pitches[len(pitches) // 2] >= 60 else "F4"}
        doc["parts"].append(part)
        for pitch, start, end, vel in line["notes"]:
            s, e = _snap(start, q), _snap(end, q)
            e = max(e, s + q)
            snapped += (s != start) or (e != end)
            doc["notes"].append({"id": roll.new_id(doc), "part": pid, "pitch": pitch, "start": s, "dur": e - s,
                                 "vel": max(1, min(127, vel))})
    if not doc["parts"]:
        raise roll.RollError("the MIDI file has no notes" + (" besides drums" if drums else ""))
    if split is not None:
        _split(doc, roll.parse_pitch(split)[0])

    end = max(n["start"] + n["dur"] for n in doc["notes"])
    _times(doc, metas["times"], scale, end)
    if metas["keys"]:
        _, sf, mi = metas["keys"][0]
        k = Key.from_fifths(max(-7, min(7, sf)), "minor" if mi else "major")
        doc["key"] = [{"measure": 1, "tonic": k.tonic_name, "mode": k.mode}]
    tempos = [{"tick": _snap(t * scale, roll.QUANTUM), "bpm": round(60_000_000 / us, 2)} for t, us in metas["tempos"] if us]
    doc["tempo"] = list({e["tick"]: e for e in tempos}.values()) or [{"tick": 0, "bpm": 120}]
    if not doc["title"]:
        doc["title"] = next((t["name"] for t in raw["tracks"] if t["name"] and not t["notes"]), "")
    roll.validate(doc)
    warnings = roll.normalize(doc)
    return doc, {"snapped": snapped, "dropped": {"drums": drums} if drums else {}, "grid": grid,
                 "warnings": warnings}


def _split(doc: dict, at: int) -> None:
    for group in list(doc["groups"]):
        parts = [p for p in doc["parts"] if p["group"] == group["id"]]
        if len(parts) != 1:
            continue
        upper = parts[0]
        lower = {**upper, "id": upper["id"] + "-lower", "name": "Left hand", "staff": 2, "clef": "F4",
                 "color": roll.COLORS[(roll.COLORS.index(upper["color"]) + 1) % len(roll.COLORS)]
                 if upper["color"] in roll.COLORS else roll.COLORS[1]}
        upper.update({"name": "Right hand", "clef": "G2"})
        doc["parts"].insert(doc["parts"].index(upper) + 1, lower)
        for n in doc["notes"]:
            if n["part"] == upper["id"] and n["pitch"] < at:
                n["part"] = lower["id"]


def _times(doc: dict, times: list[tuple], scale: Fraction, end: int) -> None:
    """Time signatures from MIDI events, placed on the barlines they fall on."""
    entries, sig, bar_start, measure = [], (4, 4), 0, 1
    changes = [(_snap(t * scale, roll.QUANTUM), (n, d)) for t, n, d in times]
    for tick, new in changes:
        while bar_start + roll.capacity(sig) <= tick:
            bar_start += roll.capacity(sig)
            measure += 1
        if tick > bar_start:  # mid-measure: it takes effect at the next barline
            bar_start += roll.capacity(sig)
            measure += 1
        try:
            roll.parse_time(f"{new[0]}/{new[1]}")
        except roll.RollError:
            continue
        entries = [e for e in entries if e["measure"] != measure]
        entries.append({"measure": measure, "beats": new[0], "beat_type": new[1]})
        sig = new
    doc["time"] = entries or [{"measure": 1, "beats": 4, "beat_type": 4}]
    doc["measures"] = max(e["measure"] for e in doc["time"])
    doc["measures"] = max(doc["measures"], TimeMap(doc).measures_to_cover(end))
