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

When the server runs over HTTP, tools that make files (export, combine, import) also return a `url`.
ChatGPT turns these into download links. They are served from `/<secret>/files/...`, which only covers
the folders the UI serves.

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
| `open_in_musescore` / `sync_musescore_edits` | Open a result or full score in MuseScore Studio on this Mac. Edits you save there come back. |

Each tool declares whether it is read-only or destructive; only `delete_input` is destructive. Clients
use this to decide when to ask before calling it. Mistakes such as an unknown name or a taken name come
back as readable errors that list the valid options.

## Example goals

Ask for outcomes, not tool names:

- *What's in my music library? Which transcriptions look shaky, and what full scores exist?*
- *Compose a 4-bar C-major waltz for piano, add it as waltz-sketch, and give me a PDF and a MIDI.*
- *Build a full score called Merkurius-v2 from the Merkurius segments in measure order and export a PDF.*
- *Re-run recognition on skanna0523 and tell me whether bar 1 changed.*
- *Find MusicXML files in ~/Downloads and add the Merkurius ones.*

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
