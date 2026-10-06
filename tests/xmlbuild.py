"""Small MusicXML snippets for tests, shaped like HOMR's output (one part, two staves)."""
from __future__ import annotations

from pathlib import Path

TYPES = {16: "whole", 8: "half", 4: "quarter", 2: "eighth", 1: "16th"}  # by duration at divisions=4


def note(pitch: str, dur: int, staff: int = 1, voice: int | None = None, chord: bool = False) -> str:
    step, octave = pitch[0], pitch[-1]
    alter = {"#": 1, "b": -1}.get(pitch[1:-1], 0)
    return (f"<note>{'<chord/>' if chord else ''}<pitch><step>{step}</step>"
            f"{f'<alter>{alter}</alter>' if alter else ''}<octave>{octave}</octave></pitch>"
            f"<duration>{dur}</duration><voice>{voice or (staff - 1) * 4 + 1}</voice>"
            f"<type>{TYPES[dur]}</type><staff>{staff}</staff></note>")


def rest(dur: int, staff: int = 1, voice: int | None = None) -> str:
    return (f"<note><rest/><duration>{dur}</duration><voice>{voice or (staff - 1) * 4 + 1}</voice>"
            f"<type>{TYPES[dur]}</type><staff>{staff}</staff></note>")


def backup(dur: int) -> str:
    return f"<backup><duration>{dur}</duration></backup>"


def score(*measures: str, time: tuple[int, int] = (4, 4)) -> str:
    """divisions=4 (a sixteenth is 1); the time signature goes in measure 1, like HOMR's."""
    first = ("<attributes><divisions>4</divisions><key><fifths>0</fifths></key>"
             f"<time><beats>{time[0]}</beats><beat-type>{time[1]}</beat-type></time><staves>2</staves>"
             "<clef number=\"1\"><sign>G</sign><line>2</line></clef>"
             "<clef number=\"2\"><sign>G</sign><line>2</line></clef></attributes>")
    body = "".join(f'<measure number="{i}">{first if i == 1 else ""}{m}</measure>'
                   for i, m in enumerate(measures, 1))
    return f'<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0"><part id="P1">{body}</part></score-partwise>'


def write(tmp_path: Path, xml: str, name: str = "in.musicxml") -> Path:
    path = tmp_path / name
    path.write_text(xml)
    return path
