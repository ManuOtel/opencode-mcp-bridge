# Tool API

Worker-first bridge. Bosses use five tools; legacy tools are advanced compatibility.

## Worker tools

- `worker_catalog(query?, free_only=true, connected_only=true, limit=20)`: list free models.
- `worker_run(message, directory?, title?, agent?, providerID?, modelID?)`: start
  background work. Returns `taskID` (= sessionID), state, model, directory, title.
- `worker_status(taskID, directory?, include_output=true, max_output_chars=12000)`:
  poll state (`running`/`idle`/`error`/`unknown`) plus `messageID` and bounded
  latest output only. `/session/status` lists active sessions only, so an
  absent entry with a completed assistant message infers `idle`; absent with
  no assistant stays `unknown`.
- `worker_verify(taskID, directory?, max_output_chars=12000)`: status output plus a
  read-only git bundle (`status --short`, `diff --stat`, `diff --check`
  exit/output, changed files, `latest_commit` directory-HEAD evidence for
  information only). Uses fixed git args only, no shell. Missing or
  non-git directories return `verification.ok=false` cleanly.
- `worker_cleanup(taskID, directory?, action="delete")`: `abort` stops the worker;
  `delete` aborts best-effort then deletes and reports `aborted` accurately
  (false plus a generic `cleanup_warning` when the pre-delete abort fails).
  Validated before side effects.

## Legacy tools (advanced compatibility)

`list_providers`, `list_agents`, `create_session`, `send_message`,
`list_sessions`, `get_session`, `list_messages`, `abort_session`,
`delete_session`, `get_diff`, `exec_run` (raw shell, full profile only).

## Profiles

`OPENCODE_MCP_TOOL_PROFILE=full` (default) exposes every tool. `worker` exposes
only the five `worker_*` tools so plugin hosts avoid context bloat. Invalid
values fail fast with a clear error.
