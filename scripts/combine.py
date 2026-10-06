"""Concatenate result scores, in order, into one full score.

Usage:  uv run python scripts/combine.py <title> <out.musicxml> <first.musicxml> <second.musicxml> ...

The first file gives the part list; every file must have the same number of parts and staves.
Measures are numbered from 1 again and each file starts a new system. At each join, attributes
that only repeat what is already in force (clefs, key, time signature, divisions, staves) are
dropped, so no courtesy clef or time signature appears there. The title becomes the work title,
and the source files are listed in <identification>.
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

SOURCES_FIELD = "concatenated-from"


def staves(part: ET.Element) -> int:
    return max((int(s.text) for s in part.iter("staves")), default=1)


def attribute_key(el: ET.Element) -> tuple[str, str]:
    return el.tag, el.get("number", "")


def canonical(el: ET.Element) -> str:
    return ET.tostring(el, encoding="unicode").replace(" ", "").replace("\n", "")


def set_title(root: ET.Element, title: str) -> None:
    work = root.find("work")
    if work is None:
        work = ET.Element("work")
        root.insert(0, work)
    (work.find("work-title") if work.find("work-title") is not None else ET.SubElement(work, "work-title")).text = title
    if (movement := root.find("movement-title")) is not None:
        movement.text = title


def concatenate(sources: list[Path], title: str, dst: Path) -> int:
    """Write the full score to dst and return its number of measures."""
    if not sources:
        raise ValueError("nothing to concatenate")
    trees = [ET.parse(s) for s in sources]
    first = trees[0].getroot()
    first_parts = first.findall("part")
    for src, tree in zip(sources[1:], trees[1:]):
        parts = tree.getroot().findall("part")
        if len(parts) != len(first_parts):
            raise ValueError(f"{src.name} has {len(parts)} part(s), {sources[0].name} has {len(first_parts)}")
        for a, b in zip(first_parts, parts):
            if staves(a) != staves(b):
                raise ValueError(f"{src.name} has {staves(b)} staves, {sources[0].name} has {staves(a)}")

    count = 0
    for index, part in enumerate(first_parts):
        in_force: dict[tuple[str, str], str] = {}
        measures: list[ET.Element] = []
        for file_no, tree in enumerate(trees):
            for measure_no, measure in enumerate(tree.getroot().findall("part")[index].findall("measure")):
                at_join = file_no > 0 and measure_no == 0
                for attrs in measure.findall("attributes"):
                    for el in list(attrs):
                        key, value = attribute_key(el), canonical(el)
                        if at_join and in_force.get(key) == value:
                            attrs.remove(el)
                        in_force[key] = value
                    if len(attrs) == 0:
                        measure.remove(attrs)
                if at_join and measure.find("print") is None:
                    new_system = ET.Element("print", {"new-system": "yes"})
                    measure.insert(0, new_system)
                measures.append(measure)
        for m in part.findall("measure"):
            part.remove(m)
        for number, m in enumerate(measures, 1):
            m.set("number", str(number))
            part.append(m)
        count = max(count, len(measures))

    set_title(first, title)
    identification = first.find("identification")
    if identification is None:
        identification = ET.Element("identification")
        later = [i for i, el in enumerate(first) if el.tag in ("defaults", "credit", "part-list", "part")]
        first.insert(later[0] if later else len(first), identification)
    for misc in identification.findall("miscellaneous"):
        identification.remove(misc)
    field = ET.SubElement(ET.SubElement(identification, "miscellaneous"), "miscellaneous-field", name=SOURCES_FIELD)
    field.text = ", ".join(s.name for s in sources)

    ET.indent(trees[0], space="  ")
    dst.parent.mkdir(parents=True, exist_ok=True)
    trees[0].write(dst, encoding="UTF-8", xml_declaration=True)
    return count


def sources_of(path: Path) -> list[str]:
    """The files a full score was concatenated from, as recorded in it."""
    field = ET.parse(path).getroot().find(f"identification/miscellaneous/miscellaneous-field[@name='{SOURCES_FIELD}']")
    return [s.strip() for s in field.text.split(",")] if field is not None and field.text else []


def retitle(path: Path, title: str) -> None:
    tree = ET.parse(path)
    set_title(tree.getroot(), title)
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("title")
    ap.add_argument("out")
    ap.add_argument("sources", nargs="+")
    args = ap.parse_args()
    n = concatenate([Path(s) for s in args.sources], args.title, Path(args.out))
    print(f"{args.out}: {n} measures from {len(args.sources)} files")


if __name__ == "__main__":
    main()
