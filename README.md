# opencode-mcp-bridge

MCP bridge exposing a local `opencode serve`/`opencode web` instance to ChatGPT
via Streamable HTTP (`POST https://opencode-mcp.manuotel.com/mcp`).

Full-access build: ChatGPT can work in any directory and run terminal commands.
Protected by a static Bearer token on the MCP side and Basic auth to opencode.

## Tools

| Tool | What it does |
| --- | --- |
| `list_providers` | Providers + model IDs + connected status. Call first (model picker). |
| `list_agents` | Available agents (plan, build, ...). |
| `create_session` | New session, optional title/directory/agent/providerID/modelID. |
| `send_message` | Prompt a session, wait for the reply. Optional model/agent override. |
| `list_sessions` | Recent sessions. |
| `get_session` | One session by ID. |
| `list_messages` | Messages in a session. |
| `abort_session` | Abort a running session. |
| `delete_session` | Delete a session and its data (clean up tests). |
| `get_diff` | File diffs from a session. |
| `exec_run` | Raw shell on the server. No sandbox. Prefer sessions for code edits. |

If `providerID`/`modelID` are omitted, opencode uses its default model.

## Security warning

`exec_run` plus open directories means anyone with the Bearer token has full
shell on the box. Treat `MCP_BEARER_TOKEN` like a root password: long random
value, rotate on leak, never commit `.env`.

## Deploy option A: host systemd (recommended, full terminal access)

Bridge runs on the host like `opencode-web.service`, so `exec_run` is real
host shell and `http://10.0.1.1:4096` is directly reachable.

```bash
sudo mkdir -p /opt/opencode-mcp-bridge /etc/opencode-mcp-bridge
sudo git clone git@github-personal:ManuOtel/opencode-mcp-bridge.git /opt/opencode-mcp-bridge
cd /opt/opencode-mcp-bridge
sudo /root/.local/bin/uv sync --frozen --no-dev
# write /etc/opencode-mcp-bridge/env (mode 600) from .env.example:
# OPENCODE_BASE_URL=http://10.0.1.1:4096
# OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD from /etc/opencode-server/env
# MCP_BEARER_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
sudo cp deploy/opencode-mcp-bridge.service /etc/systemd/system/
sudo cp deploy/traefik-opencode-mcp.yaml /data/coolify/proxy/dynamic/opencode-mcp.yaml
sudo systemctl daemon-reload && sudo systemctl enable --now opencode-mcp-bridge
curl -s http://127.0.0.1:8087/health
```

Then add the Cloudflare Tunnel public hostname
`opencode-mcp.manuotel.com` pointing at the Traefik HTTP origin (same origin
as `opencode.manuotel.com`).

## Deploy option B: Coolify (container-scoped exec)

Deploy this repo as a Coolify app, set the env vars from `.env.example` in the
Coolify UI, domain `opencode-mcp.manuotel.com`. Note: in Docker, `exec_run`
runs inside the container, not on the host. Opencode tools work the same.

## ChatGPT wiring

1. ChatGPT > Developer Mode ON > Connectors > Create connector.
2. Tunnel off, URL mode, URL `https://opencode-mcp.manuotel.com/mcp`,
   auth header `Authorization: Bearer <MCP_BEARER_TOKEN>`.
3. Scan tools. Test read first: "list providers, then list sessions".
4. Test write second: "create a session titled chatgpt-test and send hello".

## Dev

```bash
uv sync
uv run ruff format src tests
uv run ruff check src tests
uv run pytest
MCP_BEARER_TOKEN=... BASE=http://127.0.0.1:8087 ./scripts/smoke.sh
```
