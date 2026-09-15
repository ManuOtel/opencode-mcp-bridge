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

Default transport is the safe `/worker-mcp` endpoint (set `OPENCODE_MCP_URL`
to `https://<your-domain>/worker-mcp`). It exposes exactly eight worker
tools and never includes `exec_run`:

- `worker_catalog`, `worker_run`, `worker_wait`, `worker_status`,
  `worker_verify`, `worker_cleanup`, `worker_decide`, `worker_resume`

## 1. Scope the task

- State: goal, repo path, branch/worktree, files in scope, files off limits.
- Write acceptance criteria: observable checks (tests, commands, diff shape).
  No vague "make it better".
- Pick the model: start with `opencode/muse-spark-1.3-contributor-free`
  (provider `opencode`, model `muse-spark-1.3-contributor-free`).
- When availability is uncertain, call `worker_catalog` first and record the
  selected provider/model before launching.
- If the free model is missing/unavailable in `worker_catalog`, or `worker_run`
  cannot start because that provider/model is unavailable, retry the same
  scoped task exactly once with `opencode-go/muse-spark-1.3-contributor`
  (provider `opencode-go`, model `muse-spark-1.3-contributor`, from
  `worker_catalog` `recommendations[1]`).
- This paid fallback is pre-authorized by the product owner for this project
  only when the free model is unavailable. Do not use Copilot or any other
  paid model.
- The bridge does not silently auto-switch; the coordinator performs one
  explicit fallback `worker_run`.
- Never switch models after a worker has started. Never launch parallel
  duplicate retries of the same task.
- Distinguish catalog/start unavailability from a worker failure after start.
  After start, inspect status/output and follow recovery/verification; never
  silently retry after possible partial mutation.

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

## 3. Wait and recover

- Prefer bounded `worker_wait` (`timeout_s` default 30, clamped 1-120)
  for progress: the server holds the call until the task state or
  latest message changes, then returns. At the deadline it returns
  `timed_out=true` with `next_action="worker_wait"` so you can call it
  again. No client sleep loops. `include_output=false` gives a cheap
  state-only wait.
- Use `worker_status` for an immediate snapshot only: after a timed-out
  wait, after an error, or when you need one quick look. States:
  `running` (wait again), `idle` (verify), `error`/`unknown`
  (recover, do not report success).
- Read the stable result fields (`taskID`, `state`, `retryable`,
  `next_action`, `error_code`, `evidence`) and follow `next_action`.
- Approval-gated runs only: `worker_run` may return
  `state="approval_required"` instead of starting work. Then `worker_decide`
  approves without starting anything and `worker_resume` starts the same
  task exactly once (same `message`, `directory`, and approval token).
  Nothing touches OpenCode before the resume; duplicates and mismatches
  fail safely with no second session.
- Keep `include_output` true and the default cap unless output is huge.
  `worker_wait`/`worker_status` return latest assistant text only,
  never full history.
- `unknown` usually means wrong `directory` or a gone session. Re-check
  once with `worker_status` using the exact `directory` returned by
  `worker_run` before anything else.
- `error` means the latest assistant message carries a provider error. Read
  `output` before retrying; the fix may be the prompt, not the infra.
- Retry at most once with the cause fixed. Never fire parallel retries of
  the same task; duplicates cause duplicate side effects.
- Abort a live stuck session with `worker_cleanup(action=abort)`; delete
  with `worker_cleanup(action=delete)` only when the task is abandoned.
  Cleanup is task-scoped: only the given `taskID` is ever touched.
  Cleanup deletes session data and cannot be undone.

## 4. Verify before accepting

- Call `worker_verify` first: it re-checks state and evidence without side effects. It stays the evidence gate: never accept a task from the worker summary alone.
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
