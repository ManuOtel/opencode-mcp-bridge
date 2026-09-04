# opencode-mcp-bridge

MCP bridge for a self-hosted [`OpenCode`](https://opencode.ai) instance.
It exposes OpenCode sessions, models, diffs, and server shell access as MCP
tools over Streamable HTTP, so any MCP-compatible harness can drive it:
ChatGPT (developer connectors), Claude Code, Codex, MCP Inspector, and more.

This repo ships a Codex plugin (`opencode-worker`, see `.codex-plugin/`) with a
worker playbook (`skills/`) for async background workers, plus a Claude Code
plugin (`opencode-worker`, see `plugins/claude-code/`) with the
`coordinate-opencode-worker` skill. Worker-first: scope a
task, poll it, verify the diff, then clean up.

## Quick connect

```bash
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
./scripts/install-client.sh both
```

See [docs/client-setup.md](docs/client-setup.md) for copy/paste Codex and
Claude Code commands, token handling, `/worker-mcp` vs `/mcp` URLs, and the
opinionated plugin skills.

## Worker quickstart (start here)

1. Pick a model: `worker_catalog` (defaults to free + connected only).
2. Launch: `worker_run` with `message`, `directory`, `title`, and optional `requestID`
   for safe retries. Save `taskID` and `directory`.
3. Poll: `worker_status` with the same `taskID` and `directory` until `idle`.
   The saved directory is reused when `directory` is omitted.
4. Verify: call `worker_verify`, then inspect the exact diff and run tests/lint
   with the host's own tools; confirm acceptance criteria.
5. Clean up: `worker_cleanup` when done.

```text
worker_catalog()
worker_run(message="Implement X in /path/to/repo", directory="/path/to/repo", title="feat-x")
worker_status(taskID="<taskID>", directory="/path/to/repo")  # repeat until idle
worker_verify(taskID="<taskID>", directory="/path/to/repo")
```

Status and messages are directory-scoped: always pass the `directory` returned
by `worker_run` when it differs from the server default, or status reads `unknown`.
When omitted, `worker_status` and `worker_verify` recover the saved directory
from the durable task registry (`TASK_STATE_PATH`). Tasks are idempotent by
`requestID`: same ID plus same inputs returns the existing task with
`deduplicated=true`; conflicting reuse fails before side effects. If the
recorded session is gone from OpenCode, the retry recreates it
(`deduplicated=false`).
States: `running` (wait), `idle` (verify), `error`/`unknown` (recover, see
`skills/recover-opencode-task/SKILL.md`).

## Plugin install and config

- Codex (Git marketplace, recommended): the repository is the source of truth.
  The root `opencode-worker` plugin is exposed via `.agents/plugins/marketplace.json`
  (Git-backed root source, `ref` master):

  ```bash
  codex plugin marketplace add ManuOtel/opencode-mcp-bridge --ref master
  ```

  Then install `opencode-worker` from that marketplace. Set
  `OPENCODE_MCP_BEARER_TOKEN` in the environment; the token itself is never
  stored in the repo. See [docs/client-setup.md](docs/client-setup.md) section 6.
  This is the Codex plugin with the opinionated worker skills
  (`delegate-to-opencode`, `verify-opencode-work`, `recover-opencode-task`,
  `opencode-git-workflow`).
- Claude Code (Git marketplace, recommended): the nested `opencode-worker`
  plugin is exposed via the repo-root Claude marketplace
  `.claude-plugin/marketplace.json` (relative source `./plugins/claude-code`,
  owner `ManuOtel`):

  ```bash
  export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
  claude plugin marketplace add ManuOtel/opencode-mcp-bridge
  claude plugin install opencode-worker@opencode-mcp-bridge
  ```

  Inside an interactive Claude Code session the equivalents are the session
  commands `/plugin marketplace add ManuOtel/opencode-mcp-bridge` and
  `/plugin install opencode-worker@opencode-mcp-bridge` (not shell commands).
  Reload if requested. The plugin bundles the MCP transport plus the
  `coordinate-opencode-worker` skill. See
  [docs/client-setup.md](docs/client-setup.md) section 7. There is no npm or
  Brew package; both marketplaces install from this GitHub repo.
- Claude Code (manual transport only, no skills): `claude mcp add --transport http opencode https://opencode-mcp.manuotel.com/worker-mcp --header "Authorization: Bearer <token>"`
- Codex (manual transport only): `codex mcp add opencode --url https://opencode-mcp.manuotel.com/worker-mcp --bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN`.
  The manifest is
  `.codex-plugin/plugin.json`; bundled MCP config is `.mcp.json` (server `opencode`,
  `https://opencode-mcp.manuotel.com/worker-mcp`, worker-only tools).
- Other clients: add an MCP server with an `Authorization: Bearer <token>` header.
  Existing clients keep the full catalog at `https://<your-domain>/mcp`;
  worker-only clients use `https://<your-domain>/worker-mcp`. Both paths share
  the same Bearer token.
  - Claude Code: `claude mcp add --transport http opencode-bridge https://<your-domain>/mcp --header "Authorization: Bearer <token>"`
  - ChatGPT: Developer Mode ON > Connectors > Create connector, URL mode with
    `https://<your-domain>/mcp` + Bearer token, then Scan Tools.
  - Debug: MCP Inspector or `./scripts/smoke.sh` (see script header).

Per-tool approval: `worker_run` and `worker_cleanup` prompt; read-only worker
tools (`worker_status`, `worker_catalog`, `worker_verify`) auto-approve. The
policy ships in `.mcp.json`; if your client ignores it, enforce the same in
Codex config:

```toml
[plugins."opencode-worker".mcp_servers.opencode]
enabled = true
default_tools_approval_mode = "prompt"

[plugins."opencode-worker".mcp_servers.opencode.tools.worker_status]
approval_mode = "approve"

[plugins."opencode-worker".mcp_servers.opencode.tools.worker_catalog]
approval_mode = "approve"

[plugins."opencode-worker".mcp_servers.opencode.tools.worker_verify]
approval_mode = "approve"
```

## Tools

Primary worker tools:

| Tool | What it does |
| --- | --- |
| `worker_run` | Start a background worker (create session + async prompt). Returns compact `taskID` (= session ID), state, model, directory, title, `requestID`, `deduplicated`. Optional `requestID` makes retries idempotent. |
| `worker_status` | Poll state (`running`/`idle`/`error`/`unknown`) plus latest assistant text only, with truncation counts and bounded `directory` (recovered when omitted). |
| `worker_catalog` | List models, free + connected only by default, with bridge defaults. |
| `worker_verify` | Re-check a finished worker (state + evidence), read-only. Part of the 0.2.0 worker API. |
| `worker_cleanup` | Abort (`action=abort`) or delete (`action=delete`) a worker session. Prompts before running. Part of the 0.2.0 worker API (`abort_session` / `delete_session` remain the full-profile equivalents). |

Session and utility tools:

| Tool | What it does |
| --- | --- |
| `list_providers` | Providers + model IDs + connected status. Call first (model picker). |
| `list_agents` | Available agents (plan, build, ...). |
| `create_session` | New session, optional title/directory. |
| `send_message` | Prompt a session, wait for the reply. Optional model/agent override. |
| `list_sessions` | Recent sessions. |
| `get_session` | One session by ID. |
| `list_messages` | Messages in a session. |
| `abort_session` | Abort a running session. |
| `delete_session` | Delete a session and its data (clean up tests). |
| `get_diff` | File diffs from a session. |
| `exec_run` | Raw shell on the bridge host. No sandbox. Opt-in only (`ENABLE_EXEC_RUN=true`); disabled by default. Prefer sessions for code edits. |

## Endpoints and coexistence

- `/mcp` always serves the full backward-compatible 16-tool catalog, so
  existing clients never lose `list_*`, session, diff, or `exec_run` tools.
  `exec_run` stays listed but fails closed unless `ENABLE_EXEC_RUN=true`;
  production hosts that need legacy shell compatibility set it explicitly
  in the deployment env file.
- `/worker-mcp` serves exactly the five worker tools (`worker_catalog`,
  `worker_run`, `worker_status`, `worker_verify`, `worker_cleanup`) for the
  Codex plugin and other context-sensitive hosts.
- Both endpoints share the same Bearer token; `/health` stays open. There is
  no global tool-profile switch.

## Legacy compatibility

- `send_message` accepts `message`; `prompt` remains a backward-compatible alias.
  Supply exactly one.
- `providerID`/`modelID` must be given together or omitted. When omitted, the bridge
  uses its configured default model. Select agent/provider/model on `send_message`
  (or `worker_run`), not `create_session`.
- `worker_run` takes the same model options and defaults to the configured free model.
  `requestID` is optional; omit it to keep legacy behavior.
  `worker_catalog` filters (`free_only`, `connected_only` default true, `limit` default
  20, cap 100).
- `abort_session`, `delete_session`, and `get_diff` are full-profile legacy
  equivalents of `worker_cleanup` and `worker_verify`. Prefer the worker tools.

## Free-model policy

- Default model is `opencode/muse-spark-1.3-contributor-free`. Confirm with `worker_catalog`.
- No paid models, no Copilot, unless explicitly requested for that task.

## Security and approval profiles

- Security note: `/worker-mcp` is the recommended endpoint. It exposes
  exactly the five `worker_*` tools and never includes `exec_run`, so a
  leaked token cannot become a direct shell. Use `/mcp` only for legacy
  compatibility.
- Treat `MCP_BEARER_TOKEN` like a root password: long random value
  (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`), rotate on leak,
  never commit `.env`.
- `exec_run` is opt-in (`ENABLE_EXEC_RUN=true`) and disabled by default.
  When enabled, plus open directories, anyone with the Bearer token has a shell
  where the bridge runs. Prefer session tools for code edits; reserve `exec_run` for
  system ops (docker, systemctl, logs).
- `/health` is the only unauthenticated endpoint (reverse-proxy checks). Everything
  under `/mcp` and `/worker-mcp` requires the same Bearer token.
- Approval profile: writes prompt (`worker_run`, `worker_cleanup`, `exec_run`,
  `send_message`), reads auto-approve (`worker_status`, `worker_catalog`,
  `worker_verify`, `list_*`, `get_*`). Tighten to prompt-everything on shared hosts.

## Local run and configuration

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/), plus a running
`opencode serve` or `opencode web` (see [OpenCode server docs](https://opencode.ai/docs/server/)).

```bash
git clone https://github.com/ManuOtel/opencode-mcp-bridge.git
cd opencode-mcp-bridge
uv sync
cp .env.example .env
# edit .env: OpenCode credentials + a fresh MCP_BEARER_TOKEN
uv run python -m opencode_mcp_bridge.server
```

Check it: `curl http://127.0.0.1:8087/health` should report OpenCode healthy.
`POST /mcp` and `POST /worker-mcp` without a Bearer token must return 401.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENCODE_BASE_URL` | `http://127.0.0.1:4096` | OpenCode server URL. |
| `OPENCODE_SERVER_USERNAME` | `opencode` | Basic auth user for OpenCode. |
| `OPENCODE_SERVER_PASSWORD` | (required) | Basic auth password of your OpenCode server. |
| `MCP_BEARER_TOKEN` | (required) | Static token clients send as `Authorization: Bearer <token>`. |
| `MCP_HOST` | `127.0.0.1` | Bridge listen address. Use a host IP reachable from your reverse proxy when proxying from Docker. |
| `MCP_PORT` | `8087` | Bridge listen port. |
| `DEFAULT_DIRECTORY` | `$HOME` | Working directory for sessions when clients omit it. |
| `DEFAULT_PROVIDER_ID` | `opencode` | Default provider. |
| `DEFAULT_MODEL_ID` | `muse-spark-1.3-contributor-free` | Default model. |
| `EXEC_TIMEOUT_S` | `120` | Cap for `exec_run` timeouts. |
| `EXEC_MAX_OUTPUT_CHARS` | `20000` | Output truncation cap for `exec_run`. |
| `ENABLE_EXEC_RUN` | `false` | Opt-in for `exec_run` on `/mcp`. Set `true` only where a shell is intended. |
| `TASK_STATE_PATH` | `/var/lib/opencode-mcp-bridge/tasks.json` | JSON registry for durable tasks (atomic writes, bounded records, no prompts or secrets). |

Put a reverse proxy with TLS in front. Traefik example: `deploy/traefik-opencode-mcp.yaml`.
Host systemd keeps full terminal access for `exec_run` (see
`deploy/opencode-mcp-bridge.service`, env file `0600`); Docker scopes `exec_run` to
the container (`docker compose up -d` after filling `.env`).

## Contributor workflow

Read [AGENTS.md](AGENTS.md) first: ownership boundaries, edit discipline, free-model
policy, test commands, secrets, worktree/commit/merge rules, and reporting.
The worker playbook lives in `skills/` (`delegate-to-opencode`,
`verify-opencode-work`, `recover-opencode-task`, `opencode-git-workflow`).

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

CI (`.github/workflows/ci.yml`) runs the same checks on pull requests and
pushes to master, plus JSON validation of the Codex and Claude plugin manifests.

## License

PolyForm Noncommercial 1.0.0 - free for noncommercial use and modification,
commercial use needs permission. See [LICENSE.md](LICENSE.md). For a
commercial license, reach out: manuotel@gmail.com
