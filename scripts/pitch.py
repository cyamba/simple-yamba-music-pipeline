"""Spelled pitches, keys with church modes, and the two mirrors the piano roll uses.

Trimmed from yamba-compositions-engine (core/pitch.py, core/keys.py). A pitch is (d, c): d is the
diatonic index (octave * 7 + step, C4 -> 28) and c is the MIDI number (C4 -> 60). A chromatic mirror
maps (d, c) to (sum_d - d, sum_c - c), so mirroring about D4 sends F4 to B3 and Bb4 to F#3, and
mirroring twice is the identity. A diatonic mirror flips scale degrees about a scale note and negates
each note's chromatic deviation from the scale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

STEP_NAMES = "CDEFGAB"
STEP_SEMITONES = (0, 2, 4, 5, 7, 9, 11)
ACCIDENTALS = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}
_PITCH_RE = re.compile(r"^([A-Ga-g])(#{1,2}|x|b{1,2}|)?(-?\d+)$")

IONIAN = (0, 2, 4, 5, 7, 9, 11)
MODES = ("major", "dorian", "phrygian", "lydian", "mixolydian", "minor", "locrian")  # ionian .. locrian
SCALES = {mode: tuple((IONIAN[(i + k) % 7] - IONIAN[i]) % 12 for k in range(7)) for i, mode in enumerate(MODES)}
MODE_ALIASES = {"ionian": "major", "aeolian": "minor"}


@dataclass(frozen=True, order=True)
class Pitch:
    d: int
    c: int

    @classmethod
    def from_parts(cls, step: str, alter: int, octave: int) -> Pitch:
        step_index = STEP_NAMES.index(step.upper())
        return cls(octave * 7 + step_index, 12 * (octave + 1) + STEP_SEMITONES[step_index] + alter)

    @classmethod
    def parse(cls, text: str) -> Pitch:
        """Names such as "C4", "Bb3", "F#5", "Ebb2", "Gx4" (x = double sharp)."""
        match = _PITCH_RE.match(text.strip())
        if not match:
            raise ValueError(f"not a pitch name: {text!r} (expected e.g. C4, Bb3, F#5)")
        step, accidental, octave = match.groups()
        accidental = accidental or ""
        alter = 2 if accidental == "x" else accidental.count("#") - accidental.count("b")
        return cls.from_parts(step, alter, int(octave))

    @property
    def step_index(self) -> int:
        return self.d % 7

    @property
    def step(self) -> str:
        return STEP_NAMES[self.step_index]

    @property
    def octave(self) -> int:
        return self.d // 7

    @property
    def alter(self) -> int:
        return self.c - (12 * (self.octave + 1) + STEP_SEMITONES[self.step_index])

    @property
    def name(self) -> str:
        alter = self.alter
        accidental = ACCIDENTALS.get(alter) or ("#" * alter if alter > 0 else "b" * -alter)
        return f"{self.step}{accidental}{self.octave}"

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Mirror:
    """Pitch inversion p -> (sum_d - d, sum_c - c)."""

    sum_d: int
    sum_c: int

    @classmethod
    def about(cls, axis: Pitch) -> Mirror:
        return cls(2 * axis.d, 2 * axis.c)

    @classmethod
    def between(cls, low: Pitch, high: Pitch) -> Mirror:
        """Swap two pitches, e.g. between(D4, A4) sends D4 to A4 and A4 to D4."""
        return cls(low.d + high.d, low.c + high.c)

    def apply(self, pitch: Pitch) -> Pitch:
        return Pitch(self.sum_d - pitch.d, self.sum_c - pitch.c)


def mode_name(mode: str) -> str:
    m = MODE_ALIASES.get(mode.strip().lower(), mode.strip().lower())
    if m not in SCALES:
        raise ValueError(f"unknown mode {mode!r}; use one of {', '.join(MODES)} (or ionian, aeolian)")
    return m


@dataclass(frozen=True)
class Key:
    tonic_step: int  # 0..6, C..B
    tonic_alter: int
    mode: str = "major"

    @classmethod
    def parse(cls, tonic: str, mode: str = "major") -> Key:
        """Key.parse("D", "dorian"); the tonic may carry an octave ("D4"), which is ignored."""
        text = tonic.strip()
        pitch = Pitch.parse(text if text[-1:].isdigit() else text + "4")
        return cls(pitch.step_index, pitch.alter, mode_name(mode))

    @classmethod
    def from_text(cls, text: str) -> Key:
        """ "D dorian", "Bb major", "F# minor", or just "G" (major)."""
        tonic, _, mode = text.strip().partition(" ")
        return cls.parse(tonic, mode or "major")

    @classmethod
    def from_fifths(cls, fifths: int, mode: str = "major") -> Key:
        """The key with this signature whose final is the given mode (fifths=0, dorian -> D dorian)."""
        mode = mode_name(mode)
        index = fifths + 1  # the major tonic on the line of fifths F C G D A E B, then F# .. or Bb ..
        major = cls(STEP_NAMES.index("FCGDAEB"[index % 7]), index // 7, "major")
        tonic = major.pitch_at(28 + major.tonic_step + MODES.index(mode))
        return cls(tonic.step_index, tonic.alter, mode)

    @property
    def tonic_pc(self) -> int:
        return (STEP_SEMITONES[self.tonic_step] + self.tonic_alter) % 12

    @property
    def tonic_name(self) -> str:
        return Pitch.from_parts(STEP_NAMES[self.tonic_step], self.tonic_alter, 4).name[:-1]

    @property
    def fifths(self) -> int:
        """Key signature: sharps positive, flats negative (D dorian -> 0, Bb major -> -2)."""
        index = MODES.index(self.mode)
        major_step = (self.tonic_step - index) % 7
        major_pc = (self.tonic_pc - IONIAN[index]) % 12
        natural_fifths = {0: 0, 4: 1, 1: 2, 5: 3, 2: 4, 6: 5, 3: -1}[major_step]
        alter = ((major_pc - STEP_SEMITONES[major_step] + 6) % 12) - 6
        return natural_fifths + 7 * alter

    def __str__(self) -> str:
        return f"{self.tonic_name} {self.mode}"

    def degree_of(self, pitch: Pitch) -> tuple[int, int]:
        """(degree 0..6 counted from the tonic, chromatic deviation from that scale step)."""
        degree = (pitch.step_index - self.tonic_step) % 7
        expected = (self.tonic_pc + SCALES[self.mode][degree]) % 12
        return degree, ((pitch.c - expected + 6) % 12) - 6

    def pitch_at(self, d: int, deviation: int = 0) -> Pitch:
        """The scale note written on diatonic index d, raised or lowered by `deviation`."""
        degree = (d % 7 - self.tonic_step) % 7
        target_pc = (self.tonic_pc + SCALES[self.mode][degree] + deviation) % 12
        natural = 12 * (d // 7 + 1) + STEP_SEMITONES[d % 7]
        return Pitch(d, natural + ((target_pc - natural + 6) % 12) - 6)

    def in_scale(self, pitch: Pitch) -> bool:
        return self.degree_of(pitch)[1] == 0

    def spell(self, midi: int) -> Pitch:
        """A MIDI number spelled in this key: a scale note as itself (B#3 in C# major), any other as a
        natural if it is one, else with the key's accidental direction (sharps in sharp keys and C, flats
        in flat keys)."""
        for degree, semis in enumerate(SCALES[self.mode]):
            if (self.tonic_pc + semis) % 12 == midi % 12:
                return _on_step((self.tonic_step + degree) % 7, midi)
        if midi % 12 in STEP_SEMITONES:
            return _on_step(STEP_SEMITONES.index(midi % 12), midi)
        wanted = 1 if self.fifths >= 0 else -1
        step = next(s for s, semis in enumerate(STEP_SEMITONES) if (midi - semis - wanted) % 12 == 0)
        return _on_step(step, midi)


def _on_step(step: int, midi: int) -> Pitch:
    """MIDI number midi written on letter `step` (0..6), in whichever octave keeps the accidental small."""
    octave = min(range(midi // 12 - 2, midi // 12 + 1),
                 key=lambda o: abs(midi - 12 * (o + 1) - STEP_SEMITONES[step]))
    return Pitch(octave * 7 + step, midi)


def mirror_diatonic(pitch: Pitch, key: Key, axis_d: int) -> Pitch:
    """Flip the scale degree about diatonic index axis_d / 2 (axis_d is the sum of the two axis notes'
    indexes, so 2 * D4.d for D4 itself); a note off the scale keeps its deviation, negated."""
    _, deviation = key.degree_of(pitch)
    return key.pitch_at(axis_d - pitch.d, -deviation)


def spell_name(midi: int, key: Key) -> str:
    return key.spell(midi).name
