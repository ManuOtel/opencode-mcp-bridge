# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Codex plugin packaging: `.codex-plugin/plugin.json` (`opencode-worker` 0.2.0) with
  `skills` and `mcpServers` paths plus interface metadata and default prompts.
- Bundled MCP config: `.mcp.json` HTTP server `opencode` with bearer-token env var
  `OPENCODE_MCP_BEARER_TOKEN` and per-tool approval modes (prompt for
  `worker_run`/`worker_cleanup`, auto-approve for read-only worker tools).
- Worker playbook skills: `delegate-to-opencode`, `verify-opencode-work`,
  `recover-opencode-task`, `opencode-git-workflow`.
- Claude Code plugin packaging: `plugins/claude-code/.claude-plugin/plugin.json`
  (`opencode-worker` 0.1.0) with the `coordinate-opencode-worker` skill,
  exposed via the repo-root marketplace `.claude-plugin/marketplace.json`.
- Worker API: `worker_run`, `worker_status`, `worker_catalog`, `worker_verify`,
  and `worker_cleanup` (0.2.0 worker API, all five served on `/worker-mcp`)
  with free-model defaults and async polling.
- Repository worker rules (`AGENTS.md`) and CI for Python 3.11/3.12
  (`pytest`, `ruff check`, `ruff format --check`, `git diff --check`).
