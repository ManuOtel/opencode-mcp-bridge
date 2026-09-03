# Client setup (Codex and Claude Code)

One-command path: `./scripts/install-client.sh --help`. Manual copy/paste below.
There is no npm or Brew package; both clients install from this GitHub repo.

## 1. Token setup (never print or store the token)

Export the Bearer token in each shell. The value lives only in the
environment, never in the repo or docs.

```bash
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
```

Generate a fresh token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Rotate on leak. Never commit `.env`.

## 2. Codex

```bash
codex mcp add opencode --url https://opencode-mcp.manuotel.com/worker-mcp --bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN
```

This registers the compact worker MCP by env-var reference. Codex reads the
token from `OPENCODE_MCP_BEARER_TOKEN` at request time.

Use `--name <name>` via the helper to register under a different server name.

## 3. Claude Code

Easiest path: install the `opencode-worker` plugin from this repo's Claude
marketplace (section 7). It bundles the MCP transport
(`plugins/claude-code/.mcp.json`, server `opencode`,
`https://opencode-mcp.manuotel.com/worker-mcp`) plus the
`coordinate-opencode-worker` skill. Set `OPENCODE_MCP_BEARER_TOKEN` in the
environment as in section 1; the token is never stored in the repo.

Native MCP fallback (transport only, no skills):

```bash
claude mcp add --transport http opencode https://opencode-mcp.manuotel.com/worker-mcp --header "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN"
```

Warning: Claude Code HTTP header configuration may persist the token locally
in its MCP config. Prefer Codex env-var mode when shared hosts matter, and
rotate the token if a config file leaks.

Use `--name <name>` via the helper to register under a different server name.

## 4. Which URL

- New worker clients: `https://opencode-mcp.manuotel.com/worker-mcp`
  (exactly the five `worker_*` tools, compact context). This is the
  recommended endpoint: it never exposes `exec_run`, so a leaked token
  cannot become a direct shell.
- Existing legacy users keep `/mcp`
  (`https://<your-domain>/mcp`, full 16-tool catalog). Nothing breaks:
  `exec_run` stays listed but fails closed unless the bridge operator sets
  `ENABLE_EXEC_RUN=true` in the deployment env file.
- Both paths share the same Bearer token. `/health` stays open.
- Override the helper default with `OPENCODE_MCP_URL=https://<your-domain>/worker-mcp`.

## 5. Opinionated behavior (automatic with the plugin)

Codex plugin packaging (`opencode-worker`, `.codex-plugin/plugin.json`,
bundled `.mcp.json`) and Claude plugin packaging (`opencode-worker`,
`plugins/claude-code/.claude-plugin/plugin.json`, bundled
`plugins/claude-code/.mcp.json`) are available separately from this repo.
When the plugin is installed, its skills load automatically and enforce the
opinionated workflow:

- Codex: `delegate-to-opencode`, then `verify-opencode-work`.
- Claude Code: `coordinate-opencode-worker` (scope, launch, poll/recover,
  verify, clean up, sequential Git integration).
- Codex on failure: `recover-opencode-task`.
- Code changes follow `opencode-git-workflow`.
- `AGENTS.md` binds workers: ownership boundaries, separate worktrees,
  minimal in-scope edits, free-model default
  (`opencode/muse-spark-1.3-contributor-free`), and test commands
  (`pytest`, `ruff check`, `ruff format --check`, `git diff --check`).

Manual MCP registration above connects the transport only. Install the plugin
to get the delegation, verification, recovery, and Git workflow skills.

## 6. Codex plugin install via Git marketplace

The repository is the source of truth for the Codex plugin. The root plugin
`opencode-worker` (manifest `.codex-plugin/plugin.json`, bundled `.mcp.json`)
is exposed through the repo-scoped marketplace
`.agents/plugins/marketplace.json`, which points at the repository root via a
Git-backed source (`source=url`,
`https://github.com/ManuOtel/opencode-mcp-bridge.git`, `ref=master`). No
restructuring or plugin duplication was needed.

```bash
codex plugin marketplace add ManuOtel/opencode-mcp-bridge --ref master
```

Then install the `opencode-worker` plugin from that marketplace (Plugins
Directory, or `codex plugin add opencode-worker --marketplace opencode-mcp-bridge` where the CLI supports
it). Set `OPENCODE_MCP_BEARER_TOKEN` in the environment as in section 1; the
token is never stored in the repo.

This is different from the Claude Code native helper in section 3
(`claude mcp add --transport http ...`): that command only registers the MCP
transport and carries no plugin skills. For the Claude skills, use the Claude
plugin marketplace in section 7.

## 7. Claude plugin install via Git marketplace

This is separate from the Codex marketplace in section 6. The nested Claude
plugin `opencode-worker` (manifest
`plugins/claude-code/.claude-plugin/plugin.json`, bundled
`plugins/claude-code/.mcp.json`, skills under
`plugins/claude-code/skills/`) is exposed through the repo-root Claude
marketplace `.claude-plugin/marketplace.json`, which lists the nested plugin
via the relative source `./plugins/claude-code` (owner `ManuOtel`). The
existing Codex marketplace `.agents/plugins/marketplace.json` is unchanged.

```bash
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
/plugin marketplace add ManuOtel/opencode-mcp-bridge
```

Then install the `opencode-worker` plugin from that marketplace
(`/plugin install opencode-worker@opencode-mcp-bridge`) and reload Claude
Code if it asks for it. The plugin's MCP server reads the token from
`OPENCODE_MCP_BEARER_TOKEN` at request time via `Bearer ${OPENCODE_MCP_BEARER_TOKEN}`.

The bundled transport serves the compact worker endpoint
(`https://opencode-mcp.manuotel.com/worker-mcp`, exactly the five
`worker_*` tools) so a leaked token cannot become a direct shell. The plugin
cannot guarantee the self-hosted bridge or OpenCode server is reachable;
if the tools do not respond, check the server side.
