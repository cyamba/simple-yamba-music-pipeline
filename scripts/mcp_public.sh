#!/usr/bin/env bash
# Put the MCP server on a public HTTPS URL for remote connectors (ChatGPT, claude.ai): a cloudflared quick
# tunnel to the server's HTTP transport, behind a random secret path. Ctrl+C stops both.
#
# Usage:  scripts/mcp_public.sh [PORT]        (default 8790)
#
# The port must be free: a tunnel to a port something else listens on (e.g. ui.py) would publish that instead.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${1:-8790}

command -v cloudflared >/dev/null || { echo "cloudflared isn't installed: brew install cloudflared" >&2; exit 1; }
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is in use ($(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | awk 'NR==2 {print $1, $2}')); pick another:" \
       "scripts/mcp_public.sh <port>" >&2
  exit 1
fi
LOGS=$(mktemp -d)
TOKEN=${YAMBA_MCP_TOKEN:-$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")}
trap 'kill $(jobs -p) 2>/dev/null; wait 2>/dev/null; rm -rf "$LOGS"' EXIT

cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOGS/tunnel.log" 2>&1 &
HOST=""
for _ in $(seq 60); do
  HOST=$(grep -o '[a-z0-9-]*\.trycloudflare\.com' "$LOGS/tunnel.log" | head -1 || true)
  [ -n "$HOST" ] && break
  sleep 1
done
[ -n "$HOST" ] || { cat "$LOGS/tunnel.log" >&2; echo "cloudflared gave no URL" >&2; exit 1; }

YAMBA_MCP_TOKEN=$TOKEN YAMBA_MCP_ALLOWED_HOSTS=$HOST YAMBA_MCP_PUBLIC_URL="https://$HOST" \
  uv run python scripts/mcp_server.py --transport streamable-http --port "$PORT" &
for _ in $(seq 60); do  # the server answers locally, then through the tunnel once its DNS name resolves
  [ "$(curl -s --max-time 5 "https://$HOST/healthz" || true)" = "yamba-music ok" ] && break
  sleep 2
done
[ "$(curl -s --max-time 5 "https://$HOST/healthz" || true)" = "yamba-music ok" ] \
  || echo "warning: https://$HOST doesn't answer as yamba-music yet" >&2

echo
echo "MCP connector URL (no authentication):  https://$HOST/$TOKEN/mcp"
echo "Anyone with that URL can use the tools while this runs. Ctrl+C stops the server and the tunnel."
wait
