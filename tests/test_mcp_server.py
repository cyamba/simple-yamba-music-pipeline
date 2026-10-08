import asyncio
import base64
import sys

import pytest
from mcp.client.client import Client
from starlette.testclient import TestClient

import edits
import mcp_server
import run_batch
import ui
from xmlbuild import backup, note, score

WHOLE = note("C5", 16) + backup(16) + note("C4", 16, staff=2)
TIMEWISE = '<?xml version="1.0"?><score-timewise version="4.0"/>'


@pytest.fixture
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    for d in ("inputs", "outputs", "review", "scores", "logs", "work"):
        (project / d).mkdir(parents=True)
    (project / "review/review.md").write_text("| sample | converted | opens in MuseScore |\n|---|---|---|\n")
    for name in ("ROOT", "OUTPUTS", "REVIEW", "INPUTS", "LOGS", "WORK"):
        monkeypatch.setattr(run_batch, name, project / ("" if name == "ROOT" else name.lower()))
    for name, path in (("ROOT", project), ("INPUTS", project / "inputs"), ("REVIEW", project / "review"),
                       ("STATE", project / "review/full-score.json"), ("SCORES", project / "scores")):
        monkeypatch.setattr(ui, name, path)
    monkeypatch.setattr(ui, "mscore_bin", lambda: None)  # no MuseScore render in tests
    monkeypatch.setattr(ui, "sync", lambda: [])  # nor edits to bring back
    return project


def run(server, *calls):
    """Call tools through a real MCP client, as an agent would. Returns (is_error, data) per call."""
    async def go():
        out = []
        async with Client(server) as client:
            for tool, args in calls:
                r = await client.call_tool(tool, args)
                out.append((r.is_error, r.content[0].text if r.is_error else r.structured_content))
        return out
    return asyncio.run(go())


def call(server, tool, **args):
    [(is_error, data)] = run(server, (tool, args))
    assert not is_error, data
    return data


def error(server, tool, **args):
    [(is_error, data)] = run(server, (tool, args))
    assert is_error, data
    return data


def test_tools_say_whether_they_change_anything():
    async def tools():
        async with Client(mcp_server.build_server()) as client:
            return (await client.list_tools()).tools
    by_name = {t.name: t.annotations for t in asyncio.run(tools())}
    assert {"get_library", "get_music", "search", "fetch", "import_musicxml", "transcribe", "get_job",
            "combine_score", "export", "open_in_musescore"} <= set(by_name)
    assert {n for n, a in by_name.items() if a.read_only_hint} == {
        "get_library", "get_music", "search", "fetch", "get_job", "get_computer_search"}
    assert {n for n, a in by_name.items() if a.destructive_hint} == {"delete_input"}


def test_an_agent_can_add_read_arrange_combine_and_rename(root):
    server = mcp_server.build_server()
    assert call(server, "import_musicxml", name="waltz", musicxml=score(WHOLE, WHOLE))["result"] == "waltz"
    assert call(server, "import_musicxml", name="intro", musicxml=score(WHOLE))["result"] == "intro"

    library = call(server, "get_library")
    assert library["inputs"] == ["intro.musicxml", "waltz.musicxml"]
    assert [(r["name"], r["status"], r["in_full_score"]) for r in library["results"]] == [
        ("waltz", "imported", True), ("intro", "imported", True)]

    music = call(server, "get_music", kind="result", name="waltz", measures="2")
    assert music["measures"] == 2 and music["time_signatures"] == [{"from_measure": 1, "time": "4/4"}]
    assert music["bars"] == [{"measure": 2, "number": "2", "time": "4/4", "staves": {"1": "C5:1", "2": "C4:1"}}]
    assert (music["lowest"], music["highest"]) == ("C4", "C5")

    [hit] = call(server, "search", query="walt")["results"]
    assert hit["id"] == "result:waltz"
    assert "m2 4/4: staff 1: C5:1 | staff 2: C4:1" in call(server, "fetch", id=hit["id"])["text"]

    arranged = call(server, "arrange_full_score", order=["intro"], name="Suite")
    assert arranged == {"name": "Suite", "will_combine": ["intro", "waltz"], "left_out": []}
    combined = call(server, "combine_score")
    assert (combined["name"], combined["measures"], combined["results"]) == ("Suite", 3, ["intro", "waltz"])
    assert (root / "scores/Suite.musicxml").exists()
    assert call(server, "get_music", kind="score", name="Suite")["measures"] == 3

    assert call(server, "rename", kind="result", old="waltz", new="valse")["renamed"] == {"waltz": "valse"}
    assert [r["name"] for r in call(server, "get_library")["results"]] == ["intro", "valse"]  # keeps its place
    assert call(server, "rename", kind="score", old="Suite", new="Suite-2")["renamed"] == {"Suite": "Suite-2"}

    call(server, "delete_input", name="valse.musicxml")
    assert call(server, "get_library")["inputs"] == ["intro.musicxml"]


def test_mistakes_come_back_as_messages_the_agent_can_act_on(root):
    server = mcp_server.build_server()
    call(server, "import_musicxml", name="waltz", musicxml=score(WHOLE))
    assert "no result called 'nope'; results: waltz" in error(server, "get_music", kind="result", name="nope")
    assert "partwise" in error(server, "import_musicxml", name="bad", musicxml=TIMEWISE)
    assert "already" in error(server, "import_musicxml", name="waltz", musicxml=score(WHOLE))
    assert "no results called nope" in error(server, "arrange_full_score", order=["nope"])
    assert "'3-8' or 'all'" in error(server, "get_music", kind="result", name="waltz", measures="one")
    assert "no input called" in error(server, "transcribe", input_name="nope.png")
    assert "either content_base64 or url" in error(server, "add_input", name="a.png")


def test_add_input_saves_images_for_the_batch_and_imports_musicxml(root):
    server = mcp_server.build_server()
    png = call(server, "add_input", name="page.png", content_base64=base64.b64encode(b"\x89PNG").decode())
    assert png["next"] == "call transcribe(input_name='page.png')" and (root / "inputs/page.png").exists()
    xml = call(server, "add_input", name="song.musicxml", content_base64=base64.b64encode(score(WHOLE).encode()).decode())
    assert (xml["result"], xml["status"]) == ("song", "imported")
    assert "accepted" in error(server, "add_input", name="notes.txt", content_base64="")


def test_transcription_runs_in_the_background_one_job_at_a_time(root, monkeypatch):
    (root / "inputs/page.png").write_bytes(b"png")
    commands = []

    def batch(only=None):
        commands.append(only)
        return [sys.executable, "-c", "import time; print('[homr] page ...', flush=True); time.sleep(1); print('1/1')"]
    monkeypatch.setattr(ui, "batch_command", batch)
    server = mcp_server.build_server()

    [(_, started), (busy, message)] = run(server, ("transcribe", {"input_name": "page.png"}), ("transcribe", {}))
    assert started["state"] == "running" and commands == ["page.png"]
    assert busy and "still running" in message
    done = call(server, "get_job", job_id=started["job_id"], wait_seconds=20)
    assert (done["state"], done["returncode"], done["log_tail"]) == ("done", 0, ["[homr] page ...", "1/1"])
    assert call(server, "get_job")["job_id"] == started["job_id"]  # the latest one


def test_only_one_input_is_redone_and_the_other_results_are_kept(root, monkeypatch):
    (root / "inputs/a.musicxml").write_text(score(WHOLE))
    (root / "inputs/b.musicxml").write_text(score(WHOLE, WHOLE))
    for name in ("mscore_bin", "write_environment"):
        monkeypatch.setattr(run_batch, name, lambda *a: None)
    monkeypatch.setattr(edits, "sync", lambda *a: [])
    monkeypatch.setattr(sys, "argv", ["run_batch.py"])
    run_batch.main()
    (root / "inputs/b.musicxml").write_text(score(WHOLE, WHOLE, WHOLE))
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--only", "b.musicxml"])
    run_batch.main()
    assert [(r["output_name"], r["status"]) for r in run_batch.read_result_rows()] == [
        ("a", "imported"), ("b", "imported")]
    assert (root / "outputs/b.musicxml").read_text().count("<measure") == 3


def test_http_serves_downloads_behind_the_token_and_checks_the_host(root):
    server = mcp_server.build_server(token="s3cret", public_url="https://abc.example/")
    out = call(server, "import_musicxml", name="waltz", musicxml=score(WHOLE))
    assert out["file"]["url"] == "https://abc.example/s3cret/files/outputs/waltz.musicxml"
    (root / "secret.txt").write_text("not for the web")

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(mcp_server.http_app(server, token="s3cret", hosts=["abc.example"]),
                    base_url="http://127.0.0.1:8777") as client:
        assert client.get("/s3cret/files/outputs/waltz.musicxml").text == score(WHOLE)
        assert client.get("/files/outputs/waltz.musicxml").status_code == 404
        assert client.get("/s3cret/files/inputs/%2e%2e/secret.txt").status_code == 404
        assert client.get("/healthz").text == "ok"
        assert client.post("/s3cret/mcp", json=init, headers=headers).status_code == 200
        assert client.post("/s3cret/mcp", json=init, headers={**headers, "Host": "abc.example"}).status_code == 200
        assert client.post("/s3cret/mcp", json=init, headers={**headers, "Host": "evil.example"}).status_code == 421
        assert client.post("/mcp", json=init, headers=headers).status_code == 404


def test_list_tools_prints_one_line_per_tool(capsys):
    assert mcp_server.main(["--list-tools"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 19 and lines[0].startswith("get_library: ")
