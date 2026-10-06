"""Fix what HOMR gets systematically wrong in its MusicXML, measure by measure.

Usage:  uv run python scripts/postprocess.py <in.musicxml> [-o <out.musicxml>] [--treble-8va STAFF]

1. Staff timing. HOMR times both staves of a grand staff with one cursor, in reading order, so a
   staff whose rhythm doesn't line up with the other one (long rests against sixteenths) gets
   its notes at wrong, overlapping onsets. Such a staff is laid out again note after note, unless
   that would make it longer than the measure: then the overlap is real polyphony and is kept.
2. Time signatures. HOMR's model only reads the denominator, and HOMR uses one numerator (the
   median measure length) for the whole page. Each measure gets the time signature its content
   fills, keeping the denominator HOMR read there, or the current one, when it fits.
3. Short staves are filled with rests (MuseScore refuses "Incomplete measure").
4. --treble-8va STAFF: HOMR has no octave clefs. Marks that staff's treble clef 8va and raises its
   pitches by an octave (MusicXML stores sounding pitch); the printed notes stay where they are.
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

# Rest values in quarters, longest first; dotted values only where they are common.
REST_VALUES = [(Fraction(4), "whole", False), (Fraction(3), "half", True), (Fraction(2), "half", False),
               (Fraction(3, 2), "quarter", True), (Fraction(1), "quarter", False),
               (Fraction(1, 2), "eighth", False), (Fraction(1, 4), "16th", False), (Fraction(1, 8), "32nd", False)]
ATTRIBUTE_ORDER = ["footnote", "level", "divisions", "key", "time", "staves", "part-symbol", "instruments",
                   "clef", "staff-details", "transpose", "directive", "measure-style"]


@dataclass
class Chord:
    """Notes that start together on one staff and voice. Times are in divisions."""

    staff: int
    voice: str
    onset: int
    duration: int
    notes: list[ET.Element]


@dataclass
class Report:
    time_signatures: int = 0  # measures whose time signature changed
    retimed: list[str] = field(default_factory=list)  # "measure/staff"
    padded: list[str] = field(default_factory=list)
    octave_clefs: int = 0

    def __str__(self) -> str:
        parts = [f"{self.time_signatures} time signatures"]
        if self.retimed:
            parts.append(f"re-timed {', '.join(self.retimed)}")
        if self.padded:
            parts.append(f"padded {', '.join(self.padded)}")
        if self.octave_clefs:
            parts.append("treble 8va")
        return "; ".join(parts)


def read_chords(measure: ET.Element) -> tuple[list[ET.Element], list[Chord], list[ET.Element]] | None:
    """Split a measure into (elements before the notes, chords, elements after the notes).

    Returns None for measures this script won't rebuild: grace notes, or attributes, directions
    etc. between notes (a clef change mid-measure would move).
    """
    head, tail, chords = [], [], []
    pos = 0
    for el in measure:
        if el.tag == "note":
            if el.find("grace") is not None or tail:
                return None
            if el.find("chord") is not None and chords:
                chords[-1].notes.append(el)
                continue
            d = int(el.findtext("duration", "0"))
            chords.append(Chord(int(el.findtext("staff", "1")), el.findtext("voice", "1"), pos, d, [el]))
            pos += d
        elif el.tag in ("backup", "forward"):
            if tail:
                return None
            d = int(el.findtext("duration", "0"))
            pos += -d if el.tag == "backup" else d
        else:
            (tail if chords else head).append(el)
    return head, chords, tail


def is_sequential(chords: list[Chord]) -> bool:
    pos = 0
    for c in sorted(chords, key=lambda c: c.onset):
        if c.onset != pos:
            return False
        pos += c.duration
    return True


def end(chords: list[Chord]) -> int:
    return max((c.onset + c.duration for c in chords), default=0)


def round_up(value: int, grid: int) -> int:
    return -(-value // grid) * grid


def retime(chords: list[Chord], limit: int) -> bool:
    """Lay one staff's chords out back to back, in reading order, if that fits in `limit`."""
    if is_sequential(chords) or len({c.onset for c in chords}) < len(chords):
        return False  # already fine, or two chords start together: real polyphony
    if sum(c.duration for c in chords) > limit:
        return False
    voice = min((c.voice for c in chords), key=int)
    pos = 0
    for c in sorted(chords, key=lambda c: c.onset):  # stable: HOMR's order for equal onsets
        c.onset, c.voice = pos, voice
        for n in c.notes:
            if (v := n.find("voice")) is not None:
                v.text = voice
        pos += c.duration
    return True


def sub(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    el.text = text
    return el


def rests(length: int, divisions: int, staff: int, voice: str, whole_measure: bool) -> list[ET.Element]:
    """Rest notes filling `length` divisions; one measure rest if the staff is empty."""
    if whole_measure:
        n = ET.Element("note")
        sub(n, "rest", measure="yes")
        sub(n, "duration", str(length))
        sub(n, "voice", voice)
        sub(n, "staff", str(staff))
        return [n]
    out, left = [], Fraction(length, divisions)
    while left > 0:
        # no fitting value (e.g. a triplet remainder): one rest without <type>, sized by duration
        value, kind, dotted = next((v for v in REST_VALUES if v[0] <= left and (v[0] * divisions).denominator == 1),
                                   (left, None, False))
        n = ET.Element("note")
        sub(n, "rest")
        sub(n, "duration", str(int(value * divisions)))
        sub(n, "voice", voice)
        if kind:
            sub(n, "type", kind)
        if dotted:
            sub(n, "dot")
        sub(n, "staff", str(staff))
        out.append(n)
        left -= value
    return out


def write_measure(measure: ET.Element, head: list[ET.Element], chords: list[Chord],
                  tail: list[ET.Element], extra: dict[tuple[int, str], list[ET.Element]]) -> None:
    """Rewrite the measure as: head, then each staff/voice from onset 0 (with forward for gaps
    and `extra` notes appended to it), then tail."""
    for el in list(measure):
        measure.remove(el)
    for el in head:
        measure.append(el)
    voices = list(dict.fromkeys([(c.staff, c.voice) for c in chords] + list(extra)))
    pos = 0
    for staff, voice in sorted(voices, key=lambda sv: (sv[0], int(sv[1]))):
        if pos:
            sub(sub(measure, "backup"), "duration", str(pos))
            pos = 0
        for c in sorted((c for c in chords if (c.staff, c.voice) == (staff, voice)), key=lambda c: c.onset):
            if c.onset > pos:
                fwd = sub(measure, "forward")
                sub(fwd, "duration", str(c.onset - pos))
                sub(fwd, "voice", voice)
                sub(fwd, "staff", str(staff))
            elif c.onset < pos:  # overlap within one voice: rewind rather than shift the notes
                sub(sub(measure, "backup"), "duration", str(pos - c.onset))
            for n in c.notes:
                measure.append(n)
            pos = c.onset + c.duration
        for n in extra.get((staff, voice), []):
            measure.append(n)
            pos += int(n.findtext("duration"))
    for el in tail:
        measure.append(el)


def time_signature(length: int, divisions: int, preferred: list[int]) -> tuple[int, int]:
    for beat_type in preferred + [4, 8, 16]:
        beats = Fraction(length * beat_type, 4 * divisions)
        if beats.denominator == 1 and beats > 0:
            return int(beats), beat_type
    raise ValueError(f"no time signature for {length} divisions at {divisions} per quarter")


def set_time(measure: ET.Element, beats: int, beat_type: int) -> None:
    attrs = measure.find("attributes")
    if attrs is None:
        attrs = ET.Element("attributes")
        leading_prints = 0
        for el in measure:  # keep <print new-system> first
            if el.tag != "print":
                break
            leading_prints += 1
        measure.insert(leading_prints, attrs)
    time = ET.Element("time")
    sub(time, "beats", str(beats))
    sub(time, "beat-type", str(beat_type))
    rank = ATTRIBUTE_ORDER.index("time")
    index = sum(1 for el in attrs if el.tag in ATTRIBUTE_ORDER and ATTRIBUTE_ORDER.index(el.tag) < rank)
    attrs.insert(index, time)


def make_treble_8va(part: ET.Element, staff: int) -> int:
    """Only notes read while the staff is in a treble clef move up, in case HOMR read a clef change."""
    clefs, treble = 0, False
    for el in part.iter():
        if el.tag == "clef" and int(el.get("number", "1")) == staff:
            treble = el.findtext("sign") == "G" and el.findtext("line", "2") == "2"
            if treble and el.find("clef-octave-change") is None:
                change = ET.Element("clef-octave-change")
                change.text = "1"
                el.insert(list(el).index(el.find("line")) + 1 if el.find("line") is not None else len(el), change)
                clefs += 1
        elif el.tag == "note" and treble and int(el.findtext("staff", "1")) == staff:
            if (octave := el.find("pitch/octave")) is not None:
                octave.text = str(int(octave.text) + 1)
    return clefs


def fix_part(part: ET.Element, treble_8va: int | None = None) -> Report:
    report = Report()
    divisions, staves, current = 1, 1, None
    for measure in part.findall("measure"):
        number = measure.get("number", "?")
        for attrs in measure.findall("attributes"):
            divisions = int(attrs.findtext("divisions", divisions))
            staves = int(attrs.findtext("staves", staves))
        read_beat_types = [int(t.findtext("beat-type")) for t in measure.iter("time")]
        for attrs in measure.findall("attributes"):
            for t in attrs.findall("time"):
                attrs.remove(t)
        grid = divisions // 2 if divisions % 2 == 0 else divisions  # eighths, if divisions allow

        parsed = read_chords(measure)
        if parsed is None:
            length = round_up(max(end_of_staff(measure), 1), grid)
        else:
            head, chords, tail = parsed
            by_staff = {s: [c for c in chords if c.staff == s] for s in range(1, staves + 1)}
            limit = round_up(max(end(cs) for cs in by_staff.values()), grid)
            for s, cs in by_staff.items():
                if retime(cs, limit):
                    report.retimed.append(f"{number}/{s}")
            length = round_up(max(end(cs) for cs in by_staff.values()), grid)
            if length == 0:  # empty measure: keep the meter
                length = current[0] * 4 * divisions // current[1] if current else 4 * divisions
            extra = {}
            for s, cs in by_staff.items():
                main = [c for c in cs if c.voice == cs[0].voice] if cs else []
                if end(main) < length:
                    voice = cs[0].voice if cs else str((s - 1) * 4 + 1)
                    extra[(s, voice)] = rests(length - end(main), divisions, s, voice, whole_measure=not cs)
                    report.padded.append(f"{number}/{s}")
            write_measure(measure, head, chords, tail, extra)

        new = time_signature(length, divisions, read_beat_types[:1] or ([current[1]] if current else []))
        if new != current:
            set_time(measure, *new)
            report.time_signatures += 1
            current = new
    if treble_8va:
        report.octave_clefs = make_treble_8va(part, treble_8va)
    return report


def end_of_staff(measure: ET.Element) -> int:
    """Furthest point any note reaches, for measures that are not rebuilt."""
    pos = furthest = last = 0
    for el in measure:
        if el.tag == "note" and el.find("grace") is None:
            d = int(el.findtext("duration", "0"))
            start = last if el.find("chord") is not None else pos
            if el.find("chord") is None:
                last, pos = start, start + d
            furthest = max(furthest, start + d)
        elif el.tag in ("backup", "forward"):
            d = int(el.findtext("duration", "0"))
            pos += -d if el.tag == "backup" else d
    return furthest


def fix_file(src: str | Path, dst: str | Path, treble_8va: int | None = None) -> Report:
    tree = ET.parse(src)
    report = Report()
    for part in tree.getroot().findall("part"):
        r = fix_part(part, treble_8va)
        report.time_signatures += r.time_signatures
        report.retimed += r.retimed
        report.padded += r.padded
        report.octave_clefs += r.octave_clefs
    ET.indent(tree, space="  ")
    tree.write(dst, encoding="UTF-8", xml_declaration=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("-o", "--out", help="output file (default: overwrite src)")
    ap.add_argument("--treble-8va", type=int, metavar="STAFF", help="mark this staff's treble clef 8va")
    args = ap.parse_args()
    print(fix_file(args.src, args.out or args.src, args.treble_8va))


if __name__ == "__main__":
    main()
