---
name: recover-opencode-task
description: Diagnose and recover unknown, error, or stuck opencode worker tasks safely without duplicate side effects.
---

# Recover Opencode Task

Use when `worker_status` returns `error` or `unknown`, output stalls, or the directory looks wrong.

## Diagnose first

- `unknown` usually means wrong `directory` or a gone session. Re-poll with the exact `directory` returned by `worker_run`.
- `error` means the latest assistant message carries a provider error. Read `output` before retrying; the fix may be the prompt, not the infra.
- Stuck (`running` with no output growth over several polls): re-check with `include_output=false` to confirm state cheaply, then decide.

## Act safely

- Directory mismatch: fix the directory, do not launch a second worker for the same task.
- Retry: fix the cause, then `worker_run` again once. Never fire parallel retries of the same task; duplicates cause duplicate side effects.
- Abort/delete: use `abort_session` for a live stuck session, `delete_session` or `worker_cleanup` only when the task is abandoned. Cleanup deletes session data and cannot be undone.

## After recovery

- A recovered task restarts at `delegate-to-opencode` (re-scope) and must pass `verify-opencode-work` before it counts as done.
