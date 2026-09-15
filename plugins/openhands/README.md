# opencode-worker plugin for OpenHands

Native OpenHands plugin that drives a self-hosted OpenCode instance as async
background workers over MCP. You bring your own bridge deployment and token;
this package ships placeholders only and points at no shared server.

Layout follows the current official OpenHands conventions: OpenHands-native
`.plugin/plugin.json` metadata, AgentSkills `skills/<name>/SKILL.md`, and a
FastMCP-shaped `.mcp.json` at the plugin root.

Official documentation sources used for this layout:

- Plugins: https://docs.openhands.dev/overview/plugins
- Skills overview: https://docs.openhands.dev/overview/skills
- Creating skills: https://docs.openhands.dev/overview/skills/creating
- MCP settings (Streamable HTTP): https://docs.openhands.dev/openhands/usage/settings/mcp-settings
- CLI MCP servers: https://docs.openhands.dev/openhands/usage/cli/mcp-servers
- SDK MCP guide: https://docs.openhands.dev/sdk/guides/mcp

If the official key names changed since this package was written, trust the
linked official docs over this file and report the drift.

## What is included

```text
plugins/openhands/
├── .plugin/plugin.json
├── .mcp.json
├── skills/coordinate-opencode-worker/SKILL.md
└── README.md
```

Only `.plugin/plugin.json` is required by OpenHands. Everything else is the
smallest useful bundle: one focused skill, one safe MCP config, this guide.

## Prerequisites

- Your own bridge deployment with a public HTTPS URL and its Bearer token.
  Keep the token in environment variables. Never paste a real token into a
  file, a chat log, or a commit.
- Generate a fresh token on the bridge host with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

- Export per machine (replace with your own host and token):

```bash
export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
```

Rules for every URL in this package:

- `https://<your-domain>/worker-mcp` is the safe default. It exposes exactly
  eight worker tools and never includes `exec_run`.
- `https://YOUR-BRIDGE-HOST/worker-mcp` (as shipped in `.mcp.json`) is a
  placeholder. It fails loudly by design. Always register your own URL.
- There is no shared production server in this package. Self-host for
  production with your own token. No tracking, provider keys, or production
  credentials are bundled here.

## Install the plugin

From a local checkout (this repo at `plugins/openhands`):

```bash
openhands --plugin /path/to/opencode-mcp-bridge/plugins/openhands
```

From GitHub (monorepo subpath form per official plugin docs):

```text
github:ManuOtel/opencode-mcp-bridge/plugins/openhands
```

Local GUI alternative: place or symlink this directory under
`.openhands/plugins/` in your workspace, then restart OpenHands. Confirm the
exact key names in the linked official docs before pasting.

## Register the MCP transport

Preferred: CLI registration against your own bridge (safe endpoint only):

```bash
openhands mcp add opencode --transport http \
  --header "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN" \
  "$OPENCODE_MCP_URL"
```

Manual alternative: edit `~/.openhands/mcp.json` (FastMCP
`{"mcpServers": {...}}` shape, key names per official CLI docs):

```json
{
  "mcpServers": {
    "opencode": {
      "url": "https://<your-domain>/worker-mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

`config.toml` alternative (Streamable HTTP section per official MCP settings
docs):

```toml
[mcp]
shttp_servers = [
  { url = "https://<your-domain>/worker-mcp", api_key = "${OPENCODE_MCP_BEARER_TOKEN}" }
]
```

Check status inside a conversation with `/mcp`. The agent then sees the eight
worker tools: `worker_catalog`, `worker_run`, `worker_wait`, `worker_status`,
`worker_verify`, `worker_cleanup`, `worker_decide`, `worker_resume`.

Prefer bounded `worker_wait` for progress (`timeout_s` default 30 seconds,
server clamp 1-120 seconds). It returns on state or message change or at the
deadline with `timed_out=true` and `next_action="worker_wait"` to call again.
No client sleep loops. Use `worker_status` for an immediate snapshot only.

## Coordinator workflow

The bundled `coordinate-opencode-worker` skill enforces this order:

1. Pick a model with `worker_catalog` (free and connected only by default).
   Start with free default `opencode/muse-spark-1.3-contributor-free`
   (provider `opencode` + model `muse-spark-1.3-contributor-free`).
   Ordered fallback, one retry only: if the free model is missing/unavailable in
   `worker_catalog`, or `worker_run` cannot start because that model/provider is
   unavailable, retry the same scoped task once with paid fallback
   `opencode-go/muse-spark-1.3-contributor` (provider `opencode-go` + model
   `muse-spark-1.3-contributor`) from `worker_catalog.recommendations[1]`.
   Never use Copilot or any other paid model. This paid fallback is pre-authorized
   by the product owner for this project only when the free default is unavailable;
   it is not a general permission to spend. The bridge never auto-switches to paid;
   the coordinator performs one explicit fallback `worker_run` after confirming the
   free model is unavailable. Do not switch models after a worker has started, and
   never launch parallel duplicate retries. Catalog/start unavailability is distinct
   from task failure after start: once started, inspect status/output first and never
   silently retry after a possible worker-side partial mutation. Record the selected
   provider/model in the report.
2. Launch with `worker_run` (`message`, `directory`, `title`). Save `taskID`
   and `directory`. Use a fresh branch plus a dedicated worktree per worker;
   concurrent workers never share a checkout.
3. Wait with bounded `worker_wait` (same `taskID` and `directory`) until
   `idle`. Use `worker_status` for an immediate snapshot only.
   States: `running` (wait again), `idle` (verify), `error`/`unknown`
   (recover).
4. Verify with `worker_verify`, then inspect the exact diff and run tests and
   lint with the host's own tools. Never trust a worker summary alone.
5. Integrate sequentially (rebase, one logical commit, rerun checks,
   coordinator resolves conflicts), remove worktrees after merge, never
   force-push a shared branch, never push without explicit authorization.
6. Clean up with `worker_cleanup` (`action=abort` stops, `action=delete`
   removes) when done or abandoned.

## Validation boundary

Package structure is covered by deterministic tests
(`tests/test_openhands_plugin.py`): JSON manifests parse, skill frontmatter
carries `name` plus `description`, the MCP config points at the safe
`/worker-mcp` placeholder with no secrets, and this guide keeps placeholders
plus bring-your-own-bridge wording. No clean OpenHands runtime is required
for those checks.

A live OpenHands runtime check (plugin load plus `/mcp` tool list against a
self-hosted bridge) was not run here; a clean OpenHands runtime was
unavailable in this environment. Run `openhands --plugin <path>` and
`openhands mcp list` against your own deployment before relying on it.
