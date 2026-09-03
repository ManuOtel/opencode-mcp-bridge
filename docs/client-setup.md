# Client setup (Codex and Claude Code)

One-command path: `./scripts/install-client.sh --help`. Manual copy/paste below.

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
bundled `.mcp.json`) is available separately from this repo. When the plugin
is installed, its skills load automatically and enforce the opinionated
workflow:

- `delegate-to-opencode`, then `verify-opencode-work`.
- On failure: `recover-opencode-task`.
- Code changes follow `opencode-git-workflow`.
- `AGENTS.md` binds workers: ownership boundaries, separate worktrees,
  minimal in-scope edits, free-model default
  (`opencode/muse-spark-1.3-contributor-free`), and test commands
  (`pytest`, `ruff check`, `ruff format --check`, `git diff --check`).

Manual MCP registration above connects the transport only. Install the plugin
to get the delegation, verification, recovery, and Git workflow skills.
