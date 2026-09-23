"""Tiny local UI: upload scores to inputs/, run the batch, browse results.

Usage:  uv run python scripts/ui.py [--port 8765]   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from run_batch import IMAGE_EXTS, INPUTS, REVIEW, ROOT

ALLOWED_EXTS = IMAGE_EXTS | {".pdf"}
SERVABLE_DIRS = ("inputs", "outputs", "logs", "review")
CONTENT_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".musicxml": "application/vnd.recordare.musicxml+xml",
                 ".log": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                 ".md": "text/plain; charset=utf-8"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HOMR batch</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 16px; color: #222; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  #drop { border: 2px dashed #aaa; border-radius: 8px; padding: 2rem; text-align: center; cursor: pointer; }
  #drop.over { border-color: #2563eb; background: #eff6ff; }
  table { border-collapse: collapse; width: 100%; } td, th { text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; }
  button { font: inherit; padding: 6px 14px; cursor: pointer; }
  pre { background: #f5f5f5; padding: 1rem; overflow-x: auto; max-height: 300px; }
  .ok { color: #15803d; } .bad { color: #b91c1c; } .muted { color: #777; }
</style></head><body>
<h1>HOMR batch</h1>
<div id="drop">Drop score images (PNG/JPG) or PDFs here, or click to choose
  <input id="file" type="file" multiple accept=".png,.jpg,.jpeg,.pdf" hidden></div>

<h2>Inputs</h2>
<table id="inputs"></table>
<p><button id="run">Run HOMR on all inputs</button> <span id="status" class="muted"></span></p>
<pre id="out" hidden></pre>

<h2>Results</h2>
<table id="results"></table>
<p class="muted">Open the .musicxml files in MuseScore Studio and write your notes in review/review.md.</p>

<script>
const $ = s => document.querySelector(s);
const link = (p, label) => p ? `<a href="/files/${encodeURI(p)}" target="_blank">${label}</a>` : '';

async function refresh() {
  const s = await (await fetch('/api/state')).json();
  $('#inputs').innerHTML = s.inputs.length
    ? s.inputs.map(n => `<tr><td>${link('inputs/' + n, n)}</td><td><button data-del="${n}">remove</button></td></tr>`).join('')
    : '<tr><td class="muted">No inputs yet.</td></tr>';
  $('#results').innerHTML = s.results.length
    ? '<tr><th>sample</th><th>status</th><th>seconds</th><th>MuseScore</th><th>files</th></tr>' +
      s.results.map(r => `<tr><td>${r.output_name}</td>
        <td class="${r.status === 'ok' ? 'ok' : 'bad'}">${r.status}</td><td>${r.seconds}</td><td>${r.musescore}</td>
        <td>${link(r.output, 'musicxml')} ${link(r.render, 'render')} ${link(r.log, 'log')}</td></tr>`).join('')
    : '<tr><td class="muted">No run yet.</td></tr>';
}

async function upload(files) {
  for (const f of files) {
    $('#status').textContent = `Uploading ${f.name}…`;
    const r = await fetch('/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f });
    if (!r.ok) alert(`${f.name}: ${await r.text()}`);
  }
  $('#status').textContent = '';
  refresh();
}

const drop = $('#drop');
drop.onclick = () => $('#file').click();
$('#file').onchange = e => upload(e.target.files);
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files); };

$('#inputs').onclick = async e => {
  const name = e.target.dataset.del;
  if (name && confirm(`Remove ${name} from inputs/?`)) {
    await fetch('/api/input?name=' + encodeURIComponent(name), { method: 'DELETE' });
    refresh();
  }
};

$('#run').onclick = async () => {
  $('#run').disabled = true;
  $('#status').textContent = 'Running… (the first run downloads models; each page can take a minute)';
  const r = await fetch('/api/run', { method: 'POST' });
  $('#out').hidden = false;
  $('#out').textContent = await r.text();
  $('#status').textContent = '';
  $('#run').disabled = false;
  refresh();
};

refresh();
</script></body></html>
"""


def read_results() -> list[dict]:
    csv_path = REVIEW / "run-results.csv"
    if not csv_path.exists():
        return []
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        render = REVIEW / "renders" / f"{r['output_name']}.pdf"
        r["render"] = str(render.relative_to(ROOT)) if render.exists() else ""
    return rows


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: bytes | str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def input_name(self) -> str | None:
        """Validated bare filename from ?name=, or None."""
        name = Path(parse_qs(urlparse(self.path).query).get("name", [""])[0]).name
        return name if name and Path(name).suffix.lower() in ALLOWED_EXTS else None

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            inputs = sorted(p.name for p in INPUTS.iterdir() if p.suffix.lower() in ALLOWED_EXTS)
            return self.send(200, json.dumps({"inputs": inputs, "results": read_results()}), "application/json")
        if path.startswith("/files/"):
            target = (ROOT / path.removeprefix("/files/")).resolve()
            allowed = any(target.is_relative_to(ROOT / d) for d in SERVABLE_DIRS)
            if allowed and target.is_file():
                return self.send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))
        self.send(404, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/upload":
            name = self.input_name()
            if not name:
                return self.send(400, f"only {', '.join(sorted(ALLOWED_EXTS))} files are accepted")
            (INPUTS / name).write_bytes(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            return self.send(200, "ok")
        if path == "/api/run":
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_batch.py")],
                               capture_output=True, text=True, cwd=ROOT)
            return self.send(200, p.stdout + (f"\n{p.stderr}" if p.returncode else ""))
        self.send(404, "not found")

    def do_DELETE(self) -> None:
        name = self.input_name()
        if urlparse(self.path).path == "/api/input" and name and (INPUTS / name).is_file():
            (INPUTS / name).unlink()
            return self.send(200, "ok")
        self.send(404, "not found")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"HOMR UI on http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
