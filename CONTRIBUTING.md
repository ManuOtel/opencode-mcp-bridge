# Contributing to opencode-mcp-bridge

This project is a self-hosted MCP bridge for OpenCode.
It exposes OpenCode sessions, models, diffs, and server shell access as MCP
tools over Streamable HTTP.
It also ships a Codex plugin and a Claude Code plugin with worker skills.
The scope covers the bridge server, the two plugins, the skills, the client
setup docs, and the deployment files in this repository.

Out of scope are the OpenCode server itself, MCP client apps, and your own
host configuration such as TLS, reverse proxy, and firewall rules.

## Read the right docs first

- `README.md` gives the product overview and the local run steps.
- `AGENTS.md` binds every worker: ownership, edits, models, tests, secrets,
  worktrees, and reporting.
- `docs/client-setup.md` covers Codex and Claude Code registration.
- `docs/copilot-setup.md` covers Copilot-family products only.
- `docs/tool-api.md` describes the worker tools and the legacy tools.
- `skills/` holds the worker playbook for background workers.
- `SECURITY.md` explains private security reports.
- `CODE_OF_CONDUCT.md` states the conduct rules.

## Local setup

You need Python 3.11 or later and `uv`.
You also need a running `opencode serve` or `opencode web` instance.

Run the following steps:

```bash
git clone https://github.com/ManuOtel/opencode-mcp-bridge.git
cd opencode-mcp-bridge
uv sync
cp .env.example .env
```

Edit `.env` with your OpenCode credentials and a fresh bearer token.
Generate a token with the following command:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Run the bridge with the following command:

```bash
uv run python -m opencode_mcp_bridge.server
```

Verify that the bridge is healthy with the following command:

```bash
curl http://127.0.0.1:8087/health
```

`POST /mcp` and `POST /worker-mcp` without a bearer token must return 401.
Use `/worker-mcp` for worker clients.
Use `/mcp` only for legacy full-catalog clients.

## Branch and worktree isolation

Work on a short-lived feature branch from `master`.
If you run parallel workers, give each worker one branch and one worktree.
Workers must not share a checkout.
Keep each branch focused on one task.
If a task must grow beyond its boundary, stop and ask before you widen it.

Do not push directly to `master`.
Open a pull request from your feature branch instead.

## Make focused changes

- Read a file before you edit it.
- Keep edits minimal and inside the assigned scope.
- Keep line length at 100 characters or less.
- End every line with a newline.
- Do not reformat unrelated files.
- Do not run `ruff format` in write mode on Python files.

## Tests and verification

Run the following commands before you report the work as done:

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

CI (`.github/workflows/ci.yml`) runs the same verification on pull requests
and on pushes to `master`.
CI tests Python 3.11 and 3.12.
CI also verifies JSON manifests with `python3 -m json.tool` for these files:

- `.codex-plugin/plugin.json`
- `.mcp.json`
- `.agents/plugins/marketplace.json`
- `.claude-plugin/marketplace.json`
- `plugins/claude-code/.claude-plugin/plugin.json`
- `plugins/claude-code/.mcp.json`

If `ruff format --check` fails, report it and leave the fix to the reviewer.
Do not claim success from a summary alone.
Quote the diff and the command output in your report.

## Security and privacy rules

- Treat `MCP_BEARER_TOKEN` as a root password.
- Never commit `.env`, tokens, passwords, or private URLs.
- Never paste tokens, prompts with personal data, or private paths into
  issues, pull requests, logs, or test fixtures.
- Redact bearer tokens and `Authorization` headers from every log excerpt.
- Prefer session tools for code edits.
- Use `exec_run` only where a shell is intended, because it runs raw shell
  commands on the bridge host with no sandbox.
- If a secret leaks, rotate it at once (see `SECURITY.md`).
- Report security faults in private through GitHub Security Advisories.
  Do not open a public issue for a security fault.

## Commit and pull request expectations

- Write small conventional commits, for example `fix(auth): reject blank
  secondary token`.
- Rebase your branch on current `master` before review if it has drifted.
- Never force-push a shared branch.
- Never push or open a pull request without permission when you act as a
  delegated worker.
- Describe the summary, scope, tests, docs impact, security review, and
  deployment impact in the pull request (see the pull request template).
- Confirm that you obeyed the Code of Conduct.

## What a useful bug report contains

- A short problem statement in one or two sentences.
- Expected behavior and actual behavior.
- Reproducible steps, in numbered order.
- Bridge version, Python version, client name, and endpoint (`/worker-mcp`
  or `/mcp`).
- Redacted logs or error text that shows the fault.
- A statement that the report holds no secrets or personal data.
- Private data goes only in a Security Advisory, never in a public issue.

Thank you for keeping changes small, verified, and safe to review.
