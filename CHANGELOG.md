# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.4.0] - 2026-09-14

### Added

- Opt-in live conformance gate (`scripts/live_conformance.sh`,
  `tests/test_live_conformance.py`,
  `tests/test_live_conformance_gate.py`): runs only when the operator
  explicitly opts in with a protected bearer token passed via
  environment/header files. Never prints, persists, or logs the token.
  Not run as part of the default offline release gate.
- Approval/resume states with bounded `worker_wait`: `worker_decide`
  and `worker_resume` join the worker surface; `worker_wait` stays
  bounded (default 30, clamped 1-120) with `timed_out` and
  `next_action` contracts. Existing task keys are unchanged.
- OpenHands native package (`plugins/openhands/`, `opencode-worker`
  0.4.0): `.plugin/plugin.json`, safe `/worker-mcp` `.mcp.json` with
  placeholder host only, `coordinate-opencode-worker` skill, and
  self-serve README. No shared-server URL or secret in the package.
  Structural tests only; no OpenHands runtime run claimed.
- Compatibility and adoption docs plus the eight-tool safe endpoint:
  `docs/compatibility.md`, adoption proof (`tests/test_adoption_proof.py`),
  and harness matrix docs. `/worker-mcp` serves exactly eight worker
  tools and never `exec_run`; `/mcp` serves 19 with `exec_run`
  fail-closed unless explicitly enabled.
- Published plugin versions: Codex `opencode-worker` 0.4.0
  (`.codex-plugin/plugin.json`, marketplace ref `v0.4.0`), Claude
  `opencode-worker` 0.4.0
  (`plugins/claude-code/.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`), and OpenHands `opencode-worker`
  0.4.0 (`plugins/openhands/.plugin/plugin.json`). Bridge and registry
  metadata (`pyproject.toml`, `server.json`) track 0.4.0.

### Security

- Bearer-only auth with no OAuth claims; RFC 9728 metadata and
  Smithery server-card stay unauthenticated without disclosing tokens.
- `exec_run` remains absent from `/worker-mcp` and fail-closed on
  `/mcp` unless `ENABLE_EXEC_RUN=true`.
- No live registry or deployment proof claimed in this entry.

## [0.3.0] - 2026-09-14

### Added

- New `worker_wait` tool (requires v0.3.0 code): bounded server-side
  long-poll for one task. Returns on state or message change, or at the
  finite `timeout_s` deadline (default 30, clamped 1-120) with
  `timed_out=true` and `next_action="worker_wait"`. Read-only with
  `include_output=false` state-only waits; no client sleep loops.
  Served on `/worker-mcp` (now six worker tools) and `/mcp` (now
  17 tools); auto-approved alongside `worker_status`, `worker_catalog`,
  and `worker_verify` in `.mcp.json`.
- Stable additive task contracts: `worker_run`, `worker_wait`,
  `worker_status`, `worker_verify`, `worker_cleanup`, and
  `worker_catalog` keep every existing key and add `state`, `timed_out`,
  `retryable`, `next_action`, `error_code` (for example `task_not_found`
  for a missing task, else `null`), and a concise `evidence` object.
  Existing clients keep working. Truthful `output_schema` on every
  worker tool, including the full status field set on `worker_verify`.
- Deterministic MCP conformance smoke tests
  (`tests/test_mcp_conformance.py`): handshake negotiation, exact
  6/17 tool surfaces, truthful wire schemas and annotations,
  free-first catalog recommendations, dedup, error/scope behavior,
  and bounded output. Focused `worker_wait` tests
  (`tests/test_worker_wait.py`) prove bounded waits, read-only
  behavior, and backward-compatible status contracts.
- Harness and adoption documentation: coordinator-ergonomics README
  section, `worker_wait`-first workflow in `docs/tool-api.md`, skills
  (`delegate-to-opencode`, `recover-opencode-task`,
  `coordinate-opencode-worker`) preferring bounded `worker_wait` with
  `worker_status` snapshots and the `worker_verify` evidence gate, and
  a six-tool `scripts/smoke.sh`.
- Published plugin versions: Codex `opencode-worker` 0.3.0
  (`.codex-plugin/plugin.json`, marketplace ref `v0.3.0`) and Claude
  `opencode-worker` 0.3.0
  (`plugins/claude-code/.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`). Bridge and registry metadata
  (`pyproject.toml`, `server.json`) track 0.3.0.

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
