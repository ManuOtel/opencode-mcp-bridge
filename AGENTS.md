# AGENTS.md - opencode-mcp-bridge worker rules

These rules bind every worker (human or agent) in this repo. They align with the
`opencode-worker` Codex plugin skills. Skill order: `delegate-to-opencode` then
`verify-opencode-work`; on failure use `recover-opencode-task`; code changes follow
`opencode-git-workflow`.

## Ownership boundaries

- The coordinator assigns explicit per-task ownership boundaries.
  Workers edit only assigned paths.
- Concurrent workers use separate worktrees; never share a checkout.
- If scope must expand, stop and report to the coordinator.
  Never widen scope unilaterally.

## Edit discipline

- Read a file before editing it. Keep edits minimal and in scope.
- Never create files unless required. Prefer editing existing files.
- End every line with a newline. Keep line length at 100 (ruff default).
- Never commit `.env`, tokens, or passwords. `.env.example` documents config; `.env` stays local.

## Free-model policy

- Default model is `opencode/muse-spark-1.3-contributor-free`. Use `worker_catalog` to confirm
  free + connected models.
- No paid models, no Copilot, unless the boss explicitly requested them for this task.

## Tests and checks (run before reporting done)

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

- `ruff format` in write mode touches Python files: do NOT run it here. Read-only
  `--check` only. If formatting fails, report it; the coordinator routes the fix.
- Validate metadata: `python3 -m json.tool` for `.codex-plugin/plugin.json`
  and `.mcp.json`; skill frontmatter must be valid YAML with `name` + `description`.

## Secrets and security

- `MCP_BEARER_TOKEN` is a root-equivalent secret. Generate with
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`, rotate on leak.
- `exec_run` is an unsandboxed shell where the bridge runs. Prefer session tools for code edits.
- `/health` is the only unauthenticated endpoint. Everything under `/mcp` needs the Bearer token.

## Worktrees and git

- `git fetch` first. One branch + one worktree per worker; concurrent writers never share a checkout.
- Small conventional commits. Rebase before merge. Coordinator resolves conflicts and reruns full checks.
- Remove worktrees after merge. Never force-push shared branches.
- Never push, never open a PR, without explicit authorization.

## Reporting

- Report: taskID + model + directory, files changed, checks run with pass/fail, evidence
  (diff refs, command output), and open follow-ups.
- Never claim success from a worker summary alone. Quote the diff and the check output.
