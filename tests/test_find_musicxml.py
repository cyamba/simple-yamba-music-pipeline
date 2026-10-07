import os
import sys
import threading
import zipfile

import pytest

from find_musicxml import Search, is_musicxml, walk
from xmlbuild import note, score

MUSIC = score(note("C4", 16))
TIMEWISE = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE score-timewise PUBLIC "-//Recordare//DTD MusicXML 4.0 '
            'Timewise//EN" "http://www.musicxml.org/dtds/timewise.dtd">\n<score-timewise version="4.0"></score-timewise>')


def make_tree(root):
    files = {"a/song.musicxml": MUSIC, "a/b/c/deep.xml": MUSIC, "timewise.xml": TIMEWISE,
             "a/plist.xml": '<?xml version="1.0"?><plist><dict/></plist>', "a/notes.txt": MUSIC,
             "node_modules/hidden.musicxml": MUSIC, ".git/x.musicxml": MUSIC, ".venv/lib/kept.musicxml": MUSIC}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    with zipfile.ZipFile(root / "a/packed.mxl", "w") as z:
        z.writestr("score.xml", MUSIC)
    return {root / p for p in ("a/song.musicxml", "a/b/c/deep.xml", "timewise.xml", "a/packed.mxl",
                               ".venv/lib/kept.musicxml")}


def collect(root):
    files, dirs = [], []
    walk([root], lambda p, st: files.append(p), lambda d, ok: dirs.append((d, ok)), threading.Event())
    return set(files), dirs


def test_is_musicxml_trusts_musicxml_extensions_and_sniffs_xml(tmp_path):
    cases = {"s.musicxml": ("", True), "s.mxl": ("", True), "p.xml": (MUSIC, True), "t.xml": (TIMEWISE, True),
             "o.xml": ("<?xml version='1.0'?><svg/>", False), "e.xml": ("", False), "s.txt": (MUSIC, False)}
    for name, (text, expected) in cases.items():
        (tmp_path / name).write_text(text)
        assert is_musicxml(tmp_path / name, (tmp_path / name).stat()) is expected, name


def test_is_musicxml_reads_utf16_xml(tmp_path):
    (tmp_path / "u.xml").write_text(MUSIC, encoding="utf-16")
    assert is_musicxml(tmp_path / "u.xml", (tmp_path / "u.xml").stat())


def test_walk_finds_nested_scores_and_skips_vcs_and_node_modules(tmp_path):
    expected = make_tree(tmp_path)
    files, dirs = collect(tmp_path)
    assert files == expected
    assert all(ok for _, ok in dirs)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_walk_does_not_follow_symlinks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/song.musicxml").write_text(MUSIC)
    (tmp_path / "a/loop").symlink_to(tmp_path)
    (tmp_path / "link.musicxml").symlink_to(tmp_path / "a/song.musicxml")
    files, _ = collect(tmp_path)
    assert files == {tmp_path / "a/song.musicxml"}


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, not root")
def test_walk_counts_unreadable_folders_and_carries_on(tmp_path):
    (tmp_path / "locked").mkdir()
    (tmp_path / "open").mkdir()
    (tmp_path / "open/song.musicxml").write_text(MUSIC)
    os.chmod(tmp_path / "locked", 0)
    try:
        files, dirs = collect(tmp_path)
    finally:
        os.chmod(tmp_path / "locked", 0o755)
    assert files == {tmp_path / "open/song.musicxml"}
    assert (str(tmp_path / "locked"), False) in dirs


def test_search_merges_spotlight_and_walk_without_duplicates(tmp_path):
    expected = make_tree(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-elsewhere.musicxml"
    outside.write_text(MUSIC)
    search = Search(roots=lambda: [tmp_path], spotlight=lambda: [tmp_path / "a/song.musicxml", outside,
                                                                 tmp_path / "a/plist.xml"])
    search.start()
    search.wait()
    s = search.snapshot()
    paths = [r["path"] for r in s["rows"]]
    assert s["phase"] == "done" and s["total"] == len(paths) == len(expected) + 1
    assert set(paths) == {str(p) for p in expected | {outside}}
    assert paths[:2] == [str(tmp_path / "a/song.musicxml"), str(outside)]  # Spotlight's hits come first
    row = next(r for r in s["rows"] if r["name"] == "deep.xml")
    assert row["modified"] == (tmp_path / "a/b/c/deep.xml").stat().st_mtime
    assert s["dirs"] > 0 and s["unreadable"] == 0
    assert search.snapshot(since=4)["rows"] == s["rows"][4:]
    assert search.found(str(outside)) and not search.found(str(tmp_path / "a/plist.xml"))


def test_stop_ends_the_search_and_a_new_start_starts_over(tmp_path):
    make_tree(tmp_path)
    gate = threading.Event()
    search = Search(roots=lambda: [tmp_path], spotlight=lambda: gate.wait(5) and [])
    search.start()
    assert search.snapshot()["phase"] == "spotlight"
    search.stop()
    gate.set()
    search.wait()
    s = search.snapshot()
    assert s["phase"] == "stopped" and s["total"] == 0 and s["dirs"] == 0
    search = Search(roots=lambda: [tmp_path], spotlight=lambda: [])
    search.start()
    search.wait()
    first = search.snapshot()
    search.start()
    search.wait()
    second = search.snapshot()
    assert second["run"] == first["run"] + 1 and second["total"] == first["total"] > 0
