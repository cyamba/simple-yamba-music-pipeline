# The pipeline as an MCP server

`scripts/mcp_server.py` lets an agent such as Claude or ChatGPT use the pipeline: add scores, transcribe
them with HOMR, read the notes, arrange and combine results into a full score, export PDF/MIDI and open
things in MuseScore Studio. The tools wrap the same functions as the UI (`ui.py`), so the agent and the UI
always agree.

It runs on your Mac, next to the data. The server supports two transports:

| Transport | Command | For |
|---|---|---|
| **stdio** (default) | `uv run python scripts/mcp_server.py` | Claude Code, Claude Desktop, Codex CLI, Gemini CLI: anything that starts the server itself |
| **streamable HTTP** | `uv run python scripts/mcp_server.py --transport streamable-http` (port 8790, path `/mcp`) | ChatGPT, claude.ai and other remote connectors: these need a public HTTPS URL |

Check that the server starts: `uv run python scripts/mcp_server.py --list-tools`.

## Connecting a client

### Claude Code

The repo's `.mcp.json` registers the server as `yamba-music`. Open the repo in Claude Code and approve it
once. `claude mcp list` should then show it as connected. To use it from every project:

```bash
claude mcp add --scope user yamba-music -- uv run --directory <repo> python scripts/mcp_server.py
```

### Claude Desktop

Add an entry to `~/Library/Application Support/Claude/claude_desktop_config.json` and restart the app. Use
the absolute path to `uv`, because Desktop doesn't use your shell's PATH:

```json
{
  "mcpServers": {
    "yamba-music": {
      "command": "/Users/<you>/.local/bin/uv",
      "args": ["run", "--directory", "<repo>", "python", "scripts/mcp_server.py"]
    }
  }
}
```

Desktop logs the server's traffic to `~/Library/Logs/Claude/mcp-server-yamba-music.log`.

### ChatGPT (and claude.ai)

Remote connectors can only reach a public HTTPS URL. This command starts the HTTP server and a
cloudflared quick tunnel together:

```bash
scripts/mcp_public.sh            # needs: brew install cloudflared
# MCP connector URL (no authentication):  https://<random>.trycloudflare.com/<secret>/mcp
```

It does three things:
- **Checks the port first.** It refuses a port something else is listening on, such as a `ui.py` started
  with `--port`. Otherwise the tunnel would publish that program instead.
- **Puts everything under a random secret path.** ChatGPT connectors offer only OAuth or no
  authentication, so the hard-to-guess URL is the protection.
- **Stops cleanly.** Ctrl+C stops both the server and the tunnel. The URL changes every run, so you
  update the connector each time.

**In ChatGPT:**
1. Go to **Plugins → Add → Add custom MCP server**.
2. Fill in:
   - Name: `Yamba Music`
   - Connection: **Server URL**, the printed URL
   - Authentication: **No authentication**
3. Tick the risk acknowledgement, click **Create as a plugin**, then **Connect**.
4. In a chat, pick it under **Plugins** (it shows as `@Yamba Music`).

**In claude.ai:** go to **Settings → Connectors → Add custom connector**, paste the same URL, and choose
no authentication.

When the server runs over HTTP, tools that make files (export, combine, import, play_midi) also return a
`url`. ChatGPT turns these into download links. They are served from `/<secret>/files/...`, which only
covers the folders the UI serves. A plain local HTTP server (no tunnel) links to `http://127.0.0.1:8790`.

After you add or change tools, open the plugin in ChatGPT and press **Refresh** so it sees them.

### Codex (CLI or the ChatGPT desktop app)

Codex starts stdio servers itself, so it needs no tunnel. Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.yamba-music]
command = "uv"
args = ["run", "--directory", "<repo>", "python", "scripts/mcp_server.py"]
```

### Any other MCP client

Use stdio with the command above, or HTTP at `http://127.0.0.1:8790/mcp`. Settings for the HTTP
transport come from the environment; `mcp_public.sh` sets all three for you:

| Variable | Meaning |
|---|---|
| `YAMBA_MCP_ALLOWED_HOSTS` | Public host names to accept besides localhost, comma-separated. Others get a 421 (DNS-rebinding protection). |
| `YAMBA_MCP_TOKEN` | Serve everything under `/<token>/`. |
| `YAMBA_MCP_PUBLIC_URL` | Base URL used to build download links. |

The server works with clients that use the `initialize` handshake (protocol 2025-11-25 and earlier) and
with the 2026-07-28 protocol.

## Tools

| Tool | What it does |
|---|---|
| `get_library` | Inputs, results (status, whether each goes into the full score), full scores, exports, which tools are installed. Call this first. |
| `get_music` | The notes of a result or full score for chosen measures, as compact tokens (`C4+E4:8 r:4`), plus key, time signatures, clefs and range. Can also return the raw MusicXML. |
| `search` / `fetch` | Look up results and full scores by text. Follows ChatGPT's search/fetch convention. |
| `import_musicxml` | Add MusicXML that the agent wrote, fixed or found as a new result. Refuses to overwrite unless `replace=true`. |
| `add_input` / `delete_input` | Add a scan, PDF or MusicXML file to `inputs/` (base64 or an http(s) URL), or delete one. |
| `transcribe` / `get_job` | Run HOMR on one input or all of them in the background, then wait or poll for the result. |
| `start_computer_search` / `get_computer_search` / `stop_computer_search` / `add_found_files` | Find MusicXML files on this Mac and add the chosen ones as results. |
| `arrange_full_score` / `combine_score` | Set the full score's order, exclusions and name, then concatenate it and render its PDF. |
| `rename` | Rename a result (including all its files) or a full score. |
| `export` | Write a PDF or MIDI to `exports/`. |
| `play_midi` | Play a result or full score in a MIDI player shown in the chat, with a link to the same player as a page. Exports the MIDI first when there is none yet, or the music changed since. |
| `open_in_musescore` / `sync_musescore_edits` | Open a result or full score in MuseScore Studio on this Mac. Edits you save there come back. |
| `create_piano_roll` / `open_piano_roll` | Make a piano roll (an editable score) from scratch, a result, a full score or a MIDI file, or open one. Shows the editor in the chat, and returns its page link. |
| `get_piano_roll` | A roll's parts, time, key, tempo and grid, and the notes of chosen measures as one stream per part (`D4:8 F4+A4:4 r:8`). |
| `edit_piano_roll` | A sequence of edits in one go, all or none (see below), with a preview of the changed measures. |
| `add_notes`, `change_notes`, `delete_notes`, `transpose_notes`, `mirror_notes`, `retrograde_notes`, `scale_time` | The same edits one at a time. |
| `piano_roll_to_result` / `revert_piano_roll` | Turn a roll into a MusicXML result, ready to combine; go back to an earlier revision. |
| `load_piano_roll`, `transform_roll`, `save_piano_roll` | For the editor in the chat only; hidden from the model where the host supports it. |

Each tool declares whether it is read-only or destructive; only `delete_input` is destructive (piano-roll edits
can be undone with `revert_piano_roll`). Clients
use this to decide when to ask before calling it. Mistakes such as an unknown name or a taken name come
back as readable errors that list the valid options.

## The MIDI player

`play_midi` shows the player from `scripts/midi_player.html`. It has a piano-roll scrubber, play/pause,
restart, loop, speed and volume, and plays through a small Web Audio synthesizer in the browser. You get it
in up to three ways:

- **In the chat.** The tool links to an [MCP Apps](https://github.com/modelcontextprotocol/ext-apps) widget,
  `ui://yamba-music/midi-player.html`. Claude (Desktop and claude.ai) and ChatGPT draw it under the answer.
  The MIDI reaches the widget in the result's `_meta`, which the host gives to the widget and not to the
  model.
- **As a page.** Each call writes `exports/<name>.player.html`, the same player with the MIDI built in, next
  to `exports/<name>.mid`. Over HTTP, the result's `player.url` links to it through the server, either on
  port 8790 or through the tunnel. Use this in apps that can't show widgets.
- **As a file.** Over stdio there is no server to link to, so `player.absolute_path` is the page on your
  Mac. It works offline; open it in any browser.

## The piano roll

A piano roll is an editable score in `rolls/<name>.roll.json`, with its MIDI rewritten next to it on every
save. In a roll, each coloured part is one staff, and the parts of one instrument become one MusicXML
part. The piano preset's right and left hands therefore make one part with two staves, like HOMR's
results, so `piano_roll_to_result` gives a result that `combine_score` can concatenate with them.

The editor is `scripts/piano_roll.html`. You can open it in four places:

- **In the chat:** `create_piano_roll` and `open_piano_roll` link to the MCP Apps widget
  `ui://yamba-music/piano-roll.html`. The roll reaches the widget in the result's `_meta`. The widget
  calls `transform_roll` and `save_piano_roll` through the host, and it can ask for full screen.
- **As a page from the UI:** `ui.py` serves it at `/roll?name=<roll>`.
- **As a page over HTTP:** the MCP server serves it at `<prefix>/roll?name=<roll>`, behind the token and
  the tunnel.
- **Over stdio:** the MCP server starts `ui.py`'s handler on `127.0.0.1:8791`, or uses `YAMBA_UI_URL` if it
  is set, and links to that.

How the editor behaves:

- **Writing notes:**
  - Click an empty cell to write a note of the grid's *default* length.
  - Double-click a cell to write a rest.
  - Double-click a note to delete it.
  - Drag a note sideways to change its length by the *min* step, up to *max*. Shortened to nothing,
    the note becomes a rest.
- **Transforms:** mirror (chromatic, or diatonic in a tonic and mode), retrograde,
  retrograde-inversion, transpose, and time × factor. They act on the bars picked on the ruler, or on
  the whole score, and they run on the server through `roll_ops.py`, the same code the agent tools use.
- **Agent edits:** an open editor reloads by itself when an agent saves the roll.

**The edit language.** Agent tools and the editor use the same text forms:

| Form | Example | Meaning |
|---|---|---|
| duration | `8`, `4.`, `2~8` | 1 2 4 8 16 32 64 128 (whole … 128th); `.` dotted; `~` tied |
| position | `3:2`, `3:2.5`, `3:2+16` | measure:beat. A beat is the time signature's denominator, so an eighth in 6/8 |
| stream | `D4:8 F4+A4:4 r:8 \| @1 A3:2` | Notes, chords and rests in order. `@` moves the cursor (a second voice); `\|` checks a barline |
| selection | `{"parts": ["rh"], "measures": "5-6", "at": "3:2", "pitches": "E4"}` | Conditions are ANDed. A note is selected when it starts in the range. Empty means the whole score |

`edit_piano_roll` takes a list of operations, applies them in order, and saves only if all of them work:

- **Notes:** `add`, `add_rest`, `delete`, `change`, `move`, `copy`, `transpose`.
- **Transforms:** `mirror`, `retrograde`, `retrograde_inversion`, `scale_time`.
- **Bars:** `insert_measures`, `delete_measures`, `set_length`.
- **Settings:** `set_time`, `set_key`, `set_tempo`, `set_grid`, `set_title`.
- **Parts:** `add_part`, `update_part`, `remove_part`.
- **Other:** `quantize`.

Details:

- **Errors** name the failing operation and say what is there instead, for example "operation 2 (change):
  nothing matches {"at": "3:2", "pitches": "E4"}; there: F5 at 3:2 (Right hand)".
- **The grid:** edits land on the roll's *min* grid; `set_grid` with a finer `min` allows finer values.
- **`scale_time`** stretches the selection from its start, and later music in the same parts moves along.
- **`set_time`** keeps notes in their measures.
- **Revisions:** each save is a revision, and the last 20 are kept for `revert_piano_roll`.

**What a roll can't hold.**

- **From a score:** only its notes are read. Dynamics, slurs, articulations, lyrics, grace notes and
  repeats are dropped. Tuplets are snapped to the grid, and the count is reported. So converting a roll
  back never replaces the result it came from, unless you ask.
- **Rest markers:** they show intent, and they become rests in the MusicXML. In a MIDI file they are just
  silence.
- **MIDI key signatures:** they say only major or minor, so a roll read from MIDI loses its mode.

## Example goals

Ask for outcomes, not tool names:

- *What's in my music library? Which transcriptions look shaky, and what full scores exist?*
- *Compose a 4-bar C-major waltz for piano, add it as waltz-sketch, and give me a PDF and a MIDI.*
- *Build a full score called Merkurius-v2 from the Merkurius segments in measure order and export a PDF.*
- *Re-run recognition on skanna0523 and tell me whether bar 1 changed.*
- *Find MusicXML files in ~/Downloads and add the Merkurius ones.*
- *Play merkurius-m01-10 for me.*
- *Start a piano roll called canon in D dorian, 3/4: write a 4-bar tune and a bass, then put the tune's
  diatonic retrograde-inversion about A4 in bars 5-8, and add it to the full score.*
- *In merkurius-m01-10's piano roll, bar 3 beat 2: make the E an F, then double the length of bars 5-6.*

## Tested with

All of these were run against this repo on 2026-10-08/09:
- **Claude Code** (headless, `claude -p` with only this server): all six goals above passed. That
  includes a real HOMR run through `transcribe` → `get_job`, and recovering from an unknown score name.
- **Claude Desktop**: connected with the `initialize` handshake and listed the tools.
- **ChatGPT** (GPT-6 Astra, custom MCP plugin through `mcp_public.sh`): it reviewed the library,
  composed and imported a waltz, exported it with working download links, built a 51-measure full score,
  and reported the error for a score that doesn't exist.
- **MCP Python client** over stdio, and over HTTPS in both protocol eras.

## Caveats

- **The URL is the key.** While `mcp_public.sh` runs, anyone with the URL can use the tools: write files
  in the repo's folders, start HOMR, and open MuseScore on your Mac. Stop it when you're done.
- **Don't write from two places at once.** The UI and the MCP server share `review/run-results.csv` and
  `review/full-score.json`, and there is no file lock. Reading from both at the same time is fine.
- **New results go into the full score.** As in the UI, every new result is included unless it is
  excluded with `arrange_full_score`.
