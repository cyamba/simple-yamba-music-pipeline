"""Tiny local UI: upload scores to inputs/, run the batch, browse results, put them in order,
rename them, concatenate them into a full score and export results or full scores to PDF or MIDI.

An uploaded .musicxml/.mxl (a score you already have) becomes a result straight away, without HOMR.
Edit opens a result or full score in MuseScore Studio; what you save there comes back (see edits.py).
It can also find every MusicXML file on the computer and add the ones you pick to the results.

Usage:  uv run python scripts/ui.py [--port 8765]   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from combine import concatenate, sources_of
from edits import SCORES, open_in_musescore, sync
from export import export
from find_musicxml import Search
from run_batch import (INPUTS, MSCORE_CANDIDATES, MUSICXML_EXTS, REVIEW, ROOT, SOURCE_EXTS, append_review_rows,
                       check_musescore, import_musicxml, mscore_bin, upsert_result)
from samples import check_name, nfc, rename_sample, rename_score

ALLOWED_EXTS = SOURCE_EXTS
STATE = REVIEW / "full-score.json"  # order of the results, which go into the full score, its name
SEARCH = Search()
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
  #fstatus { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }  /* one line, so the table doesn't jump */
  #found .fname { overflow-wrap: anywhere; min-width: 12em; }
  #found .path { max-width: 0; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  #found .path:hover, #found .path:focus { white-space: normal; overflow-wrap: anywhere; background: var(--accent-bg); outline: none; }
  #found th .sort { font: inherit; font-weight: bold; padding: 0; background: none; border: 0; color: var(--fg); cursor: pointer; }
  #found th .sort:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
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
  PDF / MIDI exports the result to exports/ and downloads it.
  Edit opens it in MuseScore Studio: save there with ⌘S (not Export) and come back here: the edits
  are loaded when this page gets focus, and concatenation uses them.
  <button id="sync" type="button">Reload edits from MuseScore</button></p>
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

<h2>MusicXML on this computer</h2>
<p class="muted small">Finds .musicxml, .mxl and MusicXML .xml files on every drive. Hover over a path (or tab to it) to see all of it.
  Tick files and add them to the results to concatenate them with the rest; they are copied into inputs/, the originals stay as they are.</p>
<div class="combine">
  <button id="search">Search this computer</button>
  <button id="stopsearch" hidden>Stop</button>
  <input id="filter" type="search" placeholder="filter by name or folder" aria-label="Filter the files found">
  <button id="addfound" disabled>Add ticked to results</button>
</div>
<p id="fstatus" class="small" role="status"></p>
<p id="fhint" class="small muted" hidden></p>
<p id="faction" class="small" role="status"></p>
<table id="found"></table>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const link = (p, label) => p ? `<a href="/files/${encodeURI(p)}" target="_blank">${label}</a>` : '';
const post = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const ACTIONS = {edit: ['Edit', 'Open {} in MuseScore Studio'], pdf: ['PDF', 'Export {} to PDF'], midi: ['MIDI', 'Export {} to MIDI']};
const actionButtons = name => Object.entries(ACTIONS).map(([act, [label, aria]]) =>
  `<button data-act="${act}" aria-label="${esc(aria.replace('{}', name))}">${label}</button>`).join(' ');
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
    ? '<tr><th></th><th>in full score</th><th>sample</th><th>status</th><th>seconds</th><th>MuseScore</th><th>files</th><th>edit / export</th><th>order</th></tr>' +
      rs.map((r, i) => `<tr draggable="true" data-i="${i}">
        <td class="handle" title="Drag to reorder">⠿</td>
        <td><input type="checkbox" data-inc="${i}" ${r.include ? 'checked' : ''} ${r.output ? '' : 'disabled'}
             aria-label="Include ${esc(r.output_name)} in the full score"></td>
        <td><span class="name" tabindex="0" role="button" data-ren="${i}" title="Click to rename">${esc(r.stem)}</span>${esc(r.output_name.slice(r.stem.length))}</td>
        <td class="${['ok', 'imported', 'edited'].includes(r.status) ? 'ok' : 'bad'}"
            title="${esc(r.status === 'edited' ? `edited in MuseScore: edits/${r.output_name}.mscz` : r.fixes ? 'fixes: ' + r.fixes : '')}">${esc(r.status)}</td>
        <td>${esc(r.seconds)}</td><td>${esc(r.musescore)}</td>
        <td class="nowrap">${link(r.output, 'musicxml')} ${link(r.render, 'render')} ${link(r.log, 'log')}</td>
        <td class="nowrap">${r.output ? actionButtons(r.output_name) : ''}</td>
        <td class="nowrap"><button data-up="${i}" ${i ? '' : 'disabled'} aria-label="Move ${esc(r.output_name)} up">↑</button>
          <button data-down="${i}" ${i < rs.length - 1 ? '' : 'disabled'} aria-label="Move ${esc(r.output_name)} down">↓</button></td></tr>`).join('')
    : '<tr><td class="muted">No run yet.</td></tr>';
  if (document.activeElement !== $('#fullname')) $('#fullname').value = state.full.name || defaultName();
  $('#scores').innerHTML = state.scores.length
    ? '<tr><th>full score</th><th>made from</th><th>files</th><th>edit / export</th></tr>' + state.scores.map((s, i) => `<tr data-s="${i}">
        <td><span class="name" tabindex="0" role="button" data-score="${i}" title="Click to rename">${esc(s.name)}</span></td>
        <td class="muted small">${esc(s.sources.map(x => x.replace(/\\.musicxml$/, '')).join(' → '))}</td>
        <td class="nowrap">${link(s.xml, 'musicxml')} ${link(s.pdf, 'render')}</td>
        <td class="nowrap">${actionButtons(s.name)}</td></tr>`).join('')
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

async function openInMuseScore(kind, name, button, statusEl) {
  button.disabled = true;
  statusEl.textContent = `Opening ${name} in MuseScore…`;
  const res = await post('/api/open', {kind, name});
  const text = await res.text();
  statusEl.innerHTML = res.ok
    ? esc(`Opened ${JSON.parse(text).mscz} in MuseScore. Save there with ⌘S, then come back: the edits load here by themselves.`)
    : `<span class="error">${esc(text)}</span>`;
  button.disabled = false;
}

function act(kind, name, button, statusEl) {
  if (button.dataset.act === 'edit') openInMuseScore(kind, name, button, statusEl);
  else exportFile(kind, name, button.dataset.act, button, statusEl);
}

// Bring back what was saved in MuseScore: when this page gets focus again, or on request.
let syncing = false;
async function syncEdits(asked) {
  if (syncing) return;
  syncing = true;
  try {
    const res = await post('/api/sync', {});
    const text = await res.text();
    if (!res.ok) { $('#rstatus').innerHTML = `<span class="error">${esc(text)}</span>`; return; }
    const {synced} = JSON.parse(text);
    if (synced.length) {
      $('#rstatus').textContent = `Loaded the edits saved in MuseScore: ${synced.join(', ')}.`;
      await refresh();
    } else if (asked) {
      $('#rstatus').textContent = 'Nothing new saved in MuseScore.';
    }
  } finally {
    syncing = false;
  }
}
addEventListener('focus', () => syncEdits(false));
$('#sync').onclick = () => syncEdits(true);

const results = $('#results');
results.addEventListener('click', e => {
  const t = e.target;
  if (t.dataset.act) act('result', state.results[+t.closest('tr').dataset.i].output_name, t, $('#rstatus'));
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
  if (state.scores.some(s => s.name === name && s.mscz)
      && !confirm(`The full score ${name} has been opened in MuseScore. Concatenating again replaces it, with any edits made to it there. Continue?`)) return;
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
  if (e.target.dataset.act) act('score', state.scores[+e.target.closest('tr').dataset.s].name, e.target, $('#sstatus'));
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

// MusicXML on this computer: the server searches in the background; poll it for the rows found since last time.
const SHOW = 500;  // rows drawn at once; the filter narrows the rest
let found = {run: null, rows: [], s: {phase: 'idle'}}, sortBy = {key: 'modified', desc: true}, polling = null;
let ticked = new Set(), shown = [];  // paths ticked to add to the results; the rows on screen
const took = t => t < 60 ? `${Math.round(t)} s` : `${Math.floor(t / 60)} min ${Math.round(t % 60)} s`;
const num = n => n.toLocaleString();

async function pollSearch() {
  clearTimeout(polling);
  let s = await (await fetch(`/api/search?since=${found.rows.length}`)).json();
  if (s.run !== found.run) {  // a new search: start over
    found = {run: s.run, rows: [], s};
    ticked.clear();
    s = await (await fetch('/api/search?since=0')).json();
  }
  const added = s.rows.length;
  found.rows = found.rows.concat(s.rows);
  found.s = s;
  showSearch();
  if (added || $('#found').innerHTML === '' || s.phase !== 'walk') renderFound();
  if (s.phase === 'spotlight' || s.phase === 'walk') polling = setTimeout(pollSearch, 1000);
}

function showSearch() {
  const s = found.s, busy = s.phase === 'spotlight' || s.phase === 'walk', n = num(found.rows.length);
  $('#search').disabled = busy;
  $('#search').textContent = s.phase === 'idle' ? 'Search this computer' : 'Search again';
  $('#stopsearch').hidden = !busy;
  const unread = s.unreadable ? `, ${num(s.unreadable)} unreadable` : '';
  const text = {spotlight: `Asking Spotlight… ${n} found so far.`,
                walk: `${n} found in ${num(s.dirs)} folders${unread} — searching ${s.current}`,
                done: `Done in ${took(s.elapsed)}: ${n} MusicXML files in ${num(s.dirs)} folders${unread}.`,
                stopped: `Stopped after ${took(s.elapsed)}: ${n} found in ${num(s.dirs)} folders${unread}.`}[s.phase] || '';
  $('#fstatus').textContent = $('#fstatus').title = text;
  $('#fhint').hidden = busy || !s.unreadable;
  $('#fhint').textContent = `${num(s.unreadable)} folders couldn't be read, so they weren't searched` +
    (s.platform === 'darwin' ? '. To include them, give your terminal Full Disk Access in System Settings › Privacy & Security and search again.' : '.');
}

function renderFound() {
  const table = $('#found'), s = found.s;
  showTicked();
  if (s.phase === 'idle') { table.innerHTML = ''; return; }
  const q = $('#filter').value.trim().toLowerCase(), {key, desc} = sortBy;
  const rows = found.rows.filter(r => !q || r.path.toLowerCase().includes(q))
    .sort((a, b) => (key === 'modified' ? a.modified - b.modified : a[key].localeCompare(b[key])) * (desc ? -1 : 1));
  // Re-rendering mustn't steal keyboard focus from a row
  const focusKey = table.contains(document.activeElement) ? document.activeElement.dataset.k : null;
  const th = (k, label) => `<th aria-sort="${key === k ? (desc ? 'descending' : 'ascending') : 'none'}">` +
    `<button class="sort" data-sort="${k}" data-k="sort:${k}">${label}${key === k ? (desc ? ' ↓' : ' ↑') : ''}</button></th>`;
  const where = {darwin: 'Finder', win32: 'Explorer'}[s.platform] || 'the file manager';
  shown = rows.slice(0, SHOW);
  const all = shown.length && shown.every(r => ticked.has(r.path));
  table.innerHTML = rows.length
    ? `<tr><th><input type="checkbox" data-tickall data-k="tickall" ${all ? 'checked' : ''} aria-label="Tick all ${num(shown.length)} files shown"></th>${th('name', 'name')}${th('modified', 'last modified')}${th('path', 'path')}<th></th></tr>` +
      shown.map(r => { const p = esc(r.path), d = new Date(r.modified * 1000); return `<tr>
        <td><input type="checkbox" data-tick="${p}" data-k="tick:${p}" ${ticked.has(r.path) ? 'checked' : ''} aria-label="Tick ${esc(r.name)} to add it to the results"></td>
        <td class="fname">${esc(r.name)}</td>
        <td class="nowrap" title="${esc(d.toISOString())}">${esc(d.toLocaleString())}</td>
        <td class="path" tabindex="0" title="${p}" data-k="path:${p}">${p}</td>
        <td class="nowrap"><button data-reveal="${p}" data-k="reveal:${p}" title="Show in ${where}" aria-label="Show ${esc(r.name)} in ${where}">Reveal</button>
          <button data-open="${p}" data-k="open:${p}" title="Open in MuseScore" aria-label="Open ${esc(r.name)} in MuseScore">Open</button>
          <button data-copy="${p}" data-k="copy:${p}" title="Copy the path" aria-label="Copy the path of ${esc(r.name)}">Copy</button></td></tr>`; }).join('') +
      (rows.length > SHOW ? `<tr><td colspan="5" class="muted small">Showing ${num(SHOW)} of ${num(rows.length)}; filter to narrow.</td></tr>` : '')
    : `<tr><td class="muted">${q && found.rows.length ? 'Nothing matches the filter.' : s.phase === 'spotlight' || s.phase === 'walk' ? 'Nothing found yet.' : 'No MusicXML files found.'}</td></tr>`;
  if (focusKey) [...table.querySelectorAll('[data-k]')].find(el => el.dataset.k === focusKey)?.focus();
}

$('#search').onclick = async () => {
  $('#faction').textContent = '';
  await post('/api/search', {});
  pollSearch();
};
$('#stopsearch').onclick = async () => { await post('/api/search/stop', {}); pollSearch(); };
$('#filter').oninput = renderFound;

function showTicked() {
  const n = ticked.size;
  $('#addfound').disabled = !n;
  $('#addfound').textContent = n ? `Add ${num(n)} ticked to results` : 'Add ticked to results';
}

$('#found').addEventListener('change', e => {
  const t = e.target;
  if (t.dataset.tick !== undefined) t.checked ? ticked.add(t.dataset.tick) : ticked.delete(t.dataset.tick);
  if (t.dataset.tickall !== undefined) {
    for (const r of shown) t.checked ? ticked.add(r.path) : ticked.delete(r.path);
    return renderFound();
  }
  showTicked();
});

$('#addfound').onclick = async () => {
  const paths = [...ticked];
  $('#addfound').disabled = true;
  $('#faction').textContent = `Adding ${num(paths.length)} to the results… (each is checked and rendered in MuseScore)`;
  const res = await post('/api/found/add', {paths});
  const text = await res.text();
  if (res.ok) {
    const a = JSON.parse(text);
    const said = [a.added.length && `Added to the results: ${a.added.join(', ')}.`,
                  a.already.length && `Already in the results: ${a.already.join(', ')}.`].filter(Boolean).join(' ');
    $('#faction').innerHTML = esc(said) + a.errors.map(x => `<br><span class="error">${esc(x.error)}</span>`).join('');
    ticked = new Set(a.errors.map(x => x.path));  // keep what failed ticked
  } else {
    $('#faction').innerHTML = `<span class="error">${esc(text)}</span>`;
  }
  renderFound();
  refresh();
};
$('#found').addEventListener('click', async e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.sort) {
    const k = b.dataset.sort;
    sortBy = {key: k, desc: sortBy.key === k ? !sortBy.desc : k === 'modified'};
    return renderFound();
  }
  if (b.dataset.copy !== undefined) {
    try {
      await navigator.clipboard.writeText(b.dataset.copy);
      $('#faction').textContent = `Copied ${b.dataset.copy}`;
    } catch (err) {
      $('#faction').innerHTML = `<span class="error">Couldn't copy: ${esc(err.message)}</span>`;
    }
    return;
  }
  const action = b.dataset.reveal !== undefined ? 'reveal' : b.dataset.open !== undefined ? 'open' : null;
  if (!action) return;
  const res = await post('/api/found/' + action, {path: b.dataset[action]});
  $('#faction').innerHTML = res.ok ? '' : `<span class="error">${esc(await res.text())}</span>`;
});

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

refresh().then(() => syncEdits(false));
pollSearch();  // picks up a search that is still running from before a reload
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
                    "pdf": str(pdf.relative_to(ROOT)) if pdf.exists() else "", "sources": sources_of(xml),
                    "mscz": xml.with_suffix(".mscz").exists()})
    return out


def combine(name: str, samples: list[str]) -> dict:
    name = check_name(name)
    sync()  # concatenate what was last saved in MuseScore
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


def launch_found(action: str, path: str) -> None:
    """Show a search result in Finder/Explorer, or open it in MuseScore. Only search results are accepted,
    so no web page can use this endpoint to launch whatever file it likes."""
    if not SEARCH.found(path):
        raise ValueError("That file isn't in the search results; search again.")
    if not Path(path).is_file():
        raise ValueError(f"{path} is gone.")
    if action == "open":
        mscore = mscore_bin()
        if not mscore:
            raise ValueError("MuseScore Studio isn't installed (or isn't where run_batch.py looks for it).")
        # On macOS hand it to the app bundle, so a MuseScore that is already open opens it in a new tab
        # (Homebrew's mscore is a shim script outside the bundle, hence the known bundle paths too)
        app = next((p for c in (mscore, *MSCORE_CANDIDATES) for p in Path(c).resolve().parents
                    if p.suffix == ".app" and p.is_dir()), None)
        cmd = ["open", "-a", str(app), path] if sys.platform == "darwin" and app else [mscore, path]
    elif sys.platform == "darwin":
        cmd = ["open", "-R", path]
    elif sys.platform == "win32":
        cmd = ["explorer", f"/select,{path}"]
    else:
        cmd = ["xdg-open", str(Path(path).parent)]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


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


def result_stem(stem: str) -> str:
    """A sample name check_name accepts: "merkurius-m22-33 (1)" -> "merkurius-m22-33-1"."""
    stem = re.sub(r"-{2,}", "-", re.sub(r"\s*[^\w .-]+\s*", "-", nfc(stem)))
    return stem.strip(" .-")[:100].rstrip(" .-") or "score"


def add_found(paths: list[str]) -> dict:
    """Copy search results into inputs/ and make each one a result, as if it had been uploaded. The
    original is never touched. A name that is taken gets -2, -3, ...; a file that is already in inputs/
    (same name, same bytes) isn't added twice."""
    out: dict = {"added": [], "already": [], "errors": []}
    for path in paths:
        src = Path(path)
        try:
            if not SEARCH.found(path):
                raise ValueError("it isn't in the search results any more; search again")
            data = src.read_bytes()
            ext = ".mxl" if src.suffix.lower() == ".mxl" else ".musicxml"  # a MusicXML .xml is kept as .musicxml
            taken = {nfc(p.stem) for p in INPUTS.iterdir() if p.suffix.lower() in ALLOWED_EXTS}
            taken |= {r["output_name"] for r in read_results()}
            base, n = result_stem(src.stem), 1
            while True:
                stem = base if n == 1 else f"{base}-{n}"
                if (INPUTS / f"{stem}{ext}").is_file() and (INPUTS / f"{stem}{ext}").read_bytes() == data:
                    out["already"].append(stem)
                    break
                if stem not in taken:
                    out["added"].append(import_upload(f"{stem}{ext}", data)["output_name"])
                    break
                n += 1
        except (ValueError, OSError) as e:
            out["errors"].append({"path": path, "error": f"{src.name}: {e}"})
    return out


def source_xml(kind: str, name: str) -> Path:
    """The MusicXML of a result ("result") or a full score ("score")."""
    if kind == "result":
        output = next((r["output"] for r in read_results() if r["output_name"] == nfc(name)), "")
        xml = ROOT / output if output else None
    elif kind == "score":
        xml = next((p for p in SCORES.glob("*.musicxml") if nfc(p.stem) == nfc(name)), None)
    else:
        raise ValueError(f"unknown kind {kind!r}")
    if xml is None:
        raise ValueError(f"no MusicXML for {name}")
    return xml


def export_file(kind: str, name: str, fmt: str) -> dict:
    """Export a result or a full score to exports/<name>.pdf|.mid."""
    sync()
    return {"file": str(export(source_xml(kind, name), fmt).relative_to(ROOT))}


def open_file(kind: str, name: str) -> dict:
    """Open a result or a full score in MuseScore Studio, as its MuseScore file (see edits.py)."""
    sync()  # so a stale copy isn't reopened over edits saved since
    return {"mscz": str(open_in_musescore(source_xml(kind, name)).relative_to(ROOT))}


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
        if path == "/api/search":
            since = parse_qs(urlparse(self.path).query).get("since", ["0"])[0]
            return self.send_json(SEARCH.snapshot(int(since) if since.isdigit() else 0))
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
            if path == "/api/sync":
                return self.send_json({"synced": sync()})
            if path == "/api/open":
                return self.send_json(open_file(body["kind"], body["name"]))
            if path == "/api/rename":
                sync()
                names = rename_sample(ROOT, body["old"], body["new"])
                state = read_state()
                for key in ("order", "exclude"):
                    state[key] = [names.get(nfc(n), n) for n in state[key]]
                write_state(state)
                return self.send_json(names)
            if path == "/api/combine":
                return self.send_json(combine(body.get("name", ""), body.get("samples", [])))
            if path == "/api/rename-score":
                sync()
                return self.send_json({"name": rename_score(SCORES, body["old"], body["new"])})
            if path == "/api/export":
                return self.send_json(export_file(body["kind"], body["name"], body["format"]))
            if path == "/api/search":
                SEARCH.start()
                return self.send(200, "ok")
            if path == "/api/search/stop":
                SEARCH.stop()
                return self.send(200, "ok")
            if path == "/api/found/add":
                return self.send_json(add_found(body["paths"]))
            if path in ("/api/found/reveal", "/api/found/open"):
                launch_found(path.removeprefix("/api/found/"), body["path"])
                return self.send(200, "ok")
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
