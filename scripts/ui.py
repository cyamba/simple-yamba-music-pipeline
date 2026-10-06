"""Tiny local UI: upload scores to inputs/, run the batch, browse results, put them in order,
rename them, concatenate them into a full score and export results or full scores to PDF or MIDI.

An uploaded .musicxml/.mxl (a score you already have) becomes a result straight away, without HOMR.

Usage:  uv run python scripts/ui.py [--port 8765]   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from combine import concatenate, sources_of
from export import export
from run_batch import (INPUTS, MUSICXML_EXTS, REVIEW, ROOT, SOURCE_EXTS, append_review_rows, check_musescore,
                       import_musicxml, mscore_bin, upsert_result)
from samples import check_name, nfc, rename_sample, rename_score

ALLOWED_EXTS = SOURCE_EXTS
SCORES = ROOT / "scores"
STATE = REVIEW / "full-score.json"  # order of the results, which go into the full score, its name
SERVABLE_DIRS = ("inputs", "outputs", "logs", "review", "scores", "exports")
CONTENT_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".musicxml": "application/vnd.recordare.musicxml+xml",
                 ".mxl": "application/vnd.recordare.musicxml", ".mid": "audio/midi",
                 ".log": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                 ".md": "text/plain; charset=utf-8"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HOMR batch</title>
<script>
  // Apply a saved light/dark choice before the page is drawn, so it doesn't flash.
  try { const t = localStorage.getItem('theme'); if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch (e) {}
</script>
<style>
  :root {
    color-scheme: light;
    --bg: #ffffff; --fg: #222222; --muted: #666666; --faint: #999999; --border: #e5e5e5; --dash: #aaaaaa;
    --accent: #2563eb; --accent-bg: #eff6ff; --code-bg: #f5f5f5; --ok: #15803d; --bad: #b91c1c;
    --link: #1d4ed8; --visited: #6d28d9; --track: #c9ccd1; --thumb: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg: #16181d; --fg: #e6e6e6; --muted: #9aa0a6; --faint: #6b7280; --border: #2c3038; --dash: #555b66;
      --accent: #6ea8fe; --accent-bg: #1e2a44; --code-bg: #1f2228; --ok: #4ade80; --bad: #f87171;
      --link: #8ab4f8; --visited: #c4a7ff; --track: #6ea8fe; --thumb: #16181d;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #16181d; --fg: #e6e6e6; --muted: #9aa0a6; --faint: #6b7280; --border: #2c3038; --dash: #555b66;
    --accent: #6ea8fe; --accent-bg: #1e2a44; --code-bg: #1f2228; --ok: #4ade80; --bad: #f87171;
    --link: #8ab4f8; --visited: #c4a7ff; --track: #6ea8fe; --thumb: #16181d;
  }
  html { background: var(--bg); }
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 16px;
         background: var(--bg); color: var(--fg); }
  a { color: var(--link); } a:visited { color: var(--visited); }
  .top { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  #drop { border: 2px dashed var(--dash); border-radius: 8px; padding: 2rem; text-align: center; cursor: pointer; }
  #drop.over { border-color: var(--accent); background: var(--accent-bg); }
  table { border-collapse: collapse; width: 100%; } td, th { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); }
  button { font: inherit; padding: 6px 14px; cursor: pointer; }
  td button { padding: 1px 8px; }
  pre { background: var(--code-bg); padding: 1rem; overflow-x: auto; max-height: 300px; }
  .ok { color: var(--ok); } .bad, .error { color: var(--bad); } .muted { color: var(--muted); } .small { font-size: 13px; }
  .nowrap { white-space: nowrap; }
  .handle { cursor: grab; color: var(--faint); user-select: none; width: 1em; }
  tr.dragging { opacity: .4; }
  tr.drop-before td { box-shadow: inset 0 2px 0 var(--accent); }
  tr.drop-after td { box-shadow: inset 0 -2px 0 var(--accent); }
  .name { cursor: text; border-bottom: 1px dashed var(--dash); }
  .name:hover, .name:focus { background: var(--accent-bg); outline: none; }
  input.rename { font: inherit; width: 16em; }
  .combine { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 1rem; }
  .combine input { font: inherit; padding: 5px 8px; width: 14em; }
  #theme { display: inline-flex; align-items: center; gap: 8px; padding: 4px 10px; background: none;
           color: var(--fg); border: 1px solid var(--border); border-radius: 999px; }
  #theme:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  #theme .track { position: relative; width: 30px; height: 16px; border-radius: 999px; background: var(--track); transition: background .15s; }
  #theme .thumb { position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%;
                  background: var(--thumb); transition: transform .15s; }
  #theme[aria-checked="true"] .thumb { transform: translateX(14px); }
  @media (prefers-reduced-motion: reduce) { #theme .track, #theme .thumb { transition: none; } }
</style></head><body>
<header class="top">
  <h1>HOMR batch</h1>
  <button id="theme" type="button" role="switch" aria-checked="false"><span class="track" aria-hidden="true"><span class="thumb"></span></span>Dark mode</button>
</header>
<div id="drop">Drop score images (PNG/JPG) or PDFs here, or click to choose
  <div class="muted small">MusicXML files you already have (.musicxml, .mxl) go straight to Results, without HOMR.</div>
  <input id="file" type="file" multiple accept=".png,.jpg,.jpeg,.pdf,.musicxml,.mxl" hidden></div>

<h2>Inputs</h2>
<table id="inputs"></table>
<p><button id="run">Run HOMR on all inputs</button> <span id="status" class="muted"></span></p>
<pre id="out" hidden></pre>

<h2>Results</h2>
<p class="muted small">Drag a row by ⠿ (or use ↑ ↓) to set the order, untick what to leave out of the full score,
  click a name to rename the sample (its input, outputs, logs and review rows are renamed with it).
  PDF / MIDI exports the result to exports/ and downloads it.</p>
<table id="results"></table>
<p id="rstatus" class="small" role="status"></p>
<div class="combine">
  <label for="fullname">Full score</label>
  <input id="fullname" placeholder="name">
  <button id="combine">Concatenate ticked results in order</button>
  <span id="cstatus" class="small" role="status"></span>
</div>
<p class="muted">Open the .musicxml files in MuseScore Studio and write your notes in review/review.md.</p>

<h2>Full scores</h2>
<table id="scores"></table>
<p id="sstatus" class="small" role="status"></p>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const link = (p, label) => p ? `<a href="/files/${encodeURI(p)}" target="_blank">${label}</a>` : '';
const post = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const exportButtons = (attr, i, name) => ['pdf', 'midi'].map(f =>
  `<button data-${attr}="${i}" data-fmt="${f}" aria-label="Export ${esc(name)} to ${f.toUpperCase()}">${f === 'pdf' ? 'PDF' : 'MIDI'}</button>`).join(' ');
let state = {inputs: [], results: [], full: {name: ''}, scores: []};

async function refresh() {
  state = await (await fetch('/api/state')).json();
  render();
}

function defaultName() {
  const names = state.results.filter(r => r.include).map(r => r.output_name);
  if (!names.length) return '';
  let p = names[0];
  for (const n of names) while (!n.startsWith(p)) p = p.slice(0, -1);
  if (names.length > 1) p = p.replace(/[-_ .][^-_ .]*$/, '');  // cut a partial word: "merkurius-m" -> "merkurius"
  p = p.replace(/[-_ .]+$/, '');
  return p ? p[0].toUpperCase() + p.slice(1) : 'Full score';
}

function render() {
  $('#inputs').innerHTML = state.inputs.length
    ? state.inputs.map(n => `<tr><td>${link('inputs/' + n, esc(n))}</td><td><button data-del="${esc(n)}">remove</button></td></tr>`).join('')
    : '<tr><td class="muted">No inputs yet.</td></tr>';
  const rs = state.results;
  $('#results').innerHTML = rs.length
    ? '<tr><th></th><th>in full score</th><th>sample</th><th>status</th><th>seconds</th><th>MuseScore</th><th>files</th><th>export</th><th>order</th></tr>' +
      rs.map((r, i) => `<tr draggable="true" data-i="${i}">
        <td class="handle" title="Drag to reorder">⠿</td>
        <td><input type="checkbox" data-inc="${i}" ${r.include ? 'checked' : ''} ${r.output ? '' : 'disabled'}
             aria-label="Include ${esc(r.output_name)} in the full score"></td>
        <td><span class="name" tabindex="0" role="button" data-ren="${i}" title="Click to rename">${esc(r.stem)}</span>${esc(r.output_name.slice(r.stem.length))}</td>
        <td class="${['ok', 'imported'].includes(r.status) ? 'ok' : 'bad'}" title="${esc(r.fixes ? 'fixes: ' + r.fixes : '')}">${esc(r.status)}</td>
        <td>${esc(r.seconds)}</td><td>${esc(r.musescore)}</td>
        <td class="nowrap">${link(r.output, 'musicxml')} ${link(r.render, 'render')} ${link(r.log, 'log')}</td>
        <td class="nowrap">${r.output ? exportButtons('exp', i, r.output_name) : ''}</td>
        <td class="nowrap"><button data-up="${i}" ${i ? '' : 'disabled'} aria-label="Move ${esc(r.output_name)} up">↑</button>
          <button data-down="${i}" ${i < rs.length - 1 ? '' : 'disabled'} aria-label="Move ${esc(r.output_name)} down">↓</button></td></tr>`).join('')
    : '<tr><td class="muted">No run yet.</td></tr>';
  if (document.activeElement !== $('#fullname')) $('#fullname').value = state.full.name || defaultName();
  $('#scores').innerHTML = state.scores.length
    ? '<tr><th>full score</th><th>made from</th><th>files</th><th>export</th></tr>' + state.scores.map((s, i) => `<tr>
        <td><span class="name" tabindex="0" role="button" data-score="${i}" title="Click to rename">${esc(s.name)}</span></td>
        <td class="muted small">${esc(s.sources.map(x => x.replace(/\\.musicxml$/, '')).join(' → '))}</td>
        <td class="nowrap">${link(s.xml, 'musicxml')} ${link(s.pdf, 'render')}</td>
        <td class="nowrap">${exportButtons('sexp', i, s.name)}</td></tr>`).join('')
    : '<tr><td class="muted">None yet.</td></tr>';
}

function saveOrder() {
  return post('/api/order', {
    order: state.results.map(r => r.output_name),
    exclude: state.results.filter(r => !r.include).map(r => r.output_name),
    name: state.full.name || ''});
}

function move(from, to, dir) {
  if (to < 0 || to >= state.results.length || from === to) return;
  const [r] = state.results.splice(from, 1);
  state.results.splice(to, 0, r);
  render();
  saveOrder();
  if (dir) {  // keep keyboard focus on the moved row
    let b = $(`[data-${dir}="${to}"]`);
    if (!b || b.disabled) b = $(`[data-${dir === 'up' ? 'down' : 'up'}="${to}"]`);
    b?.focus();
  }
}

// Inline rename: the name becomes a text field; Enter or leaving the field saves, Escape cancels.
function editName(span, current, url, statusEl) {
  const tr = span.closest('tr');
  tr.draggable = false;
  const input = document.createElement('input');
  input.className = 'rename';
  input.value = current;
  input.setAttribute('aria-label', `New name for ${current}`);
  span.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async save => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (!save || !value || value === current) return render();
    statusEl.textContent = `Renaming ${current}…`;
    const res = await post(url, {old: current, new: value});
    const text = await res.text();
    statusEl.innerHTML = res.ok ? esc(`Renamed ${current} to ${value}.`) : `<span class="error">${esc(text)}</span>`;
    if (res.ok && state.full.name === current) state.full.name = '';
    if (res.ok && url === '/api/rename-score') $('#cstatus').textContent = '';  // its link would be stale
    await refresh();
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') finish(true);
    if (e.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
}

// Export with MuseScore, then download the file it wrote to exports/.
async function exportFile(kind, name, fmt, button, statusEl) {
  button.disabled = true;
  statusEl.textContent = `Exporting ${name} to ${fmt.toUpperCase()} with MuseScore…`;
  const res = await post('/api/export', {kind, name, format: fmt});
  const text = await res.text();
  if (res.ok) {
    const {file} = JSON.parse(text);
    const a = document.createElement('a');
    a.href = '/files/' + encodeURI(file);
    a.download = file.split('/').pop();
    document.body.append(a);
    a.click();
    a.remove();
    statusEl.innerHTML = `Exported ${link(file, esc(file))}.`;
  } else {
    statusEl.innerHTML = `<span class="error">${esc(text)}</span>`;
  }
  button.disabled = false;
}

const results = $('#results');
results.addEventListener('click', e => {
  const t = e.target;
  if (t.dataset.exp !== undefined) exportFile('result', state.results[+t.dataset.exp].output_name, t.dataset.fmt, t, $('#rstatus'));
  if (t.dataset.up !== undefined) move(+t.dataset.up, +t.dataset.up - 1, 'up');
  if (t.dataset.down !== undefined) move(+t.dataset.down, +t.dataset.down + 1, 'down');
  if (t.dataset.ren !== undefined) editName(t, state.results[+t.dataset.ren].stem, '/api/rename', $('#rstatus'));
});
results.addEventListener('keydown', e => {
  if (e.target.dataset.ren !== undefined && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    editName(e.target, state.results[+e.target.dataset.ren].stem, '/api/rename', $('#rstatus'));
  }
});
results.addEventListener('change', e => {
  if (e.target.dataset.inc === undefined) return;
  state.results[+e.target.dataset.inc].include = e.target.checked;
  if (!state.full.name) $('#fullname').value = defaultName();
  saveOrder();
});

let dragFrom = null;
const clearMarks = () => document.querySelectorAll('.drop-before, .drop-after').forEach(x => x.classList.remove('drop-before', 'drop-after'));
results.addEventListener('dragstart', e => {
  const tr = e.target.closest?.('tr[data-i]');
  if (!tr) return;
  dragFrom = +tr.dataset.i;
  tr.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', tr.dataset.i);
});
results.addEventListener('dragover', e => {
  const tr = e.target.closest?.('tr[data-i]');
  if (dragFrom === null || !tr) return;
  e.preventDefault();
  clearMarks();
  const after = e.clientY > tr.getBoundingClientRect().top + tr.offsetHeight / 2;
  tr.classList.add(after ? 'drop-after' : 'drop-before');
});
results.addEventListener('drop', e => {
  const tr = e.target.closest?.('tr[data-i]');
  if (dragFrom === null || !tr) return;
  e.preventDefault();
  let to = +tr.dataset.i + (tr.classList.contains('drop-after') ? 1 : 0);
  if (to > dragFrom) to -= 1;
  const from = dragFrom;
  dragFrom = null;
  move(from, to);
});
results.addEventListener('dragend', () => {
  dragFrom = null;
  clearMarks();
  document.querySelectorAll('.dragging').forEach(x => x.classList.remove('dragging'));
});

$('#fullname').addEventListener('change', () => {
  state.full.name = $('#fullname').value.trim();
  saveOrder();
});

$('#combine').onclick = async () => {
  const name = $('#fullname').value.trim();
  const samples = state.results.filter(r => r.include).map(r => r.output_name);
  if (!samples.length) { $('#cstatus').textContent = 'Tick at least one result.'; return; }
  $('#combine').disabled = true;
  $('#cstatus').textContent = `Concatenating ${samples.length} results and rendering them in MuseScore…`;
  const res = await post('/api/combine', {name, samples});
  const text = await res.text();
  if (res.ok) {
    const c = JSON.parse(text);
    $('#cstatus').innerHTML = `Wrote ${link(c.xml, esc(c.xml))}: ${c.measures} measures from ${samples.length} results.
      MuseScore: ${c.pdf ? link(c.pdf, 'opens') : esc(c.musescore)}.`;
  } else {
    $('#cstatus').innerHTML = `<span class="error">${esc(text)}</span>`;
  }
  $('#combine').disabled = false;
  refresh();
};

const scores = $('#scores');
scores.addEventListener('click', e => {
  if (e.target.dataset.sexp !== undefined) exportFile('score', state.scores[+e.target.dataset.sexp].name, e.target.dataset.fmt, e.target, $('#sstatus'));
  if (e.target.dataset.score !== undefined) editName(e.target, state.scores[+e.target.dataset.score].name, '/api/rename-score', $('#sstatus'));
});
scores.addEventListener('keydown', e => {
  if (e.target.dataset.score !== undefined && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    editName(e.target, state.scores[+e.target.dataset.score].name, '/api/rename-score', $('#sstatus'));
  }
});

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

// Dark mode: follows the system until switched; switching back to the system's mode follows it again.
const systemDark = matchMedia('(prefers-color-scheme: dark)');
const isDark = () => (document.documentElement.dataset.theme || (systemDark.matches ? 'dark' : 'light')) === 'dark';
const showTheme = () => $('#theme').setAttribute('aria-checked', String(isDark()));
$('#theme').onclick = () => {
  const next = isDark() ? 'light' : 'dark';
  const followSystem = next === (systemDark.matches ? 'dark' : 'light');
  if (followSystem) delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = next;
  try { followSystem ? localStorage.removeItem('theme') : localStorage.setItem('theme', next); } catch (e) {}
  showTheme();
};
systemDark.addEventListener('change', showTheme);
showTheme();

refresh();
</script></body></html>
"""


def read_state() -> dict:
    state = {"order": [], "exclude": [], "name": ""}
    try:
        state |= json.loads(STATE.read_text())
    except (OSError, ValueError):
        pass
    return state


def write_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def read_results() -> list[dict]:
    """The rows of the last run, in the saved order, with whether each goes into the full score."""
    csv_path = REVIEW / "run-results.csv"
    if not csv_path.exists():
        return []
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    state = read_state()
    rank = {nfc(n): i for i, n in enumerate(state["order"])}
    excluded = {nfc(n) for n in state["exclude"]}
    rows.sort(key=lambda r: rank.get(nfc(r["output_name"]), len(rank)))
    for r in rows:
        r["output_name"], stem = nfc(r["output_name"]), nfc(Path(r["source"]).stem)
        r["stem"] = stem if r["output_name"].startswith(stem) else r["output_name"]
        r["include"] = bool(r["output"]) and r["output_name"] not in excluded
        render = REVIEW / "renders" / f"{r['output_name']}.pdf"
        r["render"] = str(render.relative_to(ROOT)) if render.exists() else ""
    return rows


def read_scores() -> list[dict]:
    out = []
    for xml in sorted(SCORES.glob("*.musicxml")) if SCORES.is_dir() else []:
        pdf = xml.with_suffix(".pdf")
        out.append({"name": nfc(xml.stem), "xml": str(xml.relative_to(ROOT)),
                    "pdf": str(pdf.relative_to(ROOT)) if pdf.exists() else "", "sources": sources_of(xml)})
    return out


def combine(name: str, samples: list[str]) -> dict:
    name = check_name(name)
    rows = {r["output_name"]: r for r in read_results()}
    missing = [s for s in samples if not rows.get(nfc(s), {}).get("output")]
    if missing:
        raise ValueError(f"no MusicXML for {', '.join(missing)}; run the batch first")
    xml = SCORES / f"{name}.musicxml"
    measures = concatenate([ROOT / rows[nfc(s)]["output"] for s in samples], name, xml)
    (SCORES / f"{name}.pdf").unlink(missing_ok=True)
    musescore = check_musescore(mscore_bin(), str(xml.relative_to(ROOT)), name, SCORES)
    pdf = SCORES / f"{name}.pdf"
    return {"name": name, "measures": measures, "musescore": musescore, "xml": str(xml.relative_to(ROOT)),
            "pdf": str(pdf.relative_to(ROOT)) if pdf.exists() and musescore == "yes" else ""}


def import_upload(name: str, data: bytes) -> dict:
    """Save an uploaded .musicxml/.mxl to inputs/ and make it a result right away."""
    stem = nfc(Path(name).stem)
    try:
        check_name(stem)
    except ValueError as e:
        raise ValueError(f"{name}: {e}") from None
    taken = next((p for p in INPUTS.iterdir()
                  if nfc(p.stem) == stem and p.suffix.lower() in ALLOWED_EXTS and nfc(p.name) != nfc(name)), None)
    if taken:
        raise ValueError(f"{taken.name} is already in inputs/ under the name {stem}; rename one of them first")
    src = INPUTS / name
    src.write_bytes(data)
    try:
        row = import_musicxml(src, mscore_bin())
    except ValueError:
        src.unlink()
        raise
    upsert_result(row)
    append_review_rows([row])
    return row


def export_file(kind: str, name: str, fmt: str) -> dict:
    """Export a result ("result") or a full score ("score") to exports/<name>.pdf|.mid."""
    if kind == "result":
        output = next((r["output"] for r in read_results() if r["output_name"] == nfc(name)), "")
        xml = ROOT / output if output else None
    elif kind == "score":
        xml = next((p for p in SCORES.glob("*.musicxml") if nfc(p.stem) == nfc(name)), None)
    else:
        raise ValueError(f"unknown kind {kind!r}")
    if xml is None:
        raise ValueError(f"no MusicXML for {name}")
    return {"file": str(export(xml, fmt).relative_to(ROOT))}


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: bytes | str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: object) -> None:
        self.send(200, json.dumps(data, ensure_ascii=False), "application/json")

    def input_name(self) -> str | None:
        """Validated bare filename from ?name=, or None."""
        name = Path(parse_qs(urlparse(self.path).query).get("name", [""])[0]).name
        return name if name and Path(name).suffix.lower() in ALLOWED_EXTS else None

    def json_body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            inputs = sorted(p.name for p in INPUTS.iterdir() if p.suffix.lower() in ALLOWED_EXTS)
            return self.send_json({"inputs": inputs, "results": read_results(),
                                   "full": {"name": read_state()["name"]}, "scores": read_scores()})
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
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if Path(name).suffix.lower() in MUSICXML_EXTS:
                try:
                    return self.send_json(import_upload(name, data))
                except (ValueError, OSError) as e:
                    return self.send(400, str(e))
            (INPUTS / name).write_bytes(data)
            return self.send(200, "ok")
        if path == "/api/run":
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_batch.py")],
                               capture_output=True, text=True, cwd=ROOT)
            return self.send(200, p.stdout + (f"\n{p.stderr}" if p.returncode else ""))
        try:
            body = self.json_body()
            if path == "/api/order":
                state = read_state()
                state.update({k: body[k] for k in ("order", "exclude", "name") if k in body})
                write_state(state)
                return self.send(200, "ok")
            if path == "/api/rename":
                names = rename_sample(ROOT, body["old"], body["new"])
                state = read_state()
                for key in ("order", "exclude"):
                    state[key] = [names.get(nfc(n), n) for n in state[key]]
                write_state(state)
                return self.send_json(names)
            if path == "/api/combine":
                return self.send_json(combine(body.get("name", ""), body.get("samples", [])))
            if path == "/api/rename-score":
                return self.send_json({"name": rename_score(SCORES, body["old"], body["new"])})
            if path == "/api/export":
                return self.send_json(export_file(body["kind"], body["name"], body["format"]))
        except (ValueError, KeyError, OSError, ET.ParseError) as e:
            return self.send(400, str(e) if not isinstance(e, KeyError) else f"missing {e}")
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
