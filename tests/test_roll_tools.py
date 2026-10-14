import asyncio
import base64
import json

import pytest
from mcp.client.client import Client
from starlette.testclient import TestClient

import mcp_server
import roll_api
import ui
from test_mcp_server import WHOLE, call, error, root  # noqa: F401 - root is a fixture
from xmlbuild import score


@pytest.fixture
def server(root, monkeypatch):  # noqa: F811
    monkeypatch.setenv("YAMBA_UI_URL", "http://127.0.0.1:8777")  # don't start a page server in tests
    return mcp_server.build_server()


def full(server, tool, **args):
    """A tool's whole result, _meta included."""
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)
    r = asyncio.run(go())
    assert not r.is_error, r.content
    return r


def test_the_tool_list_marks_the_editors_tools():
    async def listing():
        async with Client(mcp_server.build_server()) as client:
            return {t.name: t for t in (await client.list_tools()).tools}, await client.read_resource(mcp_server.ROLL_URI)
    tools, read = asyncio.run(listing())
    assert len(tools) == 36
    for name in ("create_piano_roll", "open_piano_roll"):
        assert tools[name].meta["ui"] == {"resourceUri": mcp_server.ROLL_URI}
    for name in ("load_piano_roll", "transform_roll", "save_piano_roll"):
        assert tools[name].meta == mcp_server.APP_ONLY
    ops = tools["edit_piano_roll"].input_schema["properties"]["operations"]
    assert {v["properties"]["op"]["enum"][0] for v in ops["items"]["anyOf"]} >= {"add", "mirror", "scale_time"}
    assert "roll" in tools["transform_roll"].input_schema["properties"]
    [html] = read.contents
    assert html.mime_type == "text/html;profile=mcp-app" and '<script id="boot" type="application/json">null</script>' in html.text


def test_an_agent_writes_a_piece_edits_it_in_words_and_puts_it_in_a_full_score(server, root):  # noqa: F811
    made = full(server, "create_piano_roll", name="canon", time_signature="3/4", key="D dorian", measures=4)
    assert made.structured_content["editor_url"] == "http://127.0.0.1:8777/roll?name=canon"
    assert made.meta[mcp_server.ROLL_META]["roll"]["revision"] == 1
    assert (root / "rolls/canon.roll.json").is_file() and (root / "rolls/canon.mid").read_bytes()[:4] == b"MThd"

    out = call(server, "add_notes", name="canon", part="rh", notes="D5:8 E5:8 F5:4 A5:4 | G5:2.")
    assert out["revision"] == 2 and out["preview"][0]["parts"]["rh"] == "D5:8 E5:8 F5:4 A5:4"
    out = call(server, "edit_piano_roll", name="canon", operations=[
        {"op": "copy", "select": {"measures": "1-2", "parts": ["rh"]}, "to": "3:1"},
        {"op": "retrograde_inversion", "select": {"measures": "3-4"}, "kind": "diatonic", "center": "A4"},
    ])
    assert out["changed_measures"] == "3-4" and [b["parts"]["rh"] for b in out["preview"]] == [
        "B3:2.", "A3:4 C4:4 D4:8 E4:8"]
    out = call(server, "change_notes", name="canon", at="1:2", pitches="F5", pitch="F#5")
    assert out["preview"][0]["parts"]["rh"] == "D5:8 E5:8 F#5:4 A5:4"
    assert "nothing matches" in error(server, "change_notes", name="canon", at="1:2", pitches="E4", pitch="F4")
    assert "operation 1 (mirror): the center Bb4 is not a note of D dorian" in error(
        server, "mirror_notes", name="canon", center="Bb4", kind="diatonic")

    read = call(server, "get_piano_roll", name="canon", measures="1-2", include_ids=True)
    assert [b["parts"]["rh"] for b in read["bars"]] == ["D5:8 E5:8 F#5:4 A5:4", "G5:2."]
    assert read["notes"][0] == {"id": read["notes"][0]["id"], "part": "rh", "pitch": "D5", "at": "1:1", "dur": "8", "vel": 80}

    assert call(server, "revert_piano_roll", name="canon", revision=3)["revision"] == 5
    assert call(server, "get_piano_roll", name="canon", measures="1")["bars"][0]["parts"]["rh"] == "D5:8 E5:8 F5:4 A5:4"

    call(server, "import_musicxml", name="intro", musicxml=score(WHOLE))
    result = call(server, "piano_roll_to_result", name="canon")
    assert result["result"] == "canon" and result["warnings"] == [] and result["musicxml"]["path"] == "outputs/canon.musicxml"
    combined = call(server, "combine_score", name="Suite", results=["intro", "canon"])
    assert combined["measures"] == 5
    assert call(server, "piano_roll_to_result", name="canon")["result"] == "canon"  # its own result: replaced
    library = call(server, "get_library")
    assert library["piano_rolls"] == [{"name": "canon", "title": "canon", "measures": 4,
                                       "parts": ["Right hand", "Left hand"], "revision": 6, "results": ["canon"]}]


def test_a_result_opens_as_a_roll_and_never_overwrites_itself(server, root):  # noqa: F811
    call(server, "import_musicxml", name="waltz", musicxml=score(WHOLE, WHOLE))
    made = full(server, "create_piano_roll", name="waltz", source="result", source_name="waltz")
    assert [p["id"] for p in made.structured_content["parts"]] == ["rh", "lh"]
    assert "notes only" in made.structured_content["import"]["note"]
    out = call(server, "piano_roll_to_result", name="waltz")
    assert out["result"] == "waltz-roll"  # waltz is taken by the source
    assert "already exists" in error(server, "piano_roll_to_result", name="waltz", result="waltz")
    assert "already a piano roll called waltz" in error(server, "create_piano_roll", name="waltz")


def test_midi_from_the_project_becomes_a_roll(server, root):  # noqa: F811
    call(server, "create_piano_roll", name="a", preset="single")
    call(server, "add_notes", name="a", part="melody", notes="C5:4 E5:4 G5:2")
    made = call(server, "create_piano_roll", name="b", source="midi", midi_path="rolls/a.mid")
    assert made["notes"] == 3 and made["source"]["kind"] == "midi"
    data = base64.b64encode((root / "rolls/a.mid").read_bytes()).decode()
    assert call(server, "create_piano_roll", name="c", source="midi", midi_base64=data)["notes"] == 3
    assert "inside the project" in error(server, "create_piano_roll", name="d", source="midi", midi_path="/etc/hosts")


def test_the_editor_saves_with_revisions_and_transforms_without_saving(server, root):  # noqa: F811
    doc = full(server, "create_piano_roll", name="x").meta[mcp_server.ROLL_META]["roll"]
    out = call(server, "transform_roll", roll=doc, operations=[{"op": "add", "part": "rh", "notes": "C5:1"}])
    assert len(out["roll"]["notes"]) == 1 and json.loads((root / "rolls/x.roll.json").read_text())["notes"] == []
    saved = call(server, "save_piano_roll", name="x", roll=out["roll"], base_revision=1)
    assert saved["revision"] == 2
    assert "changed since revision 1" in error(server, "save_piano_roll", name="x", roll=out["roll"], base_revision=1)
    assert call(server, "load_piano_roll", name="x", if_newer_than=2) == {"name": "x", "unchanged": True, "revision": 2}


def test_the_editor_page_and_its_api_over_http(root):  # noqa: F811
    server = mcp_server.build_server(token="s3cret", public_url="https://abc.example")
    made = full(server, "create_piano_roll", name="tune", preset="single")
    assert made.structured_content["editor_url"] == "https://abc.example/s3cret/roll?name=tune"
    headers = {"Content-Type": "application/json"}
    with TestClient(mcp_server.http_app(server, token="s3cret", hosts=["abc.example"]),
                    base_url="http://127.0.0.1:8790") as client:
        page = client.get("/s3cret/roll?name=tune")
        assert page.status_code == 200 and '"name": "tune"' in page.text and "{{BOOT}}" not in page.text
        assert client.get("/roll?name=tune").status_code == 404
        listing = client.get("/s3cret/api/roll/list").json()
        assert [r["name"] for r in listing["rolls"]] == ["tune"]
        ops = {"name": "tune", "operations": [{"op": "add", "part": "melody", "notes": "A4:2 B4:2"}]}
        assert client.post("/s3cret/api/roll/edit", json=ops, headers={**headers, "Host": "abc.example",
                                                                       "Origin": "https://abc.example"}).status_code == 200
        assert client.post("/s3cret/api/roll/edit", json=ops, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        assert client.post("/s3cret/api/roll/edit", content=json.dumps(ops), headers={"Content-Type": "text/plain"}).status_code == 403
        assert client.post("/s3cret/api/roll/edit", json=ops, headers={**headers, "Host": "evil.example"}).status_code == 403
        assert client.get("/s3cret/files/rolls/tune.mid").content[:4] == b"MThd"
        assert client.get("/s3cret/api/roll/load?name=nope").status_code == 404


def test_the_ui_serves_the_editor_too(root):  # noqa: F811
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    roll_api.create("song", preset="single")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert '"name": "song"' in urlopen(f"{base}/roll?name=song").read().decode()
        state = json.loads(urlopen(f"{base}/api/state").read())
        assert [r["name"] for r in state["rolls"]] == ["song"]
        req = Request(f"{base}/api/roll/to-result", data=json.dumps({"name": "song"}).encode(),
                      headers={"Content-Type": "application/json"})
        assert json.loads(urlopen(req).read())["result"] == "song"
    finally:
        httpd.shutdown()


def test_the_editor_page_cant_be_broken_out_of():
    page = roll_api.page_html({"mode": "page", "name": "</script><b>x"})
    assert "</script><b>" not in page and "\\u003c/script>\\u003cb>x" in page
