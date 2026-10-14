"""Piano rolls on disk, and the HTTP API the editor page talks to.

A roll lives in rolls/<name>.roll.json (the source of truth) with its MIDI next to it, rolls/<name>.mid,
rewritten on every save. Each save raises the roll's revision; a save or edit that names the revision it
started from is refused when the roll moved on since (someone else saved), and the last 20 revisions
are kept in rolls/history/<name>/ so an edit can be undone with revert.

handle() serves the editor page (GET /roll?name=) and its API (/api/roll/...) for both ui.py and the MCP
server's HTTP transport, so the page works from either. Writes need a JSON body from this machine's own
pages: a Host on the allow-list and, if the browser sends one, a matching Origin.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import combine
import roll
import roll_midi
import roll_ops
import roll_xml
import run_batch
import ui
from roll import RollError
from samples import check_name, nfc

HISTORY = 20
EDITOR = Path(__file__).with_name("piano_roll.html")
LOSSY = ("A roll keeps notes only: dynamics, slurs, articulations, lyrics, grace notes, repeats and tuplets of the "
         "source are not in it.")
MAX_MIDI = 10 * 1024 * 1024


class Conflict(RollError):
    pass


class NotFound(RollError):
    pass


def rolls_dir() -> Path:
    return ui.ROOT / "rolls"


def roll_file(name: str) -> Path:
    return rolls_dir() / f"{name}.roll.json"


def names() -> list[str]:
    d = rolls_dir()
    return sorted(nfc(p.name.removesuffix(".roll.json")) for p in d.glob("*.roll.json")) if d.is_dir() else []


def rel(path: Path) -> str:
    return str(path.relative_to(ui.ROOT))


def load(name: str) -> dict:
    path = roll_file(nfc(name))
    if not path.is_file():
        raise NotFound(f"no piano roll called {name!r}; piano rolls: {', '.join(names()) or 'none yet'}")
    return roll.validate(json.loads(path.read_text(encoding="utf-8")))


def list_rolls() -> list[dict]:
    out = []
    for name in names():
        path = roll_file(name)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            out.append({"name": name, "title": doc.get("title", ""), "measures": doc.get("measures"),
                        "revision": doc.get("revision"), "parts": [p.get("name") for p in doc.get("parts", [])],
                        "notes": len(doc.get("notes", [])), "results": doc.get("results", []),
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
                        "roll": rel(path), "midi": rel(path.with_name(f"{name}.mid"))})
        except (OSError, ValueError):
            out.append({"name": name, "error": "unreadable"})
    return out


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save(name: str, doc: dict, base_revision: int | None = None, *, new: bool = False,
         replace: bool = False) -> tuple[dict, list[str]]:
    """Write the roll and its MIDI. new: refuse an existing name unless replace."""
    name = check_name(name)
    path = roll_file(name)
    current = None
    if path.is_file():
        if new and not replace:
            raise RollError(f"there is already a piano roll called {name}; pick another name or pass replace=true")
        current = json.loads(path.read_text(encoding="utf-8"))
        if base_revision is not None and current.get("revision") != base_revision:
            raise Conflict(f"{name} changed since revision {base_revision}: it is at revision "
                           f"{current.get('revision')}. Reload it, then make the change again.")
    doc = roll.validate(roll.copy_doc(doc))
    warnings = roll.normalize(doc)
    doc["revision"] = int(current.get("revision", 0)) + 1 if current else 1
    rolls_dir().mkdir(exist_ok=True)
    if current:
        history = rolls_dir() / "history" / name
        history.mkdir(parents=True, exist_ok=True)
        (history / f"{current.get('revision', 0)}.roll.json").write_text(json.dumps(current, ensure_ascii=False))
        kept = sorted(history.glob("*.roll.json"), key=lambda p: int(p.name.split(".")[0]))
        for old in kept[:-HISTORY]:
            old.unlink()
    _write(path, roll.dump(doc).encode("utf-8"))
    _write(path.with_name(f"{name}.mid"), roll_midi.write_midi(doc))
    return doc, warnings


def describe(name: str, doc: dict) -> dict:
    return {"name": name, **roll.summary(doc), "files": {"roll": rel(roll_file(name)),
                                                         "midi": rel(rolls_dir() / f"{name}.mid")}}


def create(name: str, source: str = "new", *, source_name: str | None = None, midi: bytes | None = None,
           title: str | None = None, time_signature: str = "4/4", key: str = "C major", tempo: float = 120,
           measures: int = 4, preset: str = "piano", grid: dict | None = None, split: str | None = None,
           replace: bool = False) -> tuple[dict, dict]:
    """A new roll: from scratch, or from a result, full score or MIDI file. Returns (doc, info)."""
    name = check_name(name)
    if roll_file(name).is_file() and not replace:
        raise RollError(f"there is already a piano roll called {name}; open it, pick another name or pass "
                        "replace=true")
    info: dict = {}
    if source == "new":
        doc = roll.new_doc(title=title if title is not None else name, time=time_signature, key=key, tempo=tempo,
                           measures=measures, preset=preset, grid=grid)
    elif source in ("result", "score"):
        if not source_name:
            raise RollError(f"say which {source} to open (source_name)")
        ui.sync()
        xml = ui.source_xml(source, source_name)
        doc, info = roll_xml.read_musicxml(run_batch.load_musicxml(xml))
        doc["source"] = {"kind": source, "name": nfc(source_name), "snapped": info["snapped"],
                         "dropped": info["dropped"]}
        if grid:
            doc["grid"].update(grid)
        info["note"] = LOSSY
    elif source == "midi":
        if not midi:
            raise RollError("give the MIDI file")
        doc, info = roll_midi.read_midi(midi, grid=(grid or {}).get("min"), split=split, title=title or name)
        doc["source"] = {"kind": "midi", "name": source_name or "", "snapped": info["snapped"], "dropped": info["dropped"]}
    else:
        raise RollError(f"source is new, result, score or midi, not {source!r}")
    if title is not None:
        doc["title"] = title
    doc, warnings = save(name, doc, new=True, replace=replace)
    if warnings:
        info["warnings"] = info.get("warnings", []) + warnings
    return doc, info


def read_midi_source(*, base64_data: str | None = None, path: str | None = None) -> tuple[bytes, str]:
    """MIDI bytes from base64, or from a .mid/.midi file inside the project (e.g. exports/x.mid)."""
    if (base64_data is None) == (path is None):
        raise RollError("give the MIDI as midi_base64 or as midi_path")
    if base64_data is not None:
        data = base64.b64decode(base64_data, validate=True)
        name = ""
    else:
        root = ui.ROOT.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() not in (".mid", ".midi") or not target.is_file():
            raise RollError(f"midi_path must be a .mid file inside the project, e.g. exports/<name>.mid; not {path!r}")
        data, name = target.read_bytes(), target.stem
    if len(data) > MAX_MIDI:
        raise RollError("the MIDI file is larger than 10 MB")
    return data, name


def _preview(doc: dict, measures: list[int], limit: int = 8) -> list[dict]:
    return roll.streams(doc, measures[:limit])


def ranges(measures: list[int]) -> str:
    out, run = [], []
    for m in measures:
        if run and m == run[-1] + 1:
            run.append(m)
        else:
            if run:
                out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [m]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def edit(name: str, operations: list[dict], base_revision: int | None = None, dry_run: bool = False) -> dict:
    doc = load(name)
    if base_revision is not None and doc["revision"] != base_revision:
        raise Conflict(f"{name} is at revision {doc['revision']}, not {base_revision}; read it again first")
    new, report = roll_ops.apply_operations(doc, operations)
    if not dry_run:
        new, warnings = save(name, new, base_revision=doc["revision"])
        report["warnings"] += warnings
    changed = report["changed_measures"]
    return {"name": nfc(name), "revision": new["revision"] if not dry_run else doc["revision"], "dry_run": dry_run,
            "applied": report["applied"], "warnings": report["warnings"], "changed_measures": ranges(changed),
            "measures": new["measures"], "preview": _preview(new, changed)}


def transform(doc: dict, operations: list[dict]) -> dict:
    """Apply operations to a roll that isn't saved (the editor's), writing nothing."""
    new, report = roll_ops.apply_operations(roll.validate(roll.copy_doc(doc)), operations)
    return {"roll": new, "report": {**report, "changed_measures": ranges(report["changed_measures"])}}


def revert(name: str, revision: int) -> dict:
    doc = load(name)
    old = rolls_dir() / "history" / nfc(name) / f"{int(revision)}.roll.json"
    if not old.is_file():
        kept = sorted(int(p.name.split(".")[0]) for p in old.parent.glob("*.roll.json")) if old.parent.is_dir() else []
        raise RollError(f"revision {revision} of {name} isn't kept; kept: {', '.join(map(str, kept)) or 'none'}")
    back = json.loads(old.read_text(encoding="utf-8"))
    saved, _ = save(name, back, base_revision=doc["revision"])
    return {"name": nfc(name), "reverted_to": int(revision), "revision": saved["revision"]}


def default_result(name: str, doc: dict) -> str:
    taken = {r["output_name"] for r in ui.read_results()} | {nfc(Path(n).stem) for n in ui.list_inputs()}
    for candidate in [name, *(f"{name}-roll" + ("" if i == 1 else f"-{i}") for i in range(1, 100))]:
        if candidate not in taken or candidate in doc.get("results", []):
            return candidate
    raise RollError("give the result a name")


def to_result(name: str, result: str | None = None, replace: bool = False) -> dict:
    """Write the roll as MusicXML and add it as a result, ready to go into a full score."""
    doc = load(name)
    result = check_name(result) if result else default_result(nfc(name), doc)
    rows = {r["output_name"]: r for r in ui.read_results()}
    own = result in doc.get("results", []) and rows.get(result, {}).get("status") == "imported"
    xml = roll_xml.write_musicxml(doc)
    row = ui.import_result(result, xml, replace=replace or own)
    warnings = []
    mine = [len(doc["groups"]), sorted(len([p for p in doc["parts"] if p["group"] == g["id"]]) for g in doc["groups"])]
    for r in ui.read_results():
        if r["include"] and r["output_name"] != result and r["output"]:
            try:
                parts = ET.parse(ui.ROOT / r["output"]).getroot().findall("part")
            except (OSError, ET.ParseError):
                continue
            theirs = [len(parts), sorted(combine.staves(p) for p in parts)]
            if theirs != mine:
                warnings.append(f"{r['output_name']} has {theirs[0]} part(s) with {theirs[1]} staves and this has "
                                f"{mine[0]} with {mine[1]}: they can't be combined into one full score")
                break
    if result not in doc.get("results", []):
        doc["results"] = [*doc.get("results", []), result]
        save(name, doc)
    return {"result": nfc(row["output_name"]), "status": row["status"], "opens_in_musescore": row["musescore"],
            "musicxml": row["output"], "warnings": warnings, "note": LOSSY}


def page_html(boot: dict) -> str:
    slot = json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")  # can't close the <script> it sits in
    return EDITOR.read_text(encoding="utf-8").replace("{{BOOT}}", slot)


# --- HTTP ------------------------------------------------------------------------------------------------------
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _host_ok(host: str, hosts: tuple[str, ...]) -> bool:
    bare = re.sub(r":\d+$", "", host or "")
    return bare in LOCAL_HOSTS or bare in hosts


def guard(headers: dict, hosts: tuple[str, ...]) -> str | None:
    """Why a write request must be refused, or None. Only pages served from this machine may write."""
    h = {k.lower(): v for k, v in headers.items()}
    if not h.get("content-type", "").startswith("application/json"):
        return "send JSON (Content-Type: application/json)"
    if not _host_ok(h.get("host", ""), hosts):
        return "unknown host"
    origin = h.get("origin")
    if origin is not None and urlparse(origin).netloc != h.get("host", ""):
        return "cross-site requests are not accepted"
    return None


def handle(method: str, path: str, query: dict[str, list[str]], headers: dict, body: bytes,
           hosts: tuple[str, ...] = ()) -> tuple[int, str, bytes]:
    """(status, content type, body) for a request to /roll or /api/roll/<action>; path is without any token
    prefix."""
    js = "application/json"
    q = {k: v[0] for k, v in query.items() if v}

    def ok(data) -> tuple[int, str, bytes]:
        return 200, js, json.dumps(data, ensure_ascii=False).encode("utf-8")

    try:
        if method == "GET" and path == "/roll":
            name = q.get("name")
            boot = {"mode": "page", "name": None, "roll": None}
            if name:
                boot.update(name=nfc(name), roll=load(name))
            return 200, "text/html; charset=utf-8", page_html(boot).encode("utf-8")
        if not path.startswith("/api/roll/"):
            return 404, "text/plain; charset=utf-8", b"not found"
        action = path.removeprefix("/api/roll/")
        if method == "GET":
            if action == "list":
                return ok({"rolls": list_rolls(), "results": [r["output_name"] for r in ui.read_results() if r["output"]],
                           "scores": [s["name"] for s in ui.read_scores()]})
            if action == "load":
                doc = load(q.get("name", ""))
                since = q.get("if_newer_than")
                if since is not None and since.isdigit() and doc["revision"] <= int(since):
                    return ok({"name": nfc(q["name"]), "unchanged": True, "revision": doc["revision"]})
                return ok({"name": nfc(q["name"]), "roll": doc})
            return 404, "text/plain; charset=utf-8", b"not found"
        if method != "POST":
            return 405, "text/plain; charset=utf-8", b"method not allowed"
        refused = guard(headers, hosts)
        if refused:
            return 403, "text/plain; charset=utf-8", refused.encode()
        b = json.loads(body or b"{}")
        if not isinstance(b, dict):
            raise RollError("send a JSON object")
        if action == "create":
            midi = None
            if b.get("source") == "midi":
                midi, _ = read_midi_source(base64_data=b.get("midi_base64"), path=b.get("midi_path"))
            doc, info = create(b["name"], b.get("source", "new"), source_name=b.get("source_name"), midi=midi,
                               title=b.get("title"), time_signature=b.get("time", "4/4"), key=b.get("key", "C major"),
                               tempo=float(b.get("tempo", 120)), measures=int(b.get("measures", 4)),
                               preset=b.get("preset", "piano"), grid=b.get("grid"), split=b.get("split"),
                               replace=bool(b.get("replace")))
            return ok({"name": check_name(b["name"]), "roll": doc, "info": info})
        if action == "save":
            doc, warnings = save(b["name"], b["roll"], b.get("base_revision"), new=bool(b.get("new")),
                                 replace=bool(b.get("replace")))
            return ok({"name": check_name(b["name"]), "roll": doc, "revision": doc["revision"], "warnings": warnings})
        if action == "transform":
            return ok(transform(b["roll"], b["operations"]))
        if action == "edit":
            return ok(edit(b["name"], b["operations"], b.get("base_revision"), bool(b.get("dry_run"))))
        if action == "to-result":
            return ok(to_result(b["name"], b.get("result"), bool(b.get("replace"))))
        if action == "revert":
            return ok(revert(b["name"], int(b["revision"])))
        return 404, "text/plain; charset=utf-8", b"not found"
    except Conflict as e:
        return 409, "text/plain; charset=utf-8", str(e).encode()
    except NotFound as e:
        return 404, "text/plain; charset=utf-8", str(e).encode()
    except (ValueError, KeyError, TypeError, OSError) as e:
        msg = f"missing {e}" if isinstance(e, KeyError) else str(e)
        return 400, "text/plain; charset=utf-8", msg.encode()
