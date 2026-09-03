---
name: coordinate-opencode-worker
description: "Coordinate one async OpenCode worker task end to end: scope, launch on the free model, poll and recover, verify independently, clean up, and integrate via Git sequentially."
---

# Coordinate OpenCode Worker

Use when the boss hands work to a background worker instead of doing it inline.
One task, one worker. Do not parallelize a single task across workers.

This skill describes coordinator behavior only. It cannot guarantee the
self-hosted bridge or the OpenCode server behind it is up. If the MCP tools
do not respond, stop and tell the boss the server side needs attention
instead of retrying blindly.

## 1. Scope the task

- State: goal, repo path, branch/worktree, files in scope, files off limits.
- Write acceptance criteria: observable checks (tests, commands, diff shape).
  No vague "make it better".
- Pick the model: default is `opencode/muse-spark-1.3-contributor-free`.
  Never use a paid model unless the boss explicitly asked for it for this task.
- If unsure which model is free and connected, call `worker_catalog` first
  (defaults already filter to free + connected).

## 2. Launch in isolation

- `git fetch` first so the branch starts from current upstream.
- Give the worker its own branch (`<type>/<short-topic>`) and its own
  worktree. Never let concurrent workers share a checkout.
- Call `worker_run` with `message`, `directory`, and `title`. Save the
  returned `taskID`, `modelID`, and `directory`.
- Always pass that same `directory` to every later `worker_status` call.
  Status and messages are directory-scoped; a wrong directory reads as
  `unknown`.
- Do not pass `providerID` without `modelID` (or vice versa). The pair must
  be given together or omitted.

## 3. Poll and recover

- Poll `worker_status` with backoff. States: `running` (keep waiting),
  `idle` (verify), `error`/`unknown` (recover, do not report success).
- Keep `include_output` true and the default cap unless output is huge.
  `worker_status` returns latest assistant text only, never full history.
- `unknown` usually means wrong `directory` or a gone session. Re-poll with
  the exact `directory` returned by `worker_run` before anything else.
- `error` means the latest assistant message carries a provider error. Read
  `output` before retrying; the fix may be the prompt, not the infra.
- Retry at most once with the cause fixed. Never fire parallel retries of
  the same task; duplicates cause duplicate side effects.
- Abort a live stuck session with `worker_cleanup(action=abort)`; delete
  with `worker_cleanup(action=delete)` only when the task is abandoned.
  Cleanup deletes session data and cannot be undone.

## 4. Verify before accepting

- Never accept a task from the worker summary alone. Call `worker_verify`
  first: it re-checks state and evidence without side effects.
- Then inspect the exact diff yourself with the host's own filesystem and
  terminal tools (read the changed files, `git diff`). Check scope: only
  intended files touched, no secrets, no stray artifacts.
- Run proportionate checks with the host's own tools: at minimum the repo's
  fast tests/lint for the touched area; full suite before merge.
- Confirm acceptance criteria one by one. Missing criteria means not done.
- Treat `error` state or an error flag in the latest output as failure even
  if the text sounds confident.

## 5. Integrate sequentially and clean up

- Small conventional commits (`feat:`, `fix:`, `chore:`, ...). One logical
  change per commit. Never commit secrets (`.env`, tokens, passwords).
- Integrate workers one at a time, never in parallel into the same branch.
  Rebase the worker branch before merge so history stays linear.
- The coordinator resolves conflicts and reruns the full checks after each
  resolution. Remove worktrees after merge (`git worktree remove`).
- Never force-push a shared branch. Never push without explicit
  authorization from the boss.
- Call `worker_cleanup` when the task is done or abandoned.

## 6. Report

- Done: taskID + model + directory, files changed, checks run with
  pass/fail, and remaining follow-ups.
- Not done: say exactly which criterion failed, with file/line or command
  output as evidence, then re-scope or retry.
