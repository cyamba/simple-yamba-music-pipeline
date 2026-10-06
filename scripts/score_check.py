"""Compare a MusicXML file with a hand-written ground truth, measure by measure.

Usage:  uv run python scripts/score_check.py <file.musicxml> [<truth.json> <page>]

Without a truth file it prints the score in the same token form the truth uses, which is handy
for writing a new truth file. With one, it prints a per-measure scorecard.

Token form (one string per staff per measure, tokens separated by spaces):
    C4:8      eighth note C4          Bb5+D6:4.   dotted quarter chord
    r:2       half rest               ?:16        sixteenth note, pitch not transcribed
    R         the staff is empty or a full-measure rest
Durations are note types (1 2 4 8 16 32) with an optional dot. Pitches are written pitches (as
printed, so an 8va treble clef doesn't change them) and spelling matters: A#5 != Bb5.
A staff given as null in the truth file was not transcribed and is skipped.
"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

TYPES = {"1": Fraction(4), "2": Fraction(2), "4": Fraction(1), "8": Fraction(1, 2),
         "16": Fraction(1, 4), "32": Fraction(1, 8)}
ALTER = {"bb": -2, "b": -1, "": 0, "#": 1, "##": 2}
STEPS = "C D EF G A B"


@dataclass(frozen=True)
class Event:
    """Everything that starts at one moment on one staff: a rest, a note or a chord."""

    onset: Fraction  # in quarter notes from the start of the measure
    duration: Fraction
    pitches: tuple[str, ...]  # () for a rest

    @property
    def is_rest(self) -> bool:
        return not self.pitches


@dataclass
class Measure:
    number: str
    time: tuple[int, int]
    staves: dict[int, list[Event]] = field(default_factory=dict)

    @property
    def capacity(self) -> Fraction:
        beats, beat_type = self.time
        return Fraction(4 * beats, beat_type)

    def length(self, staff: int) -> Fraction:
        return max((e.onset + e.duration for e in self.staves.get(staff, [])), default=Fraction(0))


@dataclass
class Score:
    measures: list[Measure]
    clefs: dict[int, str]  # staff -> e.g. "G2", "G2+1" (treble 8va), "F4"


def pitch_name(step: str, alter: int, octave: int) -> str:
    acc = {v: k for k, v in ALTER.items()}.get(alter, f"({alter})")
    return f"{step}{acc}{octave}"


def pitch_key(p: str) -> int:
    step, acc, octave = p[0], p[1:-1], int(p[-1])
    return octave * 12 + STEPS.index(step) + ALTER.get(acc, 0)


def read_musicxml(path: str | Path) -> Score:
    """Read a single-part partwise MusicXML file. Grace notes are ignored."""
    part = ET.parse(path).getroot().find("part")
    if part is None:
        raise ValueError(f"{path}: no <part>")
    divisions, time = 1, (4, 4)
    clefs: dict[int, str] = {}
    octave_shift: dict[int, int] = {}  # staff -> clef-octave-change, to turn sounding into written pitch
    measures: list[Measure] = []
    for m in part.findall("measure"):
        notes: dict[tuple[int, Fraction, Fraction], set[str]] = {}
        pos = last_onset = Fraction(0)
        for el in m:
            if el.tag == "attributes":
                divisions = int(el.findtext("divisions", divisions))
                if (t := el.find("time")) is not None:
                    time = (int(t.findtext("beats")), int(t.findtext("beat-type")))
                for c in el.findall("clef"):
                    staff = int(c.get("number", "1"))
                    octave_shift[staff] = int(c.findtext("clef-octave-change", "0"))
                    clefs[staff] = f"{c.findtext('sign')}{c.findtext('line', '')}" + (
                        f"{octave_shift[staff]:+d}" if octave_shift[staff] else "")
            elif el.tag in ("backup", "forward"):
                d = Fraction(int(el.findtext("duration")), divisions)
                pos += -d if el.tag == "backup" else d
            elif el.tag == "note":
                if el.find("grace") is not None:
                    continue
                d = Fraction(int(el.findtext("duration", "0")), divisions)
                onset = last_onset if el.find("chord") is not None else pos
                staff = int(el.findtext("staff", "1"))
                slot = notes.setdefault((staff, onset, d), set())
                if (p := el.find("pitch")) is not None:
                    octave = int(p.findtext("octave")) - octave_shift.get(staff, 0)
                    slot.add(pitch_name(p.findtext("step"), int(float(p.findtext("alter", "0"))), octave))
                if el.find("chord") is None:
                    last_onset = onset
                    pos = onset + d
        measure = Measure(m.get("number", str(len(measures) + 1)), time)
        for (staff, onset, d), pitches in sorted(notes.items()):
            measure.staves.setdefault(staff, []).append(Event(onset, d, tuple(sorted(pitches, key=pitch_key))))
        measures.append(measure)
    return Score(measures, clefs)


def duration_token(d: Fraction) -> str:
    for name, value in TYPES.items():
        if d == value:
            return name
        if d == value * Fraction(3, 2):
            return name + "."
    return f"({d})"


def to_tokens(events: list[Event]) -> str:
    return " ".join(f"{'+'.join(e.pitches) or 'r'}:{duration_token(e.duration)}" for e in events)


def parse_tokens(text: str) -> list[Event]:
    """Parse a truth string ("C4:8 r:8 ?:4.") into events with onsets."""
    events, onset = [], Fraction(0)
    for tok in text.split():
        what, _, dur = tok.partition(":")
        base = TYPES[dur.rstrip(".")]
        d = base * Fraction(3, 2) if dur.endswith(".") else base
        pitches = () if what == "r" else tuple(what.split("+"))
        events.append(Event(onset, d, pitches))
        onset += d
    return events


def merge_rests(events: list[Event]) -> list[Event]:
    """Join adjacent rests: "r:8 r:2" and "r:4." are the same silence, written differently."""
    out: list[Event] = []
    for e in events:
        if e.is_rest and out and out[-1].is_rest and out[-1].onset + out[-1].duration == e.onset:
            out[-1] = Event(out[-1].onset, out[-1].duration + e.duration, ())
        else:
            out.append(e)
    return out


@dataclass
class StaffResult:
    rhythm_ok: bool  # same onsets, durations and rest/note pattern
    pitches_known: int  # pitches given in the truth
    pitches_correct: int  # of those, found at the same onset in the output


def compare_staff(truth: str, got: list[Event]) -> StaffResult:
    if truth.strip() == "R":
        return StaffResult(all(e.is_rest for e in got), 0, 0)
    expected = parse_tokens(truth)

    def shape(evs: list[Event]) -> list[tuple[Fraction, Fraction, bool]]:
        return [(e.onset, e.duration, e.is_rest) for e in merge_rests(evs)]

    got_at = {e.onset: set(e.pitches) for e in got}
    known = correct = 0
    for e in expected:
        for p in e.pitches:
            if p != "?":
                known += 1
                correct += p in got_at.get(e.onset, set())
    return StaffResult(shape(expected) == shape(got), known, correct)


@dataclass
class PageReport:
    measures_expected: int
    measures_found: int
    time_ok: int = 0
    overfull: list[str] = field(default_factory=list)
    rhythm_ok: int = 0
    staff_measures: int = 0
    pitches_known: int = 0
    pitches_correct: int = 0
    clefs_ok: bool = False
    lines: list[str] = field(default_factory=list)

    @property
    def pitch_accuracy(self) -> float:
        return self.pitches_correct / self.pitches_known if self.pitches_known else 1.0

    def summary(self) -> str:
        return (f"measures {self.measures_found}/{self.measures_expected}  "
                f"time signatures {self.time_ok}/{self.measures_expected}  "
                f"clefs {'ok' if self.clefs_ok else 'wrong'}  overfull {len(self.overfull)}  "
                f"rhythm {self.rhythm_ok}/{self.staff_measures}  "
                f"pitches {self.pitches_correct}/{self.pitches_known}")


def load_truth(path: str | Path, page: str) -> dict:
    data = json.loads(Path(path).read_text())
    return {"clefs": data["clefs"], **data["pages"][page]}


def compare(score: Score, truth: dict) -> PageReport:
    """Measures are matched by position, not by number: HOMR numbers every page from 1."""
    t_measures = truth["measures"]
    report = PageReport(len(t_measures), len(score.measures))
    report.clefs_ok = {str(k): v for k, v in score.clefs.items()} == truth["clefs"]
    for m in score.measures:
        for staff in m.staves:
            if m.length(staff) > m.capacity:
                report.overfull.append(f"{m.number}/{staff}")
    for i, tm in enumerate(t_measures):
        got = score.measures[i] if i < len(score.measures) else Measure("-", (0, 1))
        time = tuple(int(x) for x in tm["time"].split("/"))
        report.time_ok += got.time == time
        cells = [f"m{tm['number']:>3} {tm['time']:>4} {'ok ' if got.time == time else 'TS!'}"]
        for staff_key, staff in (("rh", 1), ("lh", 2)):
            if tm[staff_key] is None:
                cells.append(f"{staff_key}: (not transcribed)      ")
                continue
            res = compare_staff(tm[staff_key], got.staves.get(staff, []))
            report.staff_measures += 1
            report.rhythm_ok += res.rhythm_ok
            report.pitches_known += res.pitches_known
            report.pitches_correct += res.pitches_correct
            pitch = f"{res.pitches_correct}/{res.pitches_known}" if res.pitches_known else "-"
            cells.append(f"{staff_key}: rhythm {'ok' if res.rhythm_ok else '--'} pitch {pitch:>5}")
        report.lines.append("  ".join(cells))
    return report


def main() -> None:
    if len(sys.argv) not in (2, 4):
        sys.exit(__doc__)
    score = read_musicxml(sys.argv[1])
    if len(sys.argv) == 2:
        print(f"clefs: {score.clefs}")
        for m in score.measures:
            print(f"m{m.number} {m.time[0]}/{m.time[1]}")
            for staff, events in sorted(m.staves.items()):
                print(f"  {staff}: {to_tokens(events)}")
        return
    r = compare(score, load_truth(sys.argv[2], sys.argv[3]))
    print("\n".join(r.lines))
    print(r.summary() + f"  overfull at {r.overfull}")


if __name__ == "__main__":
    main()
