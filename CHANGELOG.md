# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.2.0] - 2026-09-04

### Added

- Worker API 0.2.0: `worker_run`, `worker_status`, `worker_catalog`,
  `worker_verify`, and `worker_cleanup` (all five served on `/worker-mcp`)
  with free-model defaults, async polling, and idempotent retries via
  `requestID`.
- Codex plugin packaging: `.codex-plugin/plugin.json` (`opencode-worker`
  0.2.0) with `skills` and `mcpServers` paths plus interface metadata and
  default prompts. Codex marketplace `.agents/plugins/marketplace.json`
  pins the Git source to tag `v0.2.0`.
- Claude Code plugin packaging: `plugins/claude-code/.claude-plugin/plugin.json`
  (`opencode-worker` 0.2.0) with the `coordinate-opencode-worker` skill,
  exposed via the repo-root marketplace `.claude-plugin/marketplace.json`.
- Bundled MCP configs: `.mcp.json` and `plugins/claude-code/.mcp.json`
  (server `opencode`) with bearer-token env vars and per-tool approval
  modes (prompt for `worker_run`/`worker_cleanup`, auto-approve for
  read-only worker tools).
- Worker playbook skills: `delegate-to-opencode`, `verify-opencode-work`,
  `recover-opencode-task`, `opencode-git-workflow`.
- Auth rotation: optional `MCP_BEARER_TOKEN_SECONDARY` overlap token with
  constant-time comparison; blank or duplicate values fail startup.
- Task locking and reliability: cross-process file lock for
  `TASK_STATE_PATH`, durable JSON registry with atomic writes, bounded
  records, and directory recovery when omitted.
- Own-bridge-first onboarding: explicit `OPENCODE_MCP_URL` required with no
  fallback server, visible Codex placeholder host, and opt-in maintainer
  demo only. Copilot-family guide (`docs/copilot-setup.md`) for GitHub
  Copilot, Copilot Studio, and Microsoft 365 Copilot.
- Community files: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,
  issue templates, and pull request template.
- CI and Docker hardening: `ci.yml` on Python 3.11/3.12/3.13 with frozen
  `uv sync`, `pytest`, `ruff check`, `ruff format --check`,
  `git diff --check`, JSON manifest validation, no-push Docker build, and
  weekly Dependabot for pip, Docker, and GitHub Actions.

### Security

- Server-side directory authorization (`ALLOWED_DIRECTORIES`) with hermetic
  tests and bounded outputs.
- Minimal `/health` response without version or error disclosure.
- Non-root bridge process and containers (systemd, Docker, Traefik
  defaults); `exec_run` opt-in only (`ENABLE_EXEC_RUN=true`) and absent
  from `/worker-mcp`.
- Redacted structured lifecycle logging (no token or secret values).

### Fixed

- Scoped `git safe.directory` per invocation for non-root deploys.
- Completed-worker inference from assistant output and conservative free
  model selection.
