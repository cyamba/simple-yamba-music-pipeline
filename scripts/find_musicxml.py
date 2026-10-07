"""Find every MusicXML file on this computer: .musicxml, .mxl, and .xml files that hold a MusicXML score.

On macOS Spotlight (mdfind) answers in seconds; a walk of every drive then adds what Spotlight doesn't index
(hidden folders, .venv, unindexed volumes). Elsewhere only the walk runs. The local UI runs a Search in the
background and polls it for new rows.

Usage:  uv run python scripts/find_musicxml.py [FOLDER ...]   (default: every drive)
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from samples import nfc

EXTS = {".musicxml", ".mxl"}
SNIFF_EXTS = {".xml"}  # most .xml isn't music, so look inside
SNIFF_BYTES = 4096
ROOT_TAGS = tuple(t.encode(enc) for t in ("<score-partwise", "<score-timewise")
                  for enc in ("utf-8", "utf-16-le", "utf-16-be"))
DATALESS = 0x40000000  # macOS SF_DATALESS: an iCloud/Dropbox placeholder; reading it would download it
WIN_PLACEHOLDER = 0x400000 | 0x1000  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE
SKIP_NAMES = {".git", "node_modules", "__pycache__", ".Trash", ".Trashes", ".Spotlight-V100", ".fseventsd",
              ".DocumentRevisions-V100", "Backups.backupdb", "$Recycle.Bin", "System Volume Information"}
# /System holds the /System/Volumes/Data firmlink: a second copy of the whole disk.
SKIP_PATHS = {"darwin": {"/System", "/dev", "/private/var/vm"}, "linux": {"/proc", "/sys", "/dev", "/run"}}
RUNNING = ("spotlight", "walk")


def is_placeholder(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_flags", 0) & DATALESS or getattr(st, "st_file_attributes", 0) & WIN_PLACEHOLDER)


def is_musicxml(path: Path, st: os.stat_result) -> bool:
    suffix = path.suffix.lower()
    if suffix in EXTS:
        return True
    if suffix not in SNIFF_EXTS or is_placeholder(st):
        return False
    try:
        with path.open("rb") as f:
            head = f.read(SNIFF_BYTES)
    except OSError:
        return False
    return any(tag in head for tag in ROOT_TAGS)


def search_roots() -> list[Path]:
    if sys.platform == "win32":
        return [Path(f"{d}:\\") for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
    return [Path("/")]


def skip_paths(roots: list[Path]) -> set[str]:
    if sys.platform == "win32":
        return {os.path.join(str(r), "Windows") for r in roots}
    return SKIP_PATHS.get(sys.platform, set())


def walk(roots: Iterable[Path], on_file: Callable[[Path, os.stat_result], None],
         on_dir: Callable[[str, bool], None], stop: threading.Event) -> None:
    """Every folder under roots, depth first, without following links; on_dir(folder, readable)."""
    roots = list(roots)
    skip = skip_paths(roots)
    stack = [str(r) for r in reversed(roots)]
    while stack and not stop.is_set():
        top = stack.pop()
        try:
            with os.scandir(top) as it:
                entries = list(it)
        except OSError:
            on_dir(top, False)
            continue
        on_dir(top, True)
        subdirs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_NAMES and e.path not in skip:
                        subdirs.append(e.path)
                elif e.is_file(follow_symlinks=False) and os.path.splitext(e.name)[1].lower() in EXTS | SNIFF_EXTS:
                    st = e.stat(follow_symlinks=False)
                    if is_musicxml(Path(e.path), st):
                        on_file(Path(e.path), st)
            except OSError:
                continue
        stack.extend(sorted(subdirs, reverse=True))


def spotlight() -> list[Path]:
    """What Spotlight has indexed (macOS), or [] elsewhere or if it fails. Hits still go through is_musicxml."""
    if sys.platform != "darwin" or not shutil.which("mdfind"):
        return []
    query = " || ".join(f'kMDItemFSName == "*{ext}"c' for ext in sorted(EXTS | SNIFF_EXTS))
    try:
        p = subprocess.run(["mdfind", "-0", query], capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001 - Spotlight is only a head start; the walk finds everything anyway
        return []
    return [Path(os.fsdecode(b)) for b in p.stdout.split(b"\0") if b]


def row(path: Path, st: os.stat_result) -> dict:
    return {"path": nfc(str(path)), "name": nfc(path.name), "modified": st.st_mtime, "size": st.st_size}


class Search:
    """One background search at a time. A new start() or stop() bumps `run`, so a thread that is still
    finishing an earlier search (e.g. waiting on mdfind) can't add to the new one."""

    def __init__(self, roots: Callable[[], list[Path]] = search_roots,
                 spotlight: Callable[[], list[Path]] = spotlight) -> None:
        self._roots, self._spotlight = roots, spotlight
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.run = 0
        self._reset("idle")

    def _reset(self, phase: str) -> None:
        self.phase, self.current = phase, ""
        self.results: list[dict] = []
        self.seen: set[str] = set()
        self.dirs = self.unreadable = 0
        self.started, self.finished = time.time(), 0.0

    def start(self) -> None:
        with self._lock:
            self._stop.set()
            self._stop = threading.Event()
            self.run += 1
            self._reset("spotlight")
            args = (self.run, self._stop)
        self._thread = threading.Thread(target=self._search, args=args, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            if self.phase in RUNNING:
                self.phase, self.finished = "stopped", time.time()

    def wait(self) -> None:
        if self._thread:
            self._thread.join()

    def _search(self, run: int, stop: threading.Event) -> None:
        for path in sorted(self._spotlight(), key=lambda p: p.suffix.lower() not in EXTS):  # sure hits first
            if stop.is_set():
                return
            try:
                st = path.stat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and is_musicxml(path, st):
                self._add(run, path, st)
        with self._lock:
            if run != self.run or stop.is_set():
                return
            self.phase = "walk"
        walk(self._roots(), lambda p, st: self._add(run, p, st), lambda d, ok: self._dir(run, d, ok), stop)
        with self._lock:
            if run == self.run and self.phase == "walk":
                self.phase, self.finished, self.current = "done", time.time(), ""

    def _add(self, run: int, path: Path, st: os.stat_result) -> None:
        r = row(path, st)
        with self._lock:
            if run == self.run and r["path"] not in self.seen:
                self.seen.add(r["path"])
                self.results.append(r)

    def _dir(self, run: int, folder: str, readable: bool) -> None:
        with self._lock:
            if run == self.run:
                self.current = folder
                if readable:
                    self.dirs += 1
                else:
                    self.unreadable += 1

    def found(self, path: str) -> bool:
        with self._lock:
            return nfc(path) in self.seen

    def snapshot(self, since: int = 0) -> dict:
        """Progress, plus the rows found after the first `since` (the browser already has those)."""
        with self._lock:
            end = self.finished or time.time()
            return {"run": self.run, "phase": self.phase, "platform": sys.platform, "current": nfc(self.current),
                    "dirs": self.dirs, "unreadable": self.unreadable, "total": len(self.results),
                    "elapsed": round(end - self.started, 1) if self.phase != "idle" else 0,
                    "rows": self.results[max(since, 0):]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="*", type=Path, help="where to look (default: every drive, plus Spotlight)")
    args = ap.parse_args()
    roots = [f.resolve() for f in args.folders]
    search = Search(roots=lambda: roots, spotlight=lambda: []) if roots else Search()
    search.start()
    search.wait()
    s = search.snapshot()
    for r in sorted(s["rows"], key=lambda r: r["modified"], reverse=True):
        print(f"{datetime.fromtimestamp(r['modified']):%Y-%m-%d %H:%M}  {r['path']}")
    print(f"{s['total']} MusicXML files in {s['dirs']} folders ({s['unreadable']} unreadable), {s['elapsed']} s",
          file=sys.stderr)


if __name__ == "__main__":
    main()
