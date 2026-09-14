# Tool API

Worker-first bridge. Bosses use the worker tools (five on released
code, six once the v0.3.0 code lands); legacy tools are advanced
compatibility. Anything marked v0.3.0 below requires the v0.3.0 bridge
code; on a v0.2.0 bridge the previous behavior still applies.

Sync/async choice: `worker_run` always returns immediately. Then either
wait bounded server-side with `worker_wait` (v0.3.0, no client sleep
loop) or take single snapshots with `worker_status` (all versions).
`worker_verify` stays the evidence gate before acceptance.

## Worker tools

- `worker_catalog(query?, free_only=true, connected_only=true, limit=20)`: list free models.
  Free is conservative: only models whose ID or name contains an explicit
  `free` marker (case-insensitive) count as free. Zero token-cost metadata
  alone never counts as free; cost metadata is preserved in entries but does
  not infer billing entitlement. The configured default provider/model sorts
  first when it survives filters, then deterministic provider/model order.
  `recommendations` is always present and filter-independent: rank 1 is the
  configured free default (try first), rank 2 is the paid OpenCode Go fallback
  `opencode-go/muse-spark-1.3-contributor` ("Muse Spark 1.3 Contributor",
  `requires_explicit_request=true`). The bridge never auto-selects paid.
- `worker_run(message, directory?, title?, agent?, providerID?, modelID?, requestID?)`: start
  background work and return immediately; it never blocks for completion.
  Returns `taskID` (= sessionID), state, model, directory, title,
  `requestID`, and `deduplicated`. Pass `requestID` for idempotent retries: same ID
  with the same inputs returns the existing task without a second session;
  conflicting reuse fails before side effects. If the recorded session no
  longer exists in OpenCode, a same-input retry recreates it
  (`deduplicated=false`); uncertain liveness keeps the stored task with no
  side effects. Every task is recorded in `TASK_STATE_PATH` JSON (bounded,
  atomic unique-temp-file writes, no prompt or credentials); prompt failure
  removes the record and deletes the session best-effort.
  v0.3.0 adds `timed_out=false`, `retryable=true`,
  `next_action="worker_wait"`, `error_code=null`, and an `evidence`
  object; all existing keys are unchanged.
- `worker_wait(taskID, directory?, timeout_s=30, max_output_chars=12000)` (requires v0.3.0 code):
  bounded server-side long-poll for one task. Holds the call until the
  task state or latest message changes, then returns with `changed=true`;
  at the finite deadline it returns the last-seen state with
  `changed=false` and `timed_out=true`. `timeout_s` defaults to 30 and is
  clamped to 1-120; non-finite values are rejected. No client sleep loop:
  the server re-checks OpenCode about twice per second and returns early
  on change. Output is bounded like `worker_status`. Read-only: never
  creates, prompts, or deletes a session. `next_action` mapping:
  `running` (including timeout) asks `worker_wait` again, `idle`/`error`
  ask `worker_verify`, `unknown` asks `worker_status`, `stale` asks
  `worker_cleanup`. A missing task returns `state="unknown"` immediately
  with `error_code="task_not_found"` and `retryable=false`. When
  `directory` is omitted, the saved task record supplies it.
- `worker_status(taskID, directory?, include_output=true, max_output_chars=12000)`:
  immediate snapshot fallback: one read-only look at state
  (`running`/`idle`/`error`/`unknown`/`stale`) plus `messageID` and bounded
  latest output only, plus bounded `directory`, plus `stale`, bounded `stale_reason`
  (elapsed/limit seconds only), and `recovery_hint`. `/session/status` lists active sessions only, so an
  absent entry with a completed assistant message infers `idle`; absent with
  no assistant stays `unknown`. A task still `running` with empty output past
  `TASK_STALE_AFTER_S` (default 600, 60-3600) reports `stale` with recovery
  needed; tasks with any output, non-running states, skipped output, and
  legacy records without timestamps never classify stale. When `directory` is omitted, the saved task
  record supplies it. v0.3.0 keeps every key and adds `timed_out`,
  `retryable`, `next_action`, `error_code`, and `evidence` (additive only).
- `worker_verify(taskID, directory?, max_output_chars=12000)`: status output plus a
  read-only git bundle (`status --short`, `diff --stat`, `diff --check`
  exit/output, changed files, `latest_commit` directory-HEAD evidence for
  information only). Uses fixed git args only, no shell. Missing or
  non-git directories return `verification.ok=false` cleanly. When `directory`
  is omitted, the saved task record supplies it. v0.3.0 keeps every key
  and adds `timed_out`, `retryable`, `next_action`, `error_code`, and
  `evidence` (additive only); the git bundle itself is unchanged.
- `worker_cleanup(taskID, directory?, action="delete")`: `abort` stops the worker;
  `delete` aborts best-effort then deletes and reports `aborted` accurately
  (false plus a generic `cleanup_warning` when the pre-delete abort fails).
  Validated before side effects. Successful `delete` removes the task record.
  `delete` is idempotent: when the session is already gone from OpenCode
  (404), the record is still removed and `delete` reports success with a
  generic warning. Only the given `taskID` is ever touched; unrelated
  sessions are never listed or killed. Stale workers clean up with
  `worker_cleanup(taskID, directory, action="delete")`. v0.3.0 keeps every
  key and adds `timed_out`, `retryable`, `next_action`, `error_code`, and
  `evidence` (additive only).

## Legacy tools (advanced compatibility)

`list_providers`, `list_agents`, `create_session`, `send_message`,
`list_sessions`, `get_session`, `list_messages`, `abort_session`,
`delete_session`, `get_diff`, `exec_run` (raw shell, full profile only,
opt-in via `ENABLE_EXEC_RUN=true`, disabled by default).

Security note: `/worker-mcp` is the recommended endpoint. It serves only
the worker tools (five on released code, six once the v0.3.0 code lands
with `worker_wait`) and never exposes `exec_run`. Use `/mcp` only
for legacy compatibility.

## Endpoints

Two Streamable HTTP endpoints share one Bearer token; `GET /health` stays open.

- `/mcp`: full backward-compatible catalog for existing clients
  (16 tools on released code, 17 once the v0.3.0 code lands with
  `worker_wait`: `worker_*` plus `list_*`, `create_session`, `send_message`, `get_session`,
  `list_messages`, `abort_session`, `delete_session`, `get_diff`, `exec_run`).
  `exec_run` stays listed for compatibility but fails closed unless
  `ENABLE_EXEC_RUN=true`; production hosts that need it set the flag
  explicitly in the deployment env file.
- `/worker-mcp`: only the worker tools (five on released code:
  `worker_catalog`,
  `worker_run`, `worker_status`, `worker_verify`, `worker_cleanup`;
  six once the v0.3.0 code lands, plus `worker_wait`) so plugin
  hosts avoid context bloat. The Codex plugin (`.mcp.json`) points here.

There is no global tool-profile switch: both endpoints are always served from
the same process, so legacy clients never lose tools when workers go compact.
