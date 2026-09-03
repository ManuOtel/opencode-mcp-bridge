# opencode-mcp-bridge

MCP bridge for a self-hosted [`opencode`](https://opencode.ai) instance.
It exposes opencode sessions, models, diffs, and server shell access as MCP
tools over Streamable HTTP, so any MCP-compatible harness can drive it:
ChatGPT (developer connectors), Claude Code, Codex, MCP Inspector, and more.

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
| `exec_run` | Raw shell on the bridge host. No sandbox. Prefer sessions for code edits. |

If `providerID`/`modelID` are omitted, opencode uses its default model.

## Quickstart (local)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/), plus a running
`opencode serve` or `opencode web` (see [opencode server docs](https://opencode.ai/docs/server/)).

```bash
git clone https://github.com/ManuOtel/opencode-mcp-bridge.git
cd opencode-mcp-bridge
uv sync
cp .env.example .env
# edit .env: opencode credentials + a fresh MCP_BEARER_TOKEN
uv run python -m opencode_mcp_bridge.server
```

Check it: `curl http://127.0.0.1:8087/health` should report opencode healthy.
`POST /mcp` without a Bearer token must return 401.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENCODE_BASE_URL` | `http://127.0.0.1:4096` | Opencode server URL. |
| `OPENCODE_SERVER_USERNAME` | `opencode` | Basic auth user for opencode. |
| `OPENCODE_SERVER_PASSWORD` | (required) | Basic auth password (`OPENCODE_SERVER_PASSWORD` of your opencode server). |
| `MCP_BEARER_TOKEN` | (required) | Static token clients send as `Authorization: Bearer <token>`. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `MCP_HOST` | `127.0.0.1` | Bridge listen address. Use a host IP reachable from your reverse proxy when proxying from Docker. |
| `MCP_PORT` | `8087` | Bridge listen port. |
| `DEFAULT_DIRECTORY` | `$HOME` | Working directory for opencode sessions when clients omit it. |
| `EXEC_TIMEOUT_S` | `120` | Cap for `exec_run` timeouts. |
| `EXEC_MAX_OUTPUT_CHARS` | `20000` | Output truncation cap for `exec_run`. |

## Exposing it publicly

Put a reverse proxy with TLS in front (`/health` open, everything else
requires the Bearer token - the bridge enforces that itself):

- Traefik: see `deploy/traefik-opencode-mcp.yaml` (file-provider example).
- Caddy: `reverse_proxy /mcp/* localhost:8087` plus `handle /health`.
- Any HTTPS tunnel (Cloudflare Tunnel, Tailscale Serve, ngrok) also works.

## Deploy options

**A. Host systemd (full terminal access).** The bridge runs on the host like
opencode itself, so `exec_run` is a real host shell. See
`deploy/opencode-mcp-bridge.service`. Keep the env file root-only (`0600`).

**B. Docker (container-scoped exec).** `docker compose up -d` after filling
`.env` and setting your domain in `docker-compose.yml`. Opencode tools work
the same, but `exec_run` runs inside the container, not on the host.

## Connect a client

- **Claude Code:** `claude mcp add --transport http opencode-bridge https://<your-domain>/mcp --header "Authorization: Bearer <token>"`
- **ChatGPT:** Developer Mode ON > Connectors > Create connector, tunnel off, URL mode with `https://<your-domain>/mcp` + Bearer token, then Scan Tools.
- **Codex / others:** add an MCP server with the same URL and Bearer header.
- **Debug:** MCP Inspector or `./scripts/smoke.sh` (see script header).

Suggested first tests: "list providers" (read), then "create a session and
send it hello" (write), then "delete that session" (cleanup).

## Security warning

`exec_run` plus open directories means anyone with the Bearer token has a
shell where the bridge runs. Treat `MCP_BEARER_TOKEN` like a root password:
long random value, rotate on leak, never commit `.env`.

## Dev

```bash
uv sync
uv run ruff format src tests
uv run ruff check src tests
uv run pytest
MCP_BEARER_TOKEN=... BASE=http://127.0.0.1:8087 ./scripts/smoke.sh
```

## License

PolyForm Noncommercial 1.0.0 - free for noncommercial use and modification,
commercial use needs permission. See [LICENSE.md](LICENSE.md). For a
commercial license, reach out: manuotel@gmail.com
