---
name: delegate-to-opencode
description: Scope and launch an async OpenCode worker task with free-model defaults, acceptance criteria, and polling.
---

# Delegate to OpenCode

Use when the boss hands work to a background worker instead of doing it inline.

## Scope the task

- One task, one worker. State: goal, repo path, branch/worktree, files in scope, files off limits.
- Write acceptance criteria: observable checks (tests, commands, diff shape). No vague "make it better".
- Pick the model: default is `opencode/muse-spark-1.3-contributor-free`. Never use a paid model or Copilot unless the boss explicitly asked for it.
- If unsure which model is free and connected, call `worker_catalog` first (defaults already filter to free + connected).

## Launch

- Call `worker_run` with `message`, `directory`, and `title`. Save the returned `taskID`, `modelID`, and `directory`.
- Always pass that same `directory` to every later `worker_status` call. Status and messages are directory-scoped; a wrong directory reads as `unknown`.
- Do not pass `providerID` without `modelID` (or vice versa). The pair must be given together or omitted.

## Poll async

- Poll `worker_status` with backoff. States: `running` (keep waiting), `idle` (inspect output), `error`/`unknown` (see `recover-opencode-task`).
- Keep `include_output` true and the default cap unless output is huge. Never dump full history; `worker_status` returns latest assistant text only.
- When `idle`, move to `verify-opencode-work`. Never report success from the worker summary alone.
