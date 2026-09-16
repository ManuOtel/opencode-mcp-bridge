<div align="center">
  <img
    src="assets/opencode-mcp-bridge.svg"
    alt="OpenCode MCP Bridge logo"
    width="96"
    height="96" />
  <h1>opencode-mcp-bridge</h1>
  <p>A coordinator-facing MCP server for a self-hosted
  <a href="https://opencode.ai">OpenCode</a> instance (v0.5.1).</p>
  <p><a href="#first-use-60-seconds"><strong>First use in 60
  seconds</strong></a></p>
  <p>Protocol-level compatibility (MCP over Streamable HTTP) - no
  official partnerships; some harnesses are unverified end-to-end, see
  <a href="docs/compatibility.md">docs/compatibility.md</a>:</p>
  <p>
    <a href="https://developers.openai.com/codex/cli/reference"><img
      src="https://img.shields.io/badge/Codex-docs-24292e?logo=openai&amp;logoColor=white"
      alt="Codex docs" /></a>
    <a href="https://docs.anthropic.com/en/docs/claude-code/mcp"><img
      src="https://img.shields.io/badge/Claude_Code-docs-24292e?logo=anthropic&amp;logoColor=white"
      alt="Claude Code docs" /></a>
    <a href="https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta"><img
      src="https://img.shields.io/badge/ChatGPT-docs-24292e?logo=openai&amp;logoColor=white"
      alt="ChatGPT docs" /></a>
    <a href="https://cursor.com/docs/context/mcp"><img
      src="https://img.shields.io/badge/Cursor-docs-24292e?logo=cursor&amp;logoColor=white"
      alt="Cursor docs" /></a>
    <a href="https://code.visualstudio.com/docs/agents/reference/mcp-configuration"><img
      src="https://img.shields.io/badge/VS_Code-docs-24292e?logo=visualstudiocode&amp;logoColor=white"
      alt="VS Code docs" /></a>
    <a href="https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html"><img
      src="https://img.shields.io/badge/Gemini_CLI-docs-24292e?logo=google&amp;logoColor=white"
      alt="Gemini CLI docs" /></a>
    <a href="https://docs.openhands.dev/openhands/usage/cli/mcp-servers"><img
      src="https://img.shields.io/badge/OpenHands-docs-24292e"
      alt="OpenHands docs" /></a>
    <a href="https://pi.dev/packages/pi-mcp-adapter"><img
      src="https://img.shields.io/badge/Pi-docs-24292e"
      alt="Pi docs" /></a>
    <a href="https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference"><img
      src="https://img.shields.io/badge/Hermes-docs-24292e"
      alt="Hermes docs" /></a>
    <a href="https://github.com/modelcontextprotocol/inspector"><img
      src="https://img.shields.io/badge/MCP_Inspector-docs-24292e?logo=github&amp;logoColor=white"
      alt="MCP Inspector docs" /></a>
  </p>
</div>

A host harness (Codex, Claude Code, or any MCP-capable client)
delegates repository or system work to an OpenCode worker on another
machine. The host model scopes the task, coordinates the worker, and
verifies the result. The bridge speaks MCP over Streamable HTTP with
Bearer authentication (remote HTTP only; there is no local stdio
transport). It coordinates OpenCode workers; it does not replace
OpenCode.

Bring your own bridge: you provide an OpenCode server, your own bridge
deployment, your own token, and your own
`https://<your-domain>/worker-mcp`. Generic installs never point at
another person's server. The optional community demo endpoint operated
by ManuOtel at `https://opencode-mcp.manuotel.com/worker-mcp`
(`/worker-mcp` only) is opt-in only, requires its own token, and is not
for production. Self-host for production with your own token.
`https://YOUR-BRIDGE-HOST/worker-mcp` (as shipped in `.mcp.json`) is a
placeholder, not a usable server; it fails loudly by design.

## Documentation map

- [First use](#first-use-60-seconds): env vars and Quick connect.
- [Endpoints](#endpoints): `/worker-mcp` (recommended) vs `/mcp` (legacy).
- [Codex and Claude Code](#codex-and-claude-code): concise setup.
- [More harnesses](#more-harnesses): compact matrix plus `docs/harnesses.md`.
- [Worker workflow](#worker-workflow): run, wait, verify, clean up.
- [Security](#security), [Local deployment](#local-deployment),
  [Contributor workflow](#contributor-workflow),
  [Publish and discover](#publish-and-discover): pointers below.
- Full guides: [docs/client-setup.md](docs/client-setup.md),
  [docs/copilot-setup.md](docs/copilot-setup.md),
  [docs/harnesses.md](docs/harnesses.md),
  [docs/compatibility.md](docs/compatibility.md),
  [docs/tool-api.md](docs/tool-api.md),
  [docs/worker-operating-model.md](docs/worker-operating-model.md),
  [docs/operations.md](docs/operations.md),
  [docs/registry.md](docs/registry.md).

## First use (60 seconds)

You need your own bridge deployment and its Bearer token. Keep the
token in environment variables. Never paste a real token into a file,
a chat log, or a commit. The optional community demo above is separate
and may require its own token; generic steps below only use your bridge.

```bash
export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
```

Replace `<your-domain>` with your bridge host and `<paste-token-here>`
with `MCP_BEARER_TOKEN` from that host. Generate a fresh token with
`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.

Quick connect (your own bridge): `./scripts/install-client.sh both`
registers Codex and Claude Code transports from `OPENCODE_MCP_URL` and
`OPENCODE_MCP_BEARER_TOKEN`. It fails clearly when either is missing or
the URL is malformed (`http(s)://...` ending in `/mcp` or
`/worker-mcp`); it never falls back to anyone else's server.

```bash
./scripts/install-client.sh both --dry-run
```

Full steps: [docs/client-setup.md](docs/client-setup.md).
Copilot-family products: [docs/copilot-setup.md](docs/copilot-setup.md).

Every example below uses `https://<your-domain>/worker-mcp` (safe
default, recommended: the eight worker tools `worker_catalog`,
`worker_run`, `worker_wait`, `worker_status`, `worker_verify`,
`worker_cleanup`, `worker_decide`, `worker_resume`; never `exec_run`)
or `https://<your-domain>/mcp` (legacy full catalog of 19 tools, with
`exec_run` only when the operator sets `ENABLE_EXEC_RUN=true`).
Codex plugin bundles do not interpolate env vars in the server URL, so
register the transport per machine with your concrete URL.

## Endpoints

Two Streamable HTTP endpoints share one Bearer token. `GET /health`
plus read-only `GET`/`HEAD` on `/.well-known/oauth-protected-resource`
(and `/mcp` and `/worker-mcp` children) and
`/.well-known/mcp/server-card.json` stay open with no secrets.

| Endpoint | Tools | Use |
| --- | --- | --- |
| `/worker-mcp` | Worker tools only (8, never `exec_run`) | Default for all new clients. Least privilege; no shell. |
| `/mcp` | Full compatibility catalog (19 tools) | Legacy only. `exec_run` fails closed unless `ENABLE_EXEC_RUN=true`. |
| `/health` | None (open) | Reverse-proxy liveness checks. |
| `/ready` | None (Bearer token) | Readiness: OpenCode plus registry (200/503). |
| `/metrics` | None (Bearer token) | Bounded counters, no sensitive data. |

## Codex and Claude Code

Protocol-level compatibility (MCP over Streamable HTTP with a Bearer
header) unless an end-to-end test is documented. Matrix, status labels,
and first-call contract: [docs/compatibility.md](docs/compatibility.md).

### Codex

```bash
codex mcp add opencode --url "$OPENCODE_MCP_URL" --bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN
```

Codex reads the token from the environment at request time. The
`opencode-worker` plugin adds skills (`delegate-to-opencode`, then
`verify-opencode-work`, on failure `recover-opencode-task`; code changes
follow `opencode-git-workflow`). Install from the Git marketplace pinned
at `v0.5.1`, then register your own transport as above (the bundled
placeholder URL is not usable):

```bash
codex plugin marketplace add ManuOtel/opencode-mcp-bridge --ref v0.5.1
```

Details: [docs/client-setup.md](docs/client-setup.md) sections 2 and 6.
Official docs: https://developers.openai.com/codex/cli/reference

### Claude Code

Preferred transport: a project `.mcp.json` entry with `type: http`,
`url: ${OPENCODE_MCP_URL}`, and header
`Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}` (expanded at load
time, token stays out of the file). CLI alternative, same reference
form:

```bash
claude mcp add --transport http --header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}' opencode "$OPENCODE_MCP_URL"
claude mcp add --transport http --header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}' opencode-bridge "$OPENCODE_MCP_URL"
```

A shell-expanded header would persist the secret in local config; rotate
the token if a config file leaks. Recommended: the `opencode-worker`
plugin from this repo's Claude marketplace
(`.claude-plugin/marketplace.json`), bundling the transport plus the
`coordinate-opencode-worker` skill. Export both variables first:

```bash
claude plugin marketplace add ManuOtel/opencode-mcp-bridge
claude plugin install opencode-worker@opencode-mcp-bridge
```

There is no npm or Brew package; both marketplaces install from this Git
repo. Details: [docs/client-setup.md](docs/client-setup.md) sections 3
and 7. Official docs: https://docs.anthropic.com/en/docs/claude-code/mcp

## More harnesses

Config keys differ per product; confirm key names in the linked official
docs before pasting. Full copy-ready blocks:
[docs/harnesses.md](docs/harnesses.md). Safe pattern everywhere: URL
`https://<your-domain>/worker-mcp`, header
`Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}`, the eight
`worker_*` tools (`worker_wait` is the bounded read-only long-poll;
`worker_decide`/`worker_resume` are approval-gated).

| Harness | Where | Status |
| --- | --- | --- |
| ChatGPT Developer Mode / connectors | [docs/harnesses.md](docs/harnesses.md#chatgpt-developer-mode) | Unverified with a static Bearer header |
| Cursor | [docs/harnesses.md](docs/harnesses.md#cursor) | Protocol-level |
| VS Code | [docs/harnesses.md](docs/harnesses.md#vs-code) | Protocol-level |
| Gemini CLI | [docs/harnesses.md](docs/harnesses.md#gemini-cli) | Protocol-level |
| OpenHands | [docs/harnesses.md](docs/harnesses.md#openhands) | Unverified |
| Windsurf | [docs/harnesses.md](docs/harnesses.md#windsurf) | Protocol-level |
| Cline | [docs/harnesses.md](docs/harnesses.md#cline) | Protocol-level |
| Roo Code | [docs/harnesses.md](docs/harnesses.md#roo-code) | Protocol-level |
| Pi | [docs/harnesses.md](docs/harnesses.md#pi) | Protocol-level |
| Hermes Agent | [docs/harnesses.md](docs/harnesses.md#hermes-agent) | Protocol-level |
| GitHub Copilot / Copilot Studio / M365 Copilot | [docs/copilot-setup.md](docs/copilot-setup.md) | Separate guide |
| MCP Inspector | [docs/harnesses.md](docs/harnesses.md#mcp-inspector-debugging) | Debugging only |

### OpenHands

```bash
openhands mcp add opencode-bridge --transport http \
  --header "Authorization: Bearer <paste-token-here>" \
  "https://<your-domain>/worker-mcp"
```

Replace `<paste-token-here>` with `MCP_BEARER_TOKEN` from your bridge
host (key names per https://docs.openhands.dev/openhands/usage/cli/mcp-servers).
Unverified end-to-end; full block:
[docs/harnesses.md](docs/harnesses.md#openhands). Without a client:
`./scripts/smoke.sh`.

## Worker workflow

`worker_run` is asynchronous (returns a `taskID` at once). Then wait
bounded server-side with `worker_wait`, or snapshot with
`worker_status`:

```text
worker_catalog()
worker_run(message="Implement X in /path/to/repo", directory="/path/to/repo", title="feat-x")
worker_wait(taskID="<taskID>", directory="/path/to/repo", timeout_s=30)
worker_verify(taskID="<taskID>", directory="/path/to/repo")
worker_cleanup(taskID="<taskID>", directory="/path/to/repo")
```

1. `worker_catalog` (free and connected by default). Default:
   `opencode/muse-spark-1.3-contributor-free`. Paid fallback
   `opencode-go/muse-spark-1.3-contributor` only when explicitly
   requested, passed as `providerID`/`modelID`. Never auto-selected.
2. `worker_run` with `message`, `directory`, `title`, optional
   `requestID` for safe retries (`deduplicated=true` on same-input
   retry). Save `taskID` and `directory` (status reads are
   directory-scoped).
3. `worker_wait` (up to `timeout_s`, default 30, clamped 1-120; returns
   early on change, or `timed_out=true` with
   `next_action="worker_wait"`) or `worker_status` for one snapshot.
   `running` waits again, `idle` verifies, `stale` cleans up,
   `error`/`unknown` recovers (`skills/recover-opencode-task/SKILL.md`).
4. `worker_verify`, then inspect the exact diff and run tests and lint
   with the host's own tools. Never trust a worker summary alone.
5. `worker_cleanup` (`action=abort` stops, `action=delete` removes).

Contracts: [docs/tool-api.md](docs/tool-api.md). Coordinator behavior:
[docs/worker-operating-model.md](docs/worker-operating-model.md).
Approval in `.mcp.json`: `worker_run`/`worker_cleanup` prompt;
`worker_wait`/`worker_status`/`worker_catalog`/`worker_verify`
auto-approve.

## Security

- `MCP_BEARER_TOKEN` is root-equivalent: long random value, rotate on
  leak, never commit `.env` or tokens.
- `/worker-mcp` never exposes `exec_run`; a leaked worker token cannot
  become a direct shell. Do not expose `/mcp` or set
  `ENABLE_EXEC_RUN=true` where a shell is not intended.
- Rotation: `MCP_BEARER_TOKEN_SECONDARY` holds one overlap token; move
  clients over, promote, restart. Blank or duplicate values fail closed.
- Open with no secrets: `GET /health` plus read-only RFC 9728 discovery
  and server card. Everything under `/mcp` and `/worker-mcp` needs the
  Bearer token.

## Local deployment

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), and a running
`opencode serve` or `opencode web` ([server
docs](https://opencode.ai/docs/server/)).

```bash
git clone https://github.com/ManuOtel/opencode-mcp-bridge.git
cd opencode-mcp-bridge
uv sync
cp .env.example .env
# edit .env: OpenCode credentials + a fresh MCP_BEARER_TOKEN
uv run python -m opencode_mcp_bridge.server
```

`curl http://127.0.0.1:8087/health` returns `{"ok": true}`. `POST /mcp`
and `POST /worker-mcp` without a token return 401. Key variables:
`OPENCODE_BASE_URL`, `OPENCODE_SERVER_PASSWORD`, `MCP_BEARER_TOKEN`,
`ENABLE_EXEC_RUN` (`false`), `TASK_STATE_PATH`, `MCP_MAX_BODY_BYTES`,
`MCP_ALLOWED_ORIGINS`. Put a reverse proxy with TLS in front. Release,
checks, rotation, rollback, logs: [docs/operations.md](docs/operations.md).

## Contributor workflow

Read [AGENTS.md](AGENTS.md) first (ownership, edits, free-model policy,
tests, secrets, worktrees, commits, reporting). Skills in `skills/`.

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

## Publish and discover

In-repo, no secrets: `server.json` (safe `/worker-mcp` metadata for
`io.github.ManuOtel/opencode-mcp-bridge`), `glama.json` (claim for
`ManuOtel`), Smithery via dashboard/CLI. Publishing needs a human owner
login. Checklist: [docs/registry.md](docs/registry.md). A registry entry
lists the software; it never grants access or supplies a token.
`/worker-mcp` (8 tools, no shell) is the default; `/mcp` (19 tools,
`exec_run` opt-in) is legacy. Never publish an endpoint you do not
operate, and never commit tokens.

## Community and license

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing code or docs.
Follow the [Code of Conduct](CODE_OF_CONDUCT.md); report security faults
per [SECURITY.md](SECURITY.md). [Open an
issue](https://github.com/ManuOtel/opencode-mcp-bridge/issues/new/choose)
or a [pull
request](https://github.com/ManuOtel/opencode-mcp-bridge/pulls) from a
feature branch.

License: PolyForm Noncommercial 1.0.0 - free for noncommercial use, see
[LICENSE.md](LICENSE.md). Commercial use needs permission:
manuotel@gmail.com
