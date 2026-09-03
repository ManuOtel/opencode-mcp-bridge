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
- Worker API: `worker_run`, `worker_status`, `worker_catalog` (plus `worker_verify` /
  `worker_cleanup` landing via the companion branch) with free-model defaults and
  async polling.
- Repository worker rules (`AGENTS.md`) and CI for Python 3.11/3.12
  (`pytest`, `ruff check`, `ruff format --check`, `git diff --check`).
