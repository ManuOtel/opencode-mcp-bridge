# Client setup (Codex and Claude Code)

One-command path: `./scripts/install-client.sh --help`. Manual copy/paste below.
There is no npm or Brew package; both clients install from this GitHub repo.

For Copilot-family products (GitHub Copilot, Copilot Studio, Microsoft 365
Copilot), see [docs/copilot-setup.md](copilot-setup.md). Those three cases are
separate from Codex and Claude Code.

> Your bridge vs the maintainer demo. This repo helps you connect Codex and
> Claude Code to **your own** self-hosted `opencode-mcp-bridge` (your server,
> your token, your `https://<your-domain>/worker-mcp`). Nothing in the generic
> install path points at anyone else's server. The maintainer's demo endpoint
> (`https://opencode-mcp.manuotel.com/worker-mcp`) is opt-in only: use it
> solely if the maintainer explicitly invited you to try it, by exporting it
> as your `OPENCODE_MCP_URL` (section 4). It never becomes your server
> silently.

## 0. Bridge URL setup (required, never defaults)

Every command below needs your own bridge URL in the environment. The helper
fails fast when it is missing; there is no fallback server.

```bash
export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"
```

Use `/worker-mcp` for the compact five-tool worker endpoint (recommended).
Use `/mcp` only for legacy full-catalog clients (section 4).

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
codex mcp add opencode --url "$OPENCODE_MCP_URL" --bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN
```

This registers the compact worker MCP by env-var reference. Codex reads the
token from `OPENCODE_MCP_BEARER_TOKEN` at request time. The URL is a literal:
Codex plugin bundles do not interpolate environment variables in the server
URL, so register the transport per machine with your concrete URL (or use the
helper, which requires `OPENCODE_MCP_URL`).

Or via the helper (fails clearly when `OPENCODE_MCP_URL` is missing):

```bash
./scripts/install-client.sh codex
```

Use `--name <name>` via the helper to register under a different server name.

## 3. Claude Code

Easiest path: install the `opencode-worker` plugin from this repo's Claude
marketplace (section 7). It bundles the MCP transport
(`plugins/claude-code/.mcp.json`, server `opencode`,
URL `${OPENCODE_MCP_URL}`) plus the `coordinate-opencode-worker` skill.
Claude Code expands `${VAR}` references in `.mcp.json` at load time, so
export **both** variables from sections 0 and 1 **before** installing the
plugin; the token is never stored in the repo.

Native MCP fallback (transport only, no skills):

```bash
claude mcp add --transport http opencode "$OPENCODE_MCP_URL" --header "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN"
```

Warning: Claude Code HTTP header configuration may persist the token locally
in its MCP config. Prefer Codex env-var mode when shared hosts matter, and
rotate the token if a config file leaks.

Use `--name <name>` via the helper to register under a different server name.

## 4. Which URL

- New worker clients: `https://<your-domain>/worker-mcp`
  (exactly the five `worker_*` tools, compact context). This is the
  recommended endpoint: it never exposes `exec_run`, so a leaked token
  cannot become a direct shell.
- Existing legacy users keep `/mcp`
  (`https://<your-domain>/mcp`, full 16-tool catalog). Nothing breaks:
  `exec_run` stays listed but fails closed unless the bridge operator sets
  `ENABLE_EXEC_RUN=true` in the deployment env file.
- Both paths share the same Bearer token. `/health` stays open.
- The helper takes the URL only from `OPENCODE_MCP_URL` and exits with an
  error when it is unset or empty. There is no default server.

### Optional: maintainer demo endpoint

If the maintainer explicitly invited you to try their demo bridge, opt in
explicitly per shell (never commit this anywhere):

```bash
export OPENCODE_MCP_URL="https://opencode-mcp.manuotel.com/worker-mcp"
```

That demo is someone else's server with its own token; it is not your
bridge, and generic installs never use it unless you set it yourself.

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

Note on the Codex bundle: the shipped `.mcp.json` carries the visible
placeholder `https://YOUR-BRIDGE-HOST/worker-mcp` because Codex does not
support `${OPENCODE_MCP_URL}` interpolation in plugin server URLs. Do not
edit the placeholder in the repo; register your real transport per machine
with section 2 (the placeholder fails DNS loudly if ever used directly,
which is intentional). The per-tool approval policy in that file still
applies once your transport is registered.

## 6. Codex plugin install via Git marketplace

The repository is the source of truth for the Codex plugin. The root plugin
`opencode-worker` (manifest `.codex-plugin/plugin.json`, bundled `.mcp.json`)
is exposed through the repo-scoped marketplace
`.agents/plugins/marketplace.json`, which points at the repository root via a
Git-backed source (`source=url`,
`https://github.com/ManuOtel/opencode-mcp-bridge.git`, `ref=v0.2.0`). No
restructuring or plugin duplication was needed. The `v0.2.0` ref is the
stable release tag; it exists only after the maintainer creates it.

```bash
codex plugin marketplace add ManuOtel/opencode-mcp-bridge --ref v0.2.0
```

Then install the `opencode-worker` plugin from that marketplace (Plugins
Directory, or `codex plugin add opencode-worker --marketplace opencode-mcp-bridge` where the CLI supports
it). Set `OPENCODE_MCP_BEARER_TOKEN` in the environment as in section 1; the
token is never stored in the repo. Then register your own transport as in
section 2 (required: the bundled placeholder URL is not usable as-is).

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
via the relative source `./plugins/claude-code` (owner `ManuOtel`,
version `0.2.0`). The Claude marketplace command installs from the
checked-out Git revision; the stable release is identified by Git tag
`v0.2.0` once the maintainer creates it.

```bash
export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
```

Then register the marketplace and install the plugin. From a terminal
(outside any session):

```bash
claude plugin marketplace add ManuOtel/opencode-mcp-bridge
claude plugin install opencode-worker@opencode-mcp-bridge
```

Or from inside an interactive Claude Code session (these are session
commands, not shell commands):

```text
/plugin marketplace add ManuOtel/opencode-mcp-bridge
/plugin install opencode-worker@opencode-mcp-bridge
```

Reload Claude Code if it asks for it. The plugin's MCP server reads the URL
from `OPENCODE_MCP_URL` and the token from `OPENCODE_MCP_BEARER_TOKEN` at
request time via `${OPENCODE_MCP_URL}` and
`Bearer ${OPENCODE_MCP_BEARER_TOKEN}`. Both variables must be exported
before install, otherwise the transport has no server to reach.

The bundled transport serves the compact worker endpoint
(`https://<your-domain>/worker-mcp`, exactly the five
`worker_*` tools) so a leaked token cannot become a direct shell. The plugin
cannot guarantee the self-hosted bridge or OpenCode server is reachable;
if the tools do not respond, check the server side.

## 8. Copilot-family products

GitHub Copilot cloud agent and code review, Microsoft Copilot Studio, and
Microsoft 365 Copilot each need their own steps. See
[copilot-setup.md](copilot-setup.md) for the three separate cases, the
read-only and change-enabled JSON blocks, and the official GitHub and
Microsoft links.
