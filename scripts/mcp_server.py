"""MCP server: let an agent (Claude Code, Claude Desktop, ChatGPT, Codex, ...) work the pipeline.

The tools are thin wrappers over the functions behind the browser UI (ui.py), so an agent can do what the
UI does: add scores, transcribe them with HOMR, read the music, put the results in order, combine them into
a full score, export PDF or MIDI, and open them in MuseScore Studio for editing.

Usage:  uv run python scripts/mcp_server.py                      stdio (Claude Code, Claude Desktop, Codex CLI)
        uv run python scripts/mcp_server.py --transport streamable-http [--port 8790]
                                                                 HTTP (ChatGPT, claude.ai; behind a tunnel)
        uv run python scripts/mcp_server.py --list-tools

HTTP settings, from the environment:
  YAMBA_MCP_ALLOWED_HOSTS  public host names to accept besides localhost, comma-separated (the tunnel's)
  YAMBA_MCP_TOKEN          serve everything under /<token>/ (/<token>/mcp, /<token>/files/...), so a public
                           URL can't be guessed; ChatGPT connectors offer no auth besides OAuth
  YAMBA_MCP_PUBLIC_URL     the public base URL, e.g. https://abc.trycloudflare.com; tools then return links

See docs/MCP.md for connecting each harness.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import functools
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import FileResponse, PlainTextResponse

import run_batch
import score_check
import ui
from find_musicxml import Search
from samples import nfc

SERVER_NAME = "yamba-music"
PORT = 8790  # clear of the UI's 8765 and the ports it is often run on
MAX_BODY = 64 * 1024 * 1024  # base64 uploads of a scanned PDF
MAX_DOWNLOAD = 50 * 1024 * 1024
ERRORS = (ValueError, KeyError, OSError, ET.ParseError, subprocess.SubprocessError, binascii.Error)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

Kind = Annotated[Literal["result", "score"], Field(description="'result' (one transcribed/imported piece) or "
                                                               "'score' (a full score combined from results)")]

INSTRUCTIONS = """\
Yamba Music: a sheet-music pipeline running on the user's own Mac.

How it fits together:
- inputs: score images (.png/.jpg), PDFs, or MusicXML (.musicxml/.mxl).
- results: HOMR (optical music recognition) turns each image or PDF page into a MusicXML result. An
  imported MusicXML file becomes a result straight away.
- full score: results, in an order the user chooses, concatenated into one score.
- exports: PDF or MIDI of a result or a full score.

Working with it:
- Start with get_library. Use get_music to read the notes, and search/fetch to look things up by text.
- Transcription is slow (minutes per page). transcribe starts a background job; then call get_job with
  wait_seconds until it is done. The computer-wide file search also runs in the background.
- You can write MusicXML yourself (score-partwise) and add it with import_musicxml.
- Names may contain letters, digits, spaces, '.', '-' and '_', up to 100 characters.

Results and links:
- Paths in results are on the user's Mac.
- When a result has a 'url', give it to the user to download the file.
- open_in_musescore opens MuseScore Studio on the user's Mac. What they save there comes back automatically.

Rename or delete only when the user asks. Report failed transcriptions as they are; don't hide them.
"""


@dataclass
class Job:
    id: str
    input: str | None
    process: subprocess.Popen
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    lines: list[str] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)


class Jobs:
    """run_batch.py in the background: HOMR takes minutes per page, longer than a tool call should block."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def start(self, only: str | None) -> Job:
        with self._lock:
            running = next((j for j in self._jobs.values() if not j.done.is_set()), None)
            if running:
                raise ValueError(f"transcription job {running.id} is still running; wait for it with get_job")
            p = subprocess.Popen(ui.batch_command(only), cwd=ui.ROOT, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            job = Job(uuid.uuid4().hex[:8], only, p)
            self._jobs[job.id] = job
        threading.Thread(target=self._follow, args=(job,), daemon=True).start()
        return job

    @staticmethod
    def _follow(job: Job) -> None:
        for line in job.process.stdout:
            job.lines.append(line.rstrip())
        job.process.wait()
        job.finished = time.time()
        job.done.set()

    def get(self, job_id: str | None) -> Job:
        with self._lock:
            job = self._jobs.get(job_id) if job_id else next(reversed(self._jobs.values()), None)
        if job is None:
            raise ValueError(f"no transcription job {job_id}" if job_id else "no transcription has been started")
        return job

    def started(self) -> bool:
        with self._lock:
            return bool(self._jobs)

    def running(self) -> str | None:
        with self._lock:
            return next((j.id for j in self._jobs.values() if not j.done.is_set()), None)


def result_row(r: dict) -> dict:
    return {"name": r["output_name"], "sample": r["stem"], "source": r["source"], "status": r["status"],
            "has_musicxml": bool(r["output"]), "in_full_score": r["include"], "opens_in_musescore": r["musescore"],
            "seconds": r["seconds"], "postprocess_fixes": r["fixes"]}


def measure_range(spec: str | None, count: int) -> range:
    """'5' or '3-8' (1-based, inclusive) or 'all'; the first 8 measures when not given."""
    if not spec:
        return range(min(8, count))
    if spec.strip().lower() == "all":
        return range(count)
    first, _, last = spec.replace(" ", "").partition("-")
    if not first.isdigit() or (last and not last.isdigit()):
        raise ValueError(f"measures must look like '5', '3-8' or 'all', not {spec!r}")
    lo, hi = int(first), int(last or first)
    if not 1 <= lo <= hi:
        raise ValueError(f"bad measure range {spec!r}")
    return range(lo - 1, min(hi, count))


def music_summary(xml: Path, measures: str | None) -> dict:
    root = ET.parse(xml).getroot()
    score = score_check.read_musicxml(xml)
    events = [e for m in score.measures for evs in m.staves.values() for e in evs]
    pitches = sorted({p for e in events for p in e.pitches}, key=score_check.pitch_key)
    times, last = [], None
    for i, m in enumerate(score.measures, 1):
        if m.time != last:
            times.append({"from_measure": i, "time": f"{m.time[0]}/{m.time[1]}"})
            last = m.time
    shown = measure_range(measures, len(score.measures))
    return {
        "title": root.findtext("work/work-title") or root.findtext("movement-title") or "",
        "parts": [p.findtext("part-name") or p.get("id") for p in root.findall("part-list/score-part")],
        "key_fifths": next((int(k) for k in (el.text for el in root.iter("fifths")) if k), None),
        "measures": len(score.measures),
        "time_signatures": times,
        "clefs": {str(k): v for k, v in sorted(score.clefs.items())},
        "notes": sum(len(e.pitches) for e in events),
        "rests": sum(e.is_rest for e in events),
        "lowest": pitches[0] if pitches else None,
        "highest": pitches[-1] if pitches else None,
        "first_part_only": len(root.findall("part")) > 1,
        "shown": f"{shown.start + 1}-{shown.stop}" if shown else "none",
        "bars": [{"measure": i + 1, "number": score.measures[i].number,
                  "time": f"{score.measures[i].time[0]}/{score.measures[i].time[1]}",
                  "staves": {str(s): score_check.to_tokens(evs) for s, evs in sorted(score.measures[i].staves.items())}}
                 for i in shown],
        "token_format": "pitch:duration, chords as C4+E4:4, r = rest; durations 1 whole, 2 half, 4 quarter, "
                        "8 eighth, 16 sixteenth, a trailing '.' is dotted; written pitch (treble-8va clefs undone)",
    }


def build_server(*, token: str = "", public_url: str | None = None) -> MCPServer:
    server = MCPServer(SERVER_NAME, title="Yamba Music", version="0.1.0", instructions=INSTRUCTIONS)
    jobs = Jobs()
    prefix = f"/{token}" if token else ""
    base = public_url.rstrip("/") + prefix if public_url else None

    def file_ref(rel: str) -> dict:
        """A file in the project, as its path (on the user's Mac) and, over HTTP, a download link."""
        ref = {"path": rel, "absolute_path": str(ui.ROOT / rel)}
        if base:
            ref["url"] = f"{base}/files/{quote(rel)}"
        return ref

    def tool(annotations: ToolAnnotations):
        """Register a tool. Expected failures (a bad name, a missing file, MuseScore refusing) come back as
        tool errors the model can read and act on, not as an opaque crash."""
        def register(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except ToolError:
                    raise
                except ERRORS as e:
                    raise ToolError(f"missing {e}" if isinstance(e, KeyError) else str(e)) from e
            server.tool(annotations=annotations)(wrapper)
            return fn
        return register

    def find_result(name: str) -> dict:
        rows = ui.read_results()
        row = next((r for r in rows if r["output_name"] == nfc(name)), None)
        if row is None:
            raise ValueError(f"no result called {name!r}; results: {', '.join(r['output_name'] for r in rows)}")
        return row

    def find_score(name: str) -> dict:
        scores = ui.read_scores()
        score = next((s for s in scores if s["name"] == nfc(name)), None)
        if score is None:
            raise ValueError(f"no full score called {name!r}; full scores: {', '.join(s['name'] for s in scores)}")
        return score

    def find(kind: str, name: str) -> dict:
        return find_result(name) if kind == "result" else find_score(name)

    def job_report(job: Job) -> dict:
        done = job.done.is_set()
        out = {"job_id": job.id, "input": job.input or "all inputs",
               "state": "running" if not done else "done" if job.process.returncode == 0 else "failed",
               "elapsed_seconds": round((job.finished or time.time()) - job.started, 1),
               "returncode": job.process.returncode if done else None, "log_tail": job.lines[-30:]}
        if done:
            out["results"] = [result_row(r) for r in ui.read_results()
                              if job.input is None or nfc(r["source"]) == nfc(job.input)]
        return out

    # --- reading ---------------------------------------------------------------------------------------------
    @tool(READ)
    def get_library() -> dict[str, Any]:
        """Everything in the pipeline: inputs, results in their full-score order (with status and whether each
        goes into the full score), the full-score name, the full scores made so far, exports, and which tools
        (HOMR, MuseScore, pdftoppm) are installed. Call this first."""
        state = ui.read_state()
        exports = ui.ROOT / "exports"
        return {
            "inputs": ui.list_inputs(),
            "results": [result_row(r) for r in ui.read_results()],
            "full_score_name": state["name"],
            "scores": [{"name": s["name"], "sources": s["sources"], "has_pdf": bool(s["pdf"]),
                        "has_musescore_file": s["mscz"]} for s in ui.read_scores()],
            "exports": sorted(p.name for p in exports.iterdir() if p.is_file()) if exports.is_dir() else [],
            "tools": {"homr": Path(run_batch.homr_bin()).exists() or bool(shutil.which("homr")),
                      "musescore": bool(ui.mscore_bin()), "pdftoppm": bool(shutil.which("pdftoppm"))},
            "running_job": jobs.running(),
        }

    @tool(READ)
    def get_music(
        kind: Kind,
        name: Annotated[str, Field(description="the result or full score name")],
        measures: Annotated[str | None, Field(description="'5', '3-8' or 'all'; the first 8 when left out")] = None,
        include_musicxml: Annotated[bool, Field(description="also return the raw MusicXML (capped)")] = False,
        max_chars: Annotated[int, Field(description="cap on the raw MusicXML returned")] = 20000,
    ) -> dict[str, Any]:
        """Read the music in a result or full score: title, parts, key, time signatures, clefs, range and
        measure count, plus the notes of the chosen measures as compact tokens per staff (e.g. 'C4:4 E4+G4:8 r:8').
        It's read from the score's first part."""
        if kind == "result":
            row = find_result(name)
            meta, files = {"status": row["status"], "source": row["source"], "postprocess_fixes": row["fixes"]}, [
                row["output"], row["render"]]
        else:
            s = find_score(name)
            meta, files = {"sources": s["sources"]}, [s["xml"], s["pdf"]]
        xml = ui.source_xml(kind, name)
        meta["files"] = [file_ref(f) for f in files if f]
        out = {"kind": kind, "name": nfc(name), **meta, **music_summary(xml, measures)}
        if include_musicxml:
            text = xml.read_text(encoding="utf-8")
            out["musicxml"], out["musicxml_truncated"] = text[:max_chars], len(text) > max_chars
        return out

    @tool(READ)
    def search(query: Annotated[str, Field(description="words to look for; empty lists everything")]) -> dict[str, Any]:
        """Search the results and full scores by name and source files; the best matches (most words found)
        come first. Returns ids for fetch."""
        words = nfc(query).lower().split()
        found = [(f"{r['output_name']} {r['source']} result", {
                     "id": f"result:{r['output_name']}", "title": f"{r['output_name']} (result, {r['status']})",
                     "url": file_ref(r["output"]).get("url", str(ui.ROOT / r["output"]))})
                 for r in ui.read_results() if r["output"]]
        found += [(f"{s['name']} {' '.join(s['sources'])} full score", {
                      "id": f"score:{s['name']}", "title": f"{s['name']} (full score of {len(s['sources'])} results)",
                      "url": file_ref(s["xml"]).get("url", str(ui.ROOT / s["xml"]))})
                  for s in ui.read_scores()]
        ranked = [(sum(w in text.lower() for w in words), hit) for text, hit in found]
        return {"results": [hit for n, hit in sorted(ranked, key=lambda x: -x[0]) if n or not words]}

    @tool(READ)
    def fetch(id: Annotated[str, Field(description="an id from search, e.g. 'result:merkurius-m01-10'")]) -> dict[str, Any]:
        """A result or full score in full: what it is and every measure's notes, as text."""
        kind, _, name = id.partition(":")
        if kind not in ("result", "score") or not name:
            raise ValueError(f"ids look like 'result:<name>' or 'score:<name>', not {id!r}")
        music = get_music(kind, name, measures="all")
        lines = [f"{music['title'] or name} ({kind}); {music['measures']} measures, parts: {', '.join(music['parts'])}",
                 "time: " + ", ".join(f"{t['time']} from m{t['from_measure']}" for t in music["time_signatures"])
                 + f"; key fifths: {music['key_fifths']}; clefs: {music['clefs']}; range {music['lowest']}-{music['highest']}",
                 f"tokens: {music['token_format']}"]
        lines += [f"m{b['measure']} {b['time']}: " + " | ".join(f"staff {s}: {t}" for s, t in b["staves"].items())
                  for b in music["bars"]]
        file = music["files"][0]
        return {"id": id, "title": f"{name} ({kind})", "text": "\n".join(lines),
                "url": file.get("url", file["absolute_path"]),
                "metadata": {k: music[k] for k in ("measures", "parts", "status", "sources") if k in music}}

    # --- adding and transcribing -----------------------------------------------------------------------------
    @tool(WRITE)
    def import_musicxml(
        name: Annotated[str, Field(description="result name, e.g. 'waltz-in-c' (.musicxml is added)")],
        musicxml: Annotated[str, Field(description="a complete score-partwise MusicXML document")],
        replace: Annotated[bool, Field(description="overwrite a result of the same name")] = False,
    ) -> dict[str, Any]:
        """Add MusicXML as a new result straight away, with no HOMR: a score you wrote, corrected or found.
        MuseScore checks that it opens."""
        filename = name if Path(name).suffix.lower() in run_batch.MUSICXML_EXTS else f"{name}.musicxml"
        taken = {r["output_name"] for r in ui.read_results()} | {nfc(Path(n).stem) for n in ui.list_inputs()}
        if not replace and nfc(Path(filename).stem) in taken:
            raise ValueError(f"{Path(filename).stem} already exists; pick another name or pass replace=true")
        row = ui.import_upload(filename, musicxml.encode("utf-8"))
        return {"result": nfc(row["output_name"]), "opens_in_musescore": row["musescore"],
                "file": file_ref(row["output"])}

    @tool(WRITE)
    def add_input(
        name: Annotated[str, Field(description="file name with extension: .png .jpg .jpeg .pdf .musicxml .mxl")],
        content_base64: Annotated[str | None, Field(description="the file's bytes, base64-encoded")] = None,
        url: Annotated[str | None, Field(description="or an http(s) URL to download it from")] = None,
    ) -> dict[str, Any]:
        """Add a score file to inputs/. Images and PDFs then need transcribe(input_name=...); MusicXML becomes a
        result straight away."""
        if (content_base64 is None) == (url is None):
            raise ValueError("give either content_base64 or url")
        if url is not None:
            if urlparse(url).scheme not in ("http", "https"):
                raise ValueError("only http(s) URLs can be downloaded")
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "yamba-music"}),
                                        timeout=60) as r:
                data = r.read(MAX_DOWNLOAD + 1)
            if len(data) > MAX_DOWNLOAD:
                raise ValueError(f"{url} is larger than {MAX_DOWNLOAD // 2**20} MB")
        else:
            data = base64.b64decode(content_base64, validate=True)
        row = ui.save_input(name, data)
        if row:
            return {"input": name, "result": row["output_name"], "status": row["status"], "file": file_ref(row["output"])}
        return {"input": name, "bytes": len(data), "next": f"call transcribe(input_name={name!r})"}

    @tool(DESTRUCTIVE)
    def delete_input(name: Annotated[str, Field(description="file name in inputs/, with extension")]) -> dict[str, Any]:
        """Delete a file from inputs/. Its results stay until the next transcription of all inputs."""
        ui.delete_input(name)
        return {"deleted": name}

    @tool(WRITE)
    def transcribe(
        input_name: Annotated[str | None, Field(description="one file in inputs/; every input when left out "
                                                            "(re-runs HOMR on all of them, slow)")] = None,
    ) -> dict[str, Any]:
        """Start transcribing with HOMR in the background and return a job id; poll get_job(job_id,
        wait_seconds=30) until state is done or failed. Takes roughly 1-3 minutes per page (more on the first
        run, which downloads HOMR's models). Edits saved in MuseScore are kept."""
        if input_name is not None and nfc(input_name) not in {nfc(n) for n in ui.list_inputs()}:
            raise ValueError(f"no input called {input_name!r}; inputs: {', '.join(ui.list_inputs())}")
        return job_report(jobs.start(input_name))

    @tool(READ)
    def get_job(
        job_id: Annotated[str | None, Field(description="from transcribe; the latest job when left out")] = None,
        wait_seconds: Annotated[int, Field(description="wait up to this long (max 50) for the job to finish")] = 0,
    ) -> dict[str, Any]:
        """A transcription job's state (running, done, failed), its log so far and, once finished, its results."""
        if job_id is None and not jobs.started():
            return {"state": "none", "note": "no transcription has been started; use transcribe"}
        job = jobs.get(job_id)
        job.done.wait(max(0, min(wait_seconds, 50)))
        return job_report(job)

    # --- finding MusicXML on the computer --------------------------------------------------------------------
    @tool(WRITE)
    def start_computer_search(
        folders: Annotated[list[str] | None, Field(description="only these folders; the whole computer (Spotlight "
                                                               "then every drive) when left out")] = None,
    ) -> dict[str, Any]:
        """Look for MusicXML files (.musicxml, .mxl, MusicXML .xml) on this computer in the background. Then
        call get_computer_search; a whole-computer search can take several minutes."""
        roots = [Path(f).expanduser() for f in folders or []]
        missing = [str(r) for r in roots if not r.is_dir()]
        if missing:
            raise ValueError(f"not a folder: {', '.join(missing)}")
        ui.SEARCH.stop()
        # add_found only takes files this search found, so a new search replaces the old one
        ui.SEARCH = Search(roots=lambda: roots, spotlight=lambda: []) if roots else Search()
        ui.SEARCH.start()
        return get_computer_search(limit=0)

    @tool(READ)
    def get_computer_search(
        query: Annotated[str | None, Field(description="only paths containing all these words")] = None,
        offset: Annotated[int, Field(description="skip this many matching files")] = 0,
        limit: Annotated[int, Field(description="return at most this many")] = 50,
    ) -> dict[str, Any]:
        """Progress of the computer search (phase: spotlight, walk, done, stopped) and the MusicXML files found.
        Add the ones the user wants with add_found_files."""
        snap = ui.SEARCH.snapshot()
        words = nfc(query or "").lower().split()
        rows = [r for r in snap["rows"] if all(w in r["path"].lower() for w in words)]
        page = rows[offset:offset + limit]
        return {"phase": snap["phase"], "searching": snap["current"], "folders": snap["dirs"],
                "unreadable_folders": snap["unreadable"], "elapsed_seconds": snap["elapsed"],
                "found": snap["total"], "matching": len(rows),
                "files": [{"path": r["path"], "size": r["size"],
                           "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["modified"]))} for r in page],
                "next_offset": offset + len(page) if offset + len(page) < len(rows) else None}

    @tool(WRITE)
    def stop_computer_search() -> dict[str, Any]:
        """Stop the computer search; what it found so far stays available."""
        ui.SEARCH.stop()
        return get_computer_search(limit=0)

    @tool(WRITE)
    def add_found_files(
        paths: Annotated[list[str], Field(description="paths exactly as get_computer_search returned them")],
    ) -> dict[str, Any]:
        """Copy files found by the computer search into inputs/ and make each a result. The originals are left
        alone; a taken name gets -2, -3, and a file already added isn't added twice."""
        return ui.add_found(paths)

    # --- the full score --------------------------------------------------------------------------------------
    @tool(WRITE)
    def arrange_full_score(
        order: Annotated[list[str] | None, Field(description="result names in full-score order; results not "
                                                             "listed follow in their current order")] = None,
        exclude: Annotated[list[str] | None, Field(description="result names to leave out (replaces the list)")] = None,
        name: Annotated[str | None, Field(description="the full score's name")] = None,
    ) -> dict[str, Any]:
        """Set the order of the results, which go into the full score, and its name (as in the UI). Returns
        the results that will be combined, in order."""
        known = {r["output_name"] for r in ui.read_results()}
        unknown = [n for n in (order or []) + (exclude or []) if nfc(n) not in known]
        if unknown:
            raise ValueError(f"no results called {', '.join(unknown)}; results: {', '.join(sorted(known))}")
        if name:
            ui.check_name(name)
        if order is not None:
            first = [nfc(n) for n in order]
            order = first + [r["output_name"] for r in ui.read_results() if r["output_name"] not in first]
        state = ui.set_order(order, [nfc(n) for n in exclude] if exclude is not None else None, name)
        return {"name": state["name"], "will_combine": [r["output_name"] for r in ui.read_results() if r["include"]],
                "left_out": [r["output_name"] for r in ui.read_results() if not r["include"]]}

    @tool(WRITE)
    def combine_score(
        name: Annotated[str | None, Field(description="full score name; the saved full-score name when left out")] = None,
        results: Annotated[list[str] | None, Field(description="result names in order; the results set to go into "
                                                               "the full score, in their order, when left out")] = None,
    ) -> dict[str, Any]:
        """Concatenate results into a full score (scores/<name>.musicxml) and render its PDF with MuseScore.
        Replaces a full score of the same name."""
        name = name or ui.read_state()["name"]
        if not name:
            raise ValueError("give the full score a name")
        results = results or [r["output_name"] for r in ui.read_results() if r["include"]]
        if not results:
            raise ValueError("no results to combine")
        out = ui.combine(name, results)
        return {"name": out["name"], "measures": out["measures"], "results": results,
                "opens_in_musescore": out["musescore"], "musicxml": file_ref(out["xml"]),
                "pdf": file_ref(out["pdf"]) if out["pdf"] else None}

    @tool(WRITE)
    def rename(
        kind: Kind,
        old: Annotated[str, Field(description="for a result, its sample name (its input file's name without the "
                                              "extension; a PDF's pages -pNN are renamed with it)")],
        new: Annotated[str, Field(description="the new name")],
    ) -> dict[str, Any]:
        """Rename a result (its input, outputs, logs and renders) or a full score. The full-score order keeps it."""
        if kind == "result":
            return {"renamed": ui.rename_result(old, new)}
        return {"renamed": {nfc(old): ui.rename_full_score(old, new)}}

    # --- exporting and MuseScore -----------------------------------------------------------------------------
    @tool(WRITE)
    def export(
        kind: Kind,
        name: Annotated[str, Field(description="the result or full score name")],
        format: Annotated[Literal["pdf", "midi"], Field(description="pdf (sheet music) or midi (to play)")],
    ) -> dict[str, Any]:
        """Export a result or full score with MuseScore to exports/<name>.pdf or .mid. Edits saved in MuseScore
        are brought in first."""
        find(kind, name)
        return {"file": file_ref(ui.export_file(kind, name, format)["file"])}

    @tool(WRITE)
    def open_in_musescore(
        kind: Kind,
        name: Annotated[str, Field(description="the result or full score name")],
    ) -> dict[str, Any]:
        """Open a result or full score in MuseScore Studio on the user's Mac, for them to edit by hand. When they
        save, the edit comes back into the pipeline (or call sync_musescore_edits)."""
        find(kind, name)
        return {"opened": file_ref(ui.open_file(kind, name)["mscz"])}

    @tool(WRITE)
    def sync_musescore_edits() -> dict[str, Any]:
        """Bring back what the user saved in MuseScore Studio since the last sync. Returns the names updated."""
        return {"synced": ui.sync()}

    # --- HTTP only -------------------------------------------------------------------------------------------
    @server.custom_route(f"{prefix}/files/{{path:path}}", methods=["GET"])
    async def files(request):
        """Exports, scores, results and renders, for the links tools return; the same folders as the UI serves."""
        root = ui.ROOT.resolve()
        target = (root / request.path_params["path"]).resolve()
        if any(target.is_relative_to(root / d) for d in ui.SERVABLE_DIRS) and target.is_file():
            return FileResponse(target, media_type=ui.CONTENT_TYPES.get(target.suffix.lower(),
                                                                         "application/octet-stream"))
        return PlainTextResponse("not found", status_code=404)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request):
        return PlainTextResponse(f"{SERVER_NAME} ok")

    return server


def http_app(server: MCPServer, *, token: str = "", hosts: list[str] = ()):
    """The streamable HTTP app, accepting localhost and the given public hosts (a tunnel's)."""
    allowed = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    for h in hosts:
        allowed += [h, f"{h}:*"] if not h.endswith(":*") else [h.removesuffix(":*"), h]
    origins = ["https://chatgpt.com", "https://chat.openai.com", "https://claude.ai",
               "http://127.0.0.1:*", "http://localhost:*", *(f"https://{h.removesuffix(':*')}" for h in hosts)]
    return server.streamable_http_app(
        streamable_http_path=f"/{token}/mcp" if token else "/mcp",
        stateless_http=True,  # nothing to keep between calls; survives restarts and tunnels reconnecting
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=allowed, allowed_origins=origins),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--list-tools", action="store_true", help="print the tools and exit")
    args = ap.parse_args(argv)

    token = os.environ.get("YAMBA_MCP_TOKEN", "").strip("/")
    server = build_server(token=token, public_url=os.environ.get("YAMBA_MCP_PUBLIC_URL") or None)
    if args.list_tools:
        for t in asyncio.run(server.list_tools()):
            print(f"{t.name}: {(t.description or '').strip().splitlines()[0]}")
        return 0
    if args.transport == "stdio":
        server.run("stdio")
        return 0

    import uvicorn  # noqa: PLC0415 - comes with mcp

    hosts = [h.strip() for h in os.environ.get("YAMBA_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    app = http_app(server, token=token, hosts=hosts)
    print(f"MCP over streamable HTTP at http://{args.host}:{args.port}/{token + '/' if token else ''}mcp",
          file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
