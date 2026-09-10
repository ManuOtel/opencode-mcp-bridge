# opencode-mcp-bridge

A coordinator-facing MCP server for a self-hosted
[`OpenCode`](https://opencode.ai) instance.

A host harness (Codex, Claude Code, Cursor, or any MCP-capable client)
delegates repository or system work to an OpenCode worker on another
machine. The host model scopes the task, coordinates the worker, and
verifies the result. The bridge speaks MCP over Streamable HTTP with
Bearer authentication (remote HTTP only; there is no local stdio
transport). It coordinates OpenCode workers; it does not replace
OpenCode.

This is not a hosted OpenCode service. Each user provides an OpenCode
server, or uses one they control, plus their own bridge deployment and
token. Placeholder and demo URLs in this repo are not usable servers.

## Section map

1. [Quick start (60 seconds)](#quick-start-60-seconds)
2. [Endpoints](#endpoints)
3. [Harness setup](#harness-setup)
4. [Worker workflow](#worker-workflow)
5. [Tools](#tools)
6. [Security](#security)
7. [Local deployment](#local-deployment)
8. [Contributor workflow](#contributor-workflow)
9. [Community and license](#community-and-license)

## Quick start (60 seconds)

You need your own bridge deployment ([Local deployment](#local-deployment))
and its Bearer token. Keep the token in environment variables. Never
paste a real token into a file, a chat log, or a commit.

```bash
export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
```

Replace `<your-domain>` with your bridge host and `<paste-token-here>`
with the value of `MCP_BEARER_TOKEN` on that host. Then register the
transport in your harness (see [Harness setup](#harness-setup)).

Rules for every example in this file:

- `https://<your-domain>/worker-mcp` is the safe default. It exposes
  exactly five worker tools and never includes `exec_run`.
- `https://<your-domain>/mcp` exposes the full legacy catalog, including
  `exec_run` when the operator enables it. Use it only for legacy clients.
- `https://YOUR-BRIDGE-HOST/worker-mcp` (as shipped in `.mcp.json`) and
  any demo URL are placeholders. They fail loudly by design. Always
  register your own URL per machine.
- Generate a fresh token with
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.

The helper `./scripts/install-client.sh --help` registers Codex or Claude
Code transports from these variables. It requires both variables and
fails clearly when either is missing or the URL is malformed (it must be
`http(s)://...` ending in `/mcp` or `/worker-mcp`); it never falls back
to anyone else's server. Full Codex and Claude Code steps live in
[docs/client-setup.md](docs/client-setup.md). Copilot-family products
have their own guide at [docs/copilot-setup.md](docs/copilot-setup.md).
For the public registry metadata and publication checklist, see
[docs/registry.md](docs/registry.md). The registry entry describes the
software; it never supplies a hosted bridge or access token.

## Endpoints

Two Streamable HTTP endpoints share one Bearer token.
`GET /health` is the only unauthenticated endpoint. Remote HTTP only;
there is no local stdio command.

| Endpoint | Tools | Use |
| --- | --- | --- |
| `/worker-mcp` | Exactly five: `worker_catalog`, `worker_run`, `worker_status`, `worker_verify`, `worker_cleanup` | Default for all new clients. Least privilege; no shell. |
| `/mcp` | Full 16-tool catalog: the five worker tools plus `list_*`, session tools, `get_diff`, `exec_run` | Legacy clients only. `exec_run` stays listed but fails closed unless `ENABLE_EXEC_RUN=true`. |
| `/health` | None (open) | Reverse-proxy checks. |

There is no global tool-profile switch. Both endpoints are always served
from the same process.

## Harness setup

Compatibility is protocol-level (MCP over Streamable HTTP with a Bearer
header) unless an end-to-end test is documented in this repo. Client
config keys differ per product; confirm key names in the linked official
docs before pasting.

| Harness | How to connect | Status |
| --- | --- | --- |
| OpenAI Codex CLI | `codex mcp add` with `--bearer-token-env-var` | Protocol-level, syntax from official docs |
| Claude Code | `claude mcp add --transport http` or `opencode-worker` plugin | Protocol-level, syntax from official docs |
| ChatGPT Developer Mode | Remote MCP connector, URL mode + Bearer token | Protocol-level; needs an eligible plan and workspace, plus admin approval where required |
| Cursor | Project `.cursor/mcp.json`, `url` + `headers` | Protocol-level |
| Gemini CLI | `~/.gemini/settings.json`, `httpUrl` + `headers` | Protocol-level |
| Windsurf | `~/.codeium/windsurf/mcp_config.json`, `serverUrl` + `headers` | Protocol-level, key names from official docs |
| Cline | `cline_mcp_settings.json`, `type: streamableHttp` + `url` + `headers` | Protocol-level, key names from official docs |
| Roo Code | `mcpServers` entry, `url` + `Authorization` header | Protocol-level, client-specific shape |
| Pi | `pi-mcp-adapter`, shared `~/.config/mcp/mcp.json` | Protocol-level, syntax from official docs |
| Hermes Agent | YAML `mcp_servers` entry + `tools.include` | Protocol-level, syntax from official docs |
| GitHub Copilot / Copilot Studio / M365 Copilot | See [docs/copilot-setup.md](docs/copilot-setup.md) | Separate guide, three distinct cases |
| MCP Inspector | Streamable HTTP transport + `Authorization` header | Debugging only |

The safe pattern in every client-specific block below: URL
`https://<your-domain>/worker-mcp`, header
`Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}`, tools
`worker_catalog`, `worker_run`, `worker_status`, `worker_verify`,
`worker_cleanup`.

### OpenAI Codex CLI

```bash
codex mcp add opencode --url "$OPENCODE_MCP_URL" --bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN
```

Codex reads the token from the environment at request time. Codex plugin
bundles do not interpolate environment variables in the server URL, so
register the transport per machine with your concrete URL. There is also
an `opencode-worker` plugin with worker skills, installed from a Git
marketplace pinned at `v0.2.0`:

```bash
codex plugin marketplace add ManuOtel/opencode-mcp-bridge --ref v0.2.0
```

Then install `opencode-worker` from that marketplace and register your
own transport as above (required: the bundled placeholder URL is not
usable). Details: [docs/client-setup.md](docs/client-setup.md) sections
2 and 6. Official docs:
https://developers.openai.com/codex/cli/reference

### Claude Code

Preferred transport (no skills): a project `.mcp.json` entry. Claude
Code expands `${VAR}` references in `url` and `headers` at load time,
so the token stays in the environment and out of the file:

```json
{
  "mcpServers": {
    "opencode": {
      "type": "http",
      "url": "${OPENCODE_MCP_URL}",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

CLI alternative (transport only, no skills). Double quotes let the shell
expand the token before Claude Code sees it:

```bash
claude mcp add --transport http opencode "$OPENCODE_MCP_URL" --header "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN"
```

Warning: `claude mcp add` writes the resolved header into its local MCP
config, which can persist the token on disk. Prefer the `.mcp.json`
form above on shared hosts, and rotate the token if a config file
leaks.

Recommended path: the `opencode-worker` plugin from this repo's Claude
marketplace. It bundles the MCP transport
(URL `${OPENCODE_MCP_URL}`, token `${OPENCODE_MCP_BEARER_TOKEN}`) plus
the `coordinate-opencode-worker` skill. Export both variables before
installing:

```bash
claude plugin marketplace add ManuOtel/opencode-mcp-bridge
claude plugin install opencode-worker@opencode-mcp-bridge
```

There is no npm or Brew package; both marketplaces install from this Git
repo. Details: [docs/client-setup.md](docs/client-setup.md) sections 3
and 7. Official docs: https://docs.anthropic.com/en/docs/claude-code/mcp

### ChatGPT Developer Mode

Developer Mode ON, then Connectors, Create connector, URL mode with
`https://<your-domain>/worker-mcp` plus your Bearer token, then Scan
Tools. Select `https://<your-domain>/mcp` only when you explicitly need
the full legacy catalog or `exec_run`.
Remote MCP connectors need an eligible plan and workspace, and may need
admin approval. Availability depends on your account, not on this repo.

### Cursor

Add to `.cursor/mcp.json` in your project (key names per
https://cursor.com/docs/context/mcp):

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "url": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

### Gemini CLI

Add to `~/.gemini/settings.json` (key names per
https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html):

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "httpUrl": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

### Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`. Windsurf uses `serverUrl`
(not `url`) for remote servers and supports `${env:VAR}` interpolation
in `headers`. Official docs: https://docs.windsurf.com/windsurf/cascade/mcp

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "serverUrl": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${env:OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

Refresh the server list in Cascade after saving.

### Cline

Open MCP Servers, Configure tab, Configure MCP Servers
(`cline_mcp_settings.json`), or use the Remote Servers tab with
Transport Type Streamable HTTP. Official docs:
https://docs.cline.bot/mcp/mcp-overview

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "type": "streamableHttp",
      "url": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

Set `"type": "streamableHttp"` explicitly. Omitting it falls back to
legacy SSE transport.

### Roo Code

Client-specific shape; confirm key names in the Roo Code docs for your
version. Minimal standard form:

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "url": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

### Pi

Install the adapter, then add the bridge to the shared
`~/.config/mcp/mcp.json` (key names per
https://pi.dev/packages/pi-mcp-adapter):

```bash
pi install npm:pi-mcp-adapter
```

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "url": "https://<your-domain>/worker-mcp",
      "auth": "bearer",
      "bearerTokenEnv": "OPENCODE_MCP_BEARER_TOKEN",
      "includeTools": ["worker_catalog", "worker_run", "worker_status", "worker_verify", "worker_cleanup"],
      "lifecycle": "lazy"
    }
  }
}
```

The token stays in `OPENCODE_MCP_BEARER_TOKEN`; only the variable name
is stored in the file. Servers are lazy by default and connect on first
tool call. There is no one-click plugin for this bridge; do not claim
one. Adapter version and current syntax:
https://pi.dev/packages/pi-mcp-adapter

### Hermes Agent

Hermes uses YAML `mcp_servers` entries (key names per
https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference
and https://github.com/hermes-agent-org/hermes/blob/main/website/docs/guides/use-mcp-with-hermes.md):

```yaml
mcp_servers:
  opencode-bridge:
    url: "https://<your-domain>/worker-mcp"
    headers:
      Authorization: "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
    tools:
      include: [worker_catalog, worker_run, worker_status, worker_verify, worker_cleanup]
      resources: false
      prompts: false
```

Hermes resolves `${VAR}` (or `${env:VAR}`) references from its active
profile secret scope, falling back to the process environment. Put the
token in `~/.hermes/.env`; an unset variable keeps its literal
placeholder. Reload servers with `/reload-mcp` after changing config.

### MCP Inspector (debugging)

```bash
npx @modelcontextprotocol/inspector
```

Select Streamable HTTP transport, enter
`https://<your-domain>/worker-mcp`, and add the `Authorization: Bearer`
header in the Inspector UI. Never use a real token on a machine you do
not control. Docs: https://github.com/modelcontextprotocol/inspector

You can also smoke-test the deployment without a client:
`./scripts/smoke.sh` (see the script header).

## Worker workflow

Lifecycle, in order. There is no `worker_wait` tool; poll instead.

```text
worker_catalog()
worker_run(message="Implement X in /path/to/repo", directory="/path/to/repo", title="feat-x")
worker_status(taskID="<taskID>", directory="/path/to/repo")  # repeat until idle
worker_verify(taskID="<taskID>", directory="/path/to/repo")
worker_cleanup(taskID="<taskID>", directory="/path/to/repo")
```

1. Pick a model: `worker_catalog` (free and connected only by default).
   Default model is `opencode/muse-spark-1.3-contributor-free`. No paid
   models unless explicitly requested for that task. Ordered fallback:
   free first, then paid `opencode-go/muse-spark-1.3-contributor`
   ("Muse Spark 1.3 Contributor") from `worker_catalog.recommendations[1]`.
   Paid use must be intentional: pass `providerID`/`modelID` explicitly
   only when the boss asked for paid for that task. The bridge never
   auto-selects paid.
2. Launch: `worker_run` with `message`, `directory`, `title`, and
   optional `requestID` for safe retries. Save `taskID` and `directory`.
3. Poll: `worker_status` with the same `taskID` and `directory` until
   `idle`. States: `running` (wait), `idle` (verify), `error`/`unknown`
   (recover, see `skills/recover-opencode-task/SKILL.md`).
4. Verify: call `worker_verify`, then inspect the exact diff and run
   tests and lint with the host's own tools. Never trust a worker
   summary alone.
5. Clean up: `worker_cleanup` (`action=abort` stops, `action=delete`
   removes) when done.

Status and messages are directory-scoped: always pass the `directory`
returned by `worker_run` when it differs from the server default, or
status reads `unknown`. When omitted, `worker_status` and
`worker_verify` recover the saved directory from the durable task
registry (`TASK_STATE_PATH`). Tasks are idempotent by `requestID`: same
ID plus same inputs returns the existing task with `deduplicated=true`;
conflicting reuse fails before side effects.

The plugin skills enforce this workflow: Codex
(`delegate-to-opencode`, then `verify-opencode-work`, on failure
`recover-opencode-task`) and Claude Code
(`coordinate-opencode-worker`). Code changes follow
`opencode-git-workflow`.

## Tools

Full signatures: [docs/tool-api.md](docs/tool-api.md).

Worker tools (also the full `/worker-mcp` catalog):

| Tool | What it does |
| --- | --- |
| `worker_run` | Start a background worker. Returns `taskID` (= session ID), state, model, directory, title, `requestID`, `deduplicated`. Prompts before running. |
| `worker_status` | Poll state (`running`/`idle`/`error`/`unknown`) plus latest assistant text only, with truncation counts. Read-only. |
| `worker_catalog` | List models, free and connected only by default, with bridge defaults and ordered `recommendations` (free first, paid fallback second). Read-only. |
| `worker_verify` | Re-check a finished worker (state plus read-only git evidence). Read-only. |
| `worker_cleanup` | Abort (`action=abort`) or delete (`action=delete`) a worker session. Prompts before running. |

Legacy tools (`/mcp` only, advanced compatibility):

`list_providers`, `list_agents`, `create_session`, `send_message`,
`list_sessions`, `get_session`, `list_messages`, `abort_session`,
`delete_session`, `get_diff`, `exec_run` (raw shell, opt-in via
`ENABLE_EXEC_RUN=true`, disabled by default).

Compatibility notes: `send_message` accepts `message`; `prompt` remains
an alias (supply exactly one). `providerID`/`modelID` must be given
together or omitted; when omitted the bridge uses its configured
default. `worker_catalog` filters (`free_only`, `connected_only` default
true, `limit` default 20, cap 100) apply to `models`/`total` only;
`recommendations` is always two entries (free default rank 1, paid
`opencode-go/muse-spark-1.3-contributor` rank 2) so clients can discover
the fallback when the free model is unavailable. `abort_session`, `delete_session`, and `get_diff` are the
full-profile equivalents of `worker_cleanup` and `worker_verify`;
prefer the worker tools.

Per-tool approval ships in `.mcp.json`: `worker_run` and
`worker_cleanup` prompt; `worker_status`, `worker_catalog`, and
`worker_verify` auto-approve. If your client ignores that file, enforce
the same policy in the client config.

## Security

- Treat `MCP_BEARER_TOKEN` like a root password: long random value,
  rotate on leak, never commit `.env` or tokens. Generic install steps
  never point at another person's server.
- Use `/worker-mcp` for least privilege. It never exposes `exec_run`,
  so a leaked token cannot become a direct shell.
- Do not expose `/mcp` or set `ENABLE_EXEC_RUN=true` on an untrusted
  deployment. When enabled, plus open directories, anyone with the
  Bearer token has a shell where the bridge runs. Prefer session tools
  for code edits; reserve `exec_run` for system ops.
- Token rotation (zero downtime): `MCP_BEARER_TOKEN_SECONDARY` accepts
  one extra token during overlap. Steps: 1) generate a new token,
  2) set it as `MCP_BEARER_TOKEN_SECONDARY` and restart or reload the
  bridge, 3) move clients to the new token, 4) promote it to
  `MCP_BEARER_TOKEN`, unset the secondary, restart. Blank or duplicate
  secondary values fail startup closed. Comparison is constant-time and
  token values are never logged.
- `/health` is the only unauthenticated endpoint. Everything under
  `/mcp` and `/worker-mcp` requires the Bearer token.
- Request-body limit: `MCP_MAX_BODY_BYTES` (default 1048576, 1 MiB) caps
  the declared `Content-Length` and the actual streamed body on `/mcp`
  and `/worker-mcp`. Oversized requests get a generic 413 before any
  tool runs. Auth still runs first, so missing tokens stay 401.
- Browser-origin allowlist (optional): `MCP_ALLOWED_ORIGINS` is a
  comma-separated exact-origin list (`scheme://host[:port]`, http/https,
  no path/query/fragment) for `/mcp` and `/worker-mcp`. Unset or blank
  means no origin policy. Absent `Origin` and `Referer` stays allowed
  for CLI/SDK clients. Auth runs first (missing tokens stay 401),
  `/health` never checks origins, and rejections are a generic 403 with
  no secret or header echo.

## Local deployment

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/), plus a
running `opencode serve` or `opencode web` (see
[OpenCode server docs](https://opencode.ai/docs/server/)).

```bash
git clone https://github.com/ManuOtel/opencode-mcp-bridge.git
cd opencode-mcp-bridge
uv sync
cp .env.example .env
# edit .env: OpenCode credentials + a fresh MCP_BEARER_TOKEN
uv run python -m opencode_mcp_bridge.server
```

Check it: `curl http://127.0.0.1:8087/health` should report OpenCode
healthy. `POST /mcp` and `POST /worker-mcp` without a Bearer token must
return 401.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENCODE_BASE_URL` | `http://127.0.0.1:4096` | OpenCode server URL. |
| `OPENCODE_SERVER_USERNAME` | `opencode` | Basic auth user for OpenCode. |
| `OPENCODE_SERVER_PASSWORD` | (required) | Basic auth password of your OpenCode server. |
| `MCP_BEARER_TOKEN` | (required) | Static token clients send as `Authorization: Bearer <token>`. |
| `MCP_BEARER_TOKEN_SECONDARY` | (unset) | Overlap token for rotation; unset means single-token mode. |
| `MCP_HOST` | `127.0.0.1` | Bridge listen address. Use a host IP reachable from your reverse proxy when proxying from Docker. |
| `MCP_PORT` | `8087` | Bridge listen port. |
| `DEFAULT_DIRECTORY` | `$HOME` | Working directory for sessions when clients omit it. |
| `DEFAULT_PROVIDER_ID` | `opencode` | Default provider. |
| `DEFAULT_MODEL_ID` | `muse-spark-1.3-contributor-free` | Default model. |
| `EXEC_TIMEOUT_S` | `120` | Cap for `exec_run` timeouts. |
| `EXEC_MAX_OUTPUT_CHARS` | `20000` | Output truncation cap for `exec_run`. |
| `ENABLE_EXEC_RUN` | `false` | Opt-in for `exec_run` on `/mcp`. Set `true` only where a shell is intended. |
| `TASK_STATE_PATH` | `/var/lib/opencode-mcp-bridge/tasks.json` | JSON registry for durable tasks (atomic writes, bounded records, no prompts or secrets). |
| `MCP_MAX_BODY_BYTES` | `1048576` | Max request body (bytes) for `/mcp` and `/worker-mcp`, declared and streamed; oversized returns generic 413. |
| `MCP_ALLOWED_ORIGINS` | (unset) | Optional exact-origin allowlist for `/mcp` and `/worker-mcp`; unset/blank disables. Single trailing slash stripped. |

Put a reverse proxy with TLS in front. Traefik example:
`deploy/traefik-opencode-mcp.yaml`. Host systemd keeps full terminal
access for `exec_run` (see `deploy/opencode-mcp-bridge.service`, env
file `0640`); Docker scopes `exec_run` to the container
(`docker compose up -d` after filling `.env`). For clean release,
pre/post-deploy checks, rotation, rollback, and log steps, follow
[docs/operations.md](docs/operations.md).

## Contributor workflow

Read [AGENTS.md](AGENTS.md) first: ownership boundaries, edit
discipline, free-model policy, test commands, secrets, worktree and
commit rules, and reporting. The worker playbook lives in `skills/`
(`delegate-to-opencode`, `verify-opencode-work`,
`recover-opencode-task`, `opencode-git-workflow`). Planned work lives in
[docs/roadmap.md](docs/roadmap.md); read phases in order and do not skip
a gate.

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

CI (`.github/workflows/ci.yml`) runs the same checks on pull requests
and pushes to master across Python 3.11, 3.12, and 3.13, plus JSON
validation of the Codex and Claude plugin manifests and a no-push
Docker build. `ruff format` in write mode touches Python files: use
`--check` only and report failures instead of fixing them here.

## Community and license

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before you change code or docs.
- Obey the [Code of Conduct](CODE_OF_CONDUCT.md) in all project spaces.
- Report security faults in private per [SECURITY.md](SECURITY.md).
- Open a [bug report or feature
  request](https://github.com/ManuOtel/opencode-mcp-bridge/issues/new/choose)
  or read [open
  issues](https://github.com/ManuOtel/opencode-mcp-bridge/issues).
- Open [pull
  requests](https://github.com/ManuOtel/opencode-mcp-bridge/pulls) from
  a feature branch, never directly from `master`.

License: PolyForm Noncommercial 1.0.0 - free for noncommercial use and
modification, commercial use needs permission. See [LICENSE.md](LICENSE.md).
For a commercial license, reach out: manuotel@gmail.com
