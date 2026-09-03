# Tool API

Worker-first bridge. Bosses use five tools; legacy tools are advanced compatibility.

## Worker tools

- `worker_catalog(query?, free_only=true, connected_only=true, limit=20)`: list free models.
  Free is conservative: only models whose ID or name contains an explicit
  `free` marker (case-insensitive) count as free. Zero token-cost metadata
  alone never counts as free; cost metadata is preserved in entries but does
  not infer billing entitlement. The configured default provider/model sorts
  first when it survives filters, then deterministic provider/model order.
- `worker_run(message, directory?, title?, agent?, providerID?, modelID?, requestID?)`: start
  background work. Returns `taskID` (= sessionID), state, model, directory, title,
  `requestID`, and `deduplicated`. Pass `requestID` for idempotent retries: same ID
  with the same inputs returns the existing task without a second session;
  conflicting reuse fails before side effects. If the recorded session no
  longer exists in OpenCode, a same-input retry recreates it
  (`deduplicated=false`); uncertain liveness keeps the stored task with no
  side effects. Every task is recorded in `TASK_STATE_PATH` JSON (bounded,
  atomic unique-temp-file writes, no prompt or credentials); prompt failure
  removes the record and deletes the session best-effort.
- `worker_status(taskID, directory?, include_output=true, max_output_chars=12000)`:
  poll state (`running`/`idle`/`error`/`unknown`) plus `messageID` and bounded
  latest output only, plus bounded `directory`. `/session/status` lists active sessions only, so an
  absent entry with a completed assistant message infers `idle`; absent with
  no assistant stays `unknown`. When `directory` is omitted, the saved task
  record supplies it.
- `worker_verify(taskID, directory?, max_output_chars=12000)`: status output plus a
  read-only git bundle (`status --short`, `diff --stat`, `diff --check`
  exit/output, changed files, `latest_commit` directory-HEAD evidence for
  information only). Uses fixed git args only, no shell. Missing or
  non-git directories return `verification.ok=false` cleanly. When `directory`
  is omitted, the saved task record supplies it.
- `worker_cleanup(taskID, directory?, action="delete")`: `abort` stops the worker;
  `delete` aborts best-effort then deletes and reports `aborted` accurately
  (false plus a generic `cleanup_warning` when the pre-delete abort fails).
  Validated before side effects. Successful `delete` removes the task record.

## Legacy tools (advanced compatibility)

`list_providers`, `list_agents`, `create_session`, `send_message`,
`list_sessions`, `get_session`, `list_messages`, `abort_session`,
`delete_session`, `get_diff`, `exec_run` (raw shell, full profile only,
opt-in via `ENABLE_EXEC_RUN=true`, disabled by default).

Security note: `/worker-mcp` is the recommended endpoint. It serves only
the five `worker_*` tools and never exposes `exec_run`. Use `/mcp` only
for legacy compatibility.

## Endpoints

Two Streamable HTTP endpoints share one Bearer token; `GET /health` stays open.

- `/mcp`: full backward-compatible 16-tool catalog for existing clients
  (`worker_*` plus `list_*`, `create_session`, `send_message`, `get_session`,
  `list_messages`, `abort_session`, `delete_session`, `get_diff`, `exec_run`).
  `exec_run` stays listed for compatibility but fails closed unless
  `ENABLE_EXEC_RUN=true`; production hosts that need it set the flag
  explicitly in the deployment env file.
- `/worker-mcp`: exactly the five `worker_*` tools (`worker_catalog`,
  `worker_run`, `worker_status`, `worker_verify`, `worker_cleanup`) so plugin
  hosts avoid context bloat. The Codex plugin (`.mcp.json`) points here.

There is no global tool-profile switch: both endpoints are always served from
the same process, so legacy clients never lose tools when workers go compact.
