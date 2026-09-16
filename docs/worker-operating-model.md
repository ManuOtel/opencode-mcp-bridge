# Worker operating model

How this project treats OpenCode workers, how the coordinator compares
with current agent-to-agent systems, and where it goes next.

Status: baseline **v0.4.4**. Labels used through this guide:

- **Implemented**: ships in v0.4.4 with tests.
- **Partial**: ships in a limited form; the gap is named.
- **Planned**: not implemented; gated behind a forward plan in section 7.

This guide summarizes. It does not replace the source documents it
points to: `docs/tool-api.md` (tool contracts), `docs/operations.md`
(deploy and runbook), `docs/compatibility.md` (harness matrix),
`docs/roadmap.md` (release phases), and `AGENTS.md` (binding worker
rules).

## 1. Product boundary

Four splits define what this project is and is not.

**Coordinator versus worker.** The bridge coordinates and verifies.
OpenCode workers do repository work. The coordinator never edits
repository files; its only write is the task record in
`TASK_STATE_PATH`. Workers never decide scope, merge, deploy, or
approve their own work. One focused task at a time per worker.

**MCP tool plane versus OpenCode execution plane.** The MCP tool plane
is the eight `worker_*` tools on `/worker-mcp` (plus the legacy
full catalog on `/mcp`). The OpenCode execution plane is the
self-hosted OpenCode server the bridge calls: sessions, messages,
diffs. The task record (`taskID`, state, model, directory) is the only
bridge-side join between the two. A `taskID` equals the OpenCode
`sessionID` for started runs; for approval-paused runs the `taskID`
starts with `apr_` and `sessionID` is null until `worker_resume`.

**Bring-your-own bridge.** Every install points at the operator's own
bridge, own OpenCode server, own token, and own
`https://<your-domain>/worker-mcp`. Generic installs never point at
another person's server. The maintainer demo endpoint is opt-in only,
requires its own token, and is not for production. The MCP registry
entry advertises software, never a URL or token. See
`docs/compatibility.md` and `docs/registry.md`.

**Safe worker endpoint versus administrative endpoint.** `/worker-mcp`
serves exactly the eight worker tools and never exposes `exec_run`.
`/mcp` serves the full backward-compatible catalog (19 tools) and may
list opt-in `exec_run`, which fails closed unless
`ENABLE_EXEC_RUN=true`. A leaked worker token cannot become a direct
shell through `/worker-mcp`. Both endpoints share one Bearer token;
`GET /health` is the only open endpoint. See `docs/operations.md`
section 7 and `docs/tool-api.md`.

## 2. Vocabulary

Use these terms exactly. The identifiers that get confused are marked.

| Term | Meaning | Identifier | Confusion risk |
| --- | --- | --- | --- |
| Project | A repository or product scope (for example this bridge) | Name, repo URL | Project vs repository: one project can span repos |
| Repository | Git history plus tracked files | Remote URL, commit SHA | Repository vs worktree: the history is shared, checkouts are not |
| Branch | A movable line of commits | Branch name | Branch vs worktree: one branch can have several worktrees, one worktree holds one branch |
| Worktree | One isolated checkout of a branch | Filesystem path (for example `/tmp/opencode/<name>`) | **Do not confuse with branch or directory.** Never share a worktree between concurrent workers |
| Workspace / directory | The worker's bounded filesystem context | Canonical real path, must sit under `ALLOWED_DIRECTORIES` | **Do not confuse with worktree.** The directory is the allowlist boundary; the worktree is the checkout inside it |
| Worker task / session | The async execution record plus the OpenCode session | `taskID` (= `sessionID` after start; `apr_`-prefixed before approval) | **Do not confuse `taskID` with `requestID` or `messageID`.** `requestID` is client-supplied idempotency input; `messageID` identifies the latest output message |
| Artifact / evidence | Outputs plus independently verifiable proof | Commit SHA, diff stat, check output, `worker_verify` bundle | Artifact vs claim: a worker summary is a claim; a diff plus passing checks is evidence |

Rules that follow from the hierarchy:

- `taskID` identifies execution. `requestID` identifies intent
  (idempotent retry key). `sessionID` identifies the OpenCode session.
  `approval_token` authorizes decide/resume on a paused task.
  `messageID` identifies one output snapshot. Never substitute one for
  another in tool arguments.
- A missing task (`state="unknown"`, `error_code="task_not_found"`)
  means no record, not a failed worker. A failed worker is
  `state="error"` with a record. Section 5 treats them differently.
- Staleness is advisory: `stale=true` with `stale_reason` in elapsed
  and limit seconds only. It never deletes anything by itself.

## 3. Folder and project layout

Concrete tree. Each line is marked **[done]** (implemented),
**[part]** (partial), or **[new]** (proposed, not built).

```text
<repo-root>/
  AGENTS.md                    [done] binding worker rules
  README.md                    [done] setup, harness blocks
  server.json                  [done] registry metadata, no URLs or tokens
  .codex-plugin/plugin.json    [done] Codex plugin manifest
  .mcp.json                    [done] worker-endpoint client config
  plugins/claude-code/         [done] Claude Code plugin
  skills/                      [done] coordinator/worker skill packs
  docs/
    tool-api.md                [done] tool contracts
    operations.md              [done] deploy runbook
    compatibility.md           [done] harness matrix
    roadmap.md                 [done] release phases
    registry.md                [done] publication checklist
    worker-operating-model.md  [new] this guide
  deploy/                      [done] systemd unit, reverse-proxy config
  scripts/smoke.sh             [done] transport smoke
  scripts/live_conformance.sh  [part] live gate exists; harness coverage is manual-only so far
  src/opencode_mcp_bridge/     [done] bridge code (MCP plane only, never worker edits)
  tests/                       [done] network-free CI; live module skips without opt-in env
  /tmp/opencode/<name>/        [done by convention] temporary worker worktrees, removed after merge
  /var/lib/opencode-mcp-bridge/tasks.json  [done] bounded task registry (max 500 records)
  metrics/audit store          [new] structured counters and append-only audit (Plan A, v0.5.2)
  checkpoints/                 [new] durable snapshots only if Plan C gates pass (v0.6.0+)
```

What must never be stored, anywhere:

- Bearer tokens, passwords, `Authorization` header values, OAuth
  secrets. The task record and logs carry none by construction.
- Full worker prompts. `worker_run` and `worker_resume` hash the
  message for dedup matching; the prompt text is never persisted.
- Private host data: real domains, private URLs or paths, full session
  text, unredacted logs. Redact before sharing; see
  `docs/operations.md` section 6.

## 4. Worker lifecycle

Tools named below are the eight worker tools on `/worker-mcp`. Full
signatures: `docs/tool-api.md`.

```text
catalog -> scope -> launch -> wait/status -> verify
  -> approval/resume (only if paused) -> cleanup -> integration -> release
```

1. **Catalog.** Coordinator calls `worker_catalog` (defaults: free
   plus connected only). The configured free default sorts first;
   the paid fallback is marked `requires_explicit_request=true` and is
   never auto-selected. Model policy for this project is fixed; see
   section 11.
2. **Scope.** Coordinator assigns explicit ownership paths (for
   example `docs/roadmap.md` only) plus model guidance. No scope, no
   launch. Scope widening stops and reports back; workers never widen
   unilaterally.
3. **Launch.** Coordinator calls `worker_run` with a `requestID` for
   idempotent retries. Same ID plus same inputs returns the existing
   task (`deduplicated=true`); conflicting reuse fails before side
   effects. `worker_run` **always returns immediately**; it never
   blocks for completion. With `requires_approval=true` (or a bounded
   `risky_action` descriptor) the run pauses with
   `state="approval_required"`, an `apr_` taskID, and an
   `approval_token`; no session exists until resume.
4. **Wait / status.** Two different reads:
   - `worker_wait` (v0.3.0+): **sync-like bounded server-side
     long-poll**. Holds the call up to `timeout_s` (default 30,
     clamped 1-120), re-checks OpenCode about twice per second, and
     returns early on change (`changed=true`) or at the deadline with
     the last-seen state (`changed=false`, `timed_out=true`). It
     replaces client sleep-polling loops. Read-only.
   - `worker_status`: **immediate snapshot fallback**. One read-only
     look, returns at once. Use it for single checks and for tasks
     `worker_wait` cannot see (unknown tasks, approval states).
   - `next_action` in every v0.3.0+ response says what to call next
     (`worker_wait` while running, `worker_verify` on idle/error,
     `worker_status` on unknown, `worker_cleanup` on stale).
5. **Verify.** `worker_verify` is the evidence gate before acceptance:
   status output plus a bounded read-only git bundle (fixed git args,
   no shell). Missing or non-git directories return `ok=false`
   cleanly, not an exception-shaped surprise.
6. **Approval / resume.** Paused tasks only: `worker_decide` records
   approve or reject (token must match; failures change nothing),
   then `worker_resume` starts the deferred worker exactly once
   (message matched by hash, re-resume fails safely). Default runs
   skip this step entirely.
7. **Cleanup.** `worker_cleanup` with `action="abort"` (stop, keep
   record) or `action="delete"` (abort best-effort, delete session
   and record). Delete is idempotent: a session already gone from
   OpenCode still removes the record with a generic warning. Only the
   given `taskID` is ever touched.
8. **Integration.** Coordinator merges passing work sequentially
   (rebase, one at a time), reruns full checks, and resolves
   conflicts itself. Workers never resolve conflicts on shared
   branches.
9. **Release.** Deploy from a clean release worktree at a tag, run
   smoke plus conformance, then remove merged worktrees. Rollback is
   redeploy of the previous tag plus registry restore. See
   `docs/operations.md` sections 1, 3, and 5.

Async, cancellation, retry, and staleness:

- **Async operation** is the default: launch returns, `worker_wait`
  bounds each wait, `worker_status` snapshots on demand.
- **Cancellation** is explicit (`worker_cleanup action=abort`,
  propagated to the OpenCode session) and **partial**: abort is
  best-effort and reports honestly (`aborted=false` plus a generic
  warning when the pre-delete abort fails).
- **Retry** is idempotent via `requestID` before start and via
  session recreation on 404 for same-input retries. Never run
  duplicate parallel retries of the same intent.
- **Stale tasks** (`stale=true` past `TASK_STALE_AFTER_S`, default
  600, range 60-3600) need recovery: inspect `worker_status`, check
  the diff and tests, recover or recreate, then
  `worker_cleanup action="delete"`.
- **Leases and heartbeats** are **planned**, not implemented. Today
  there is no worker-side lease, no heartbeat, and no server-side
  expiry sweep beyond advisory staleness flags. Plan A (v0.5.2)
  proposes bounded leases with heartbeats behind an exit gate; see
  section 7.

## 5. Recipes

Each recipe lists conditions, exact tool names, and what evidence
counts as done. Evidence always means quoted diff plus check output,
never a worker summary alone.

### 5.1 Small documentation task

Conditions: one file, no behavior change, ownership path assigned
(for example `docs/roadmap.md` only).

1. `worker_catalog` confirms the free default first.
2. `worker_run` with a `requestID`, ownership paths, and free-model
   guidance.
3. `worker_wait` (bounded, repeat while `next_action="worker_wait"`)
   or `worker_status` snapshots.
4. `worker_verify`: `verification.ok=true`, `diff --check` clean,
   changed files limited to the ownership path.
5. `worker_cleanup action="delete"` after merge.

Done: small conventional commit on a fresh worktree branch, `uv run
pytest`, `uv run ruff check src tests`, `uv run ruff format --check
src tests`, `git diff --check` all pass, PR lists branch, commit,
files, commands, and output.

### 5.2 Implementation task in one worktree

Conditions: code or config change, single worker, isolated worktree
cut from `origin/master` (`git worktree add -b <branch>
/tmp/opencode/<name> origin/master`).

1. `worker_run` with `requestID`; worker builds only in its worktree,
   never in the dirty production checkout.
2. `worker_wait` loop; `worker_status` on `unknown`.
3. `worker_verify` plus JSON validation (`python3 -m json.tool`)
   when manifests changed.
4. Independent verifier reruns tests and, where the change needs it,
   smoke, Inspector, or installer dry run. The author does not
   self-approve.

Done: section 5.1 evidence plus a live check transcript where
applicable and a sequential merge by the coordinator.

### 5.3 Parallel independent workers

Conditions: tasks touch disjoint ownership paths; each worker gets its
own branch plus its own worktree. Concurrent writers never share a
checkout.

1. One `worker_run` (unique `requestID`) per task; distinct
   directories under the allowlist.
2. `worker_wait` per task; no cross-task session reads.
3. `worker_verify` per task; merge sequentially (rebase when needed),
   rerunning full checks between merges.

Done: each task meets its own recipe evidence; the merge sequence is
recorded; no shared-branch force-push occurred.

### 5.4 Approval-gated risky action

Conditions: any action the coordinator flags risky (production touch,
destructive command, broad scope). Default runs start immediately; a
risky run must pause first.

1. `worker_run` with `requires_approval=true` (or bounded
   `risky_action`, max 100 chars). Returns
   `state="approval_required"`, `apr_` taskID, `approval_token`,
   `expires_at`. Keep `TASK_STATE_PATH` owner-only (`0600`): the
   token is bearer-equivalent and persisted there.
2. Human reviews the paused record, then `worker_decide` with
   `approve` or `reject` plus the token.
3. On approve, `worker_resume` with the same message starts the worker
   exactly once (`state="resumed"`). On reject or expiry, re-scope
   and start a new `worker_run`; never resume a rejected task.
4. Normal wait, verify, cleanup flow follows.

Done: the decide and resume responses show exact-once start (one
`sessionID`, duplicate resume fails safely), plus standard verify
evidence. Approval exact-once is a benchmark dimension; see
section 8.

### 5.5 Worker failure before start versus after start

These are different failures with different handling. Do not confuse
catalog/start unavailability with post-start worker failure.

**Before start (no session exists):** `worker_catalog` shows no
connected free model, or `worker_run` cannot start (OpenCode
unreachable, lock timeout fails closed, validation rejects inputs).
Handling: fix the cause (server, config, inputs), then retry once per
the model policy in section 11. Never switch models after start
because nothing started. Evidence: the error response plus
`error_code`, `retryable`, and `next_action` fields.

**After start (a session exists):** `worker_status` or `worker_wait`
reports `state="error"`, or the task goes `unknown` with a record
(session deleted out of band). Handling: use the recovery path,
inspect `worker_status`, check the diff and tests, recover or
recreate the session, never claim success from a summary. Evidence:
status output, diff, test output, and the recovery decision.

### 5.6 Conflict resolution and sequential integration

Conditions: two merged-capable branches overlap, or a worker branch
went stale against `origin/master`.

1. `git fetch origin`; rebase the worker branch on `origin/master`.
2. The coordinator resolves conflicts, never the worker acting alone
   on a shared branch.
3. Rerun full checks after resolution; merge one PR at a time.
4. If a worktree is stale or broken, discard it and cut a fresh one
   from `origin/master`. Never force-push a shared branch.

Done: clean rebase, passing checks post-resolution, sequential merge
record, stale worktrees removed.

### 5.7 Release and deploy proof

Conditions: release worktree at a tag, `git status --porcelain`
empty.

1. Pinned checks: `uv sync --frozen`, `uv run pytest`, `uv run ruff
   check src tests`, `uv run ruff format --check src tests`,
   `git diff --check`.
2. Manifest validation and version coherence test pass.
3. Smoke: `/health` 200, 401 without token on both endpoints, 413 on
   oversize body, 8 tools on `/worker-mcp` with no `exec_run`, 19 on
   `/mcp`.
4. Opt-in live conformance gate (`scripts/live_conformance.sh`):
   one disposable free-worker run, duplicate `requestID`, status,
   bounded `worker_wait`, verify, cleanup.
5. Tag, deploy from the clean tag worktree, smoke again, clean up
   merged worktrees.

Done: check transcripts, smoke output, gate report (`endpoint`,
`health_url`, revision, tool counts, `PASS`, time). Any `FAIL` is a
failed deploy; roll back per `docs/operations.md` section 5.

## 6. SOTA capability inventory

Baseline v0.4.4 against current agent-to-agent patterns. This project
**does not claim native A2A compatibility**: there is no Agent Card,
no JSON-RPC A2A endpoint, no TCK run, and no version negotiation
against the A2A spec. Anything resembling A2A concepts below is
pattern similarity, not conformance.

| # | Capability (source) | v0.4.4 status | Notes |
| --- | --- | --- | --- |
| 1 | A2A Agent Cards: machine-readable capability advertisement ([spec](https://a2a-protocol.org/latest/specification/), [whats-new v1](https://a2a-protocol.org/latest/whats-new-v1/)) | Planned | `server.json` advertises MCP registry metadata, not an Agent Card. A thin facade is Plan B, gated on stable MCP contracts |
| 2 | A2A tasks, context IDs, artifacts ([spec](https://a2a-protocol.org/latest/specification/)) | Partial | `taskID`/`requestID`/bounded output/`worker_verify` bundle cover the shape informally; no `contextId` correlation, no portable artifact schema |
| 3 | A2A streaming and push notifications ([spec](https://a2a-protocol.org/latest/specification/)) | Partial | `worker_wait` long-poll streams state changes server-side; no SSE event stream, no webhook push |
| 4 | A2A cancellation and version negotiation ([spec](https://a2a-protocol.org/latest/specification/), [TCK](https://github.com/a2aproject/a2a-tck), [samples](https://github.com/a2aproject/a2a-samples)) | Planned | Abort/delete exist but are not A2A-cancel; no protocol version negotiation. No TCK run exists or is claimed |
| 5 | MCP Tasks: async primitives, output schemas, progress, cancellation, isolation ([tasks spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks), [registry](https://tasks.extensions.modelcontextprotocol.io/), [schema](https://modelcontextprotocol.io/specification/2025-11-25/schema)) | Partial | `worker_run` plus bounded `worker_wait` plus `next_action`/`error_code`/`evidence` fields prefigure task semantics; no spec task objects, no output-schema negotiation, no task isolation primitive |
| 6 | Manager pattern and agents-as-tools ([OpenAI agents](https://openai.github.io/openai-agents-python/multi_agent/)) | Partial | Coordinator plus scoped workers match the manager shape; no framework handoffs primitive, handoffs are procedural (ownership paths, sequential merge) |
| 7 | Agent handoffs with structured context transfer ([OpenAI agents](https://openai.github.io/openai-agents-python/multi_agent/)) | Planned | Today handoffs are task records plus branch/PR prose. No typed handoff envelope |
| 8 | Guardrails on risky actions ([OpenAI guardrails](https://openai.github.io/openai-agents-python/guardrails/)) | Partial | Approval pause (`worker_decide`/`worker_resume`, TTL-bounded, token-bound, exact-once) gates risky starts; no input/output content guardrails |
| 9 | Durable checkpoint and resume, human interrupts ([LangGraph persistence](https://langchain-ai.github.io/langgraph/concepts/persistence/), [breakpoints](https://langchain-ai.github.io/langgraph/concepts/breakpoints/)) | Partial | Approval pause plus idempotent `requestID` plus 404 session recreation give resume-before-start; no mid-run checkpointing, no breakpoint injection into a live session |
| 10 | Durable execution, leases, retries ([Temporal](https://docs.temporal.io/)) | Planned | Bounded registry plus file lock plus idempotent launch are the durability floor; no workflow engine, no leases, no scheduled retries |
| 11 | Traces and GenAI agent/tool spans ([OTel semconv](https://opentelemetry.io/docs/specs/semconv/general/), [OpenAI tracing](https://openai.github.io/openai-agents-python/tracing/)) | Planned | Structured counters exist in part (`observability.py`); no OTel spans, no GenAI semantic conventions |
| 12 | Workspace and sandbox agent servers ([OpenHands agent server](https://docs.openhands.dev/sdk/arch/agent-server)) | Partial | Strict directory allowlist plus per-task worktrees plus safe/administrative endpoint split give workspace isolation; no sandbox agent-server process model, no container-per-task |

## 7. Forward plans

**Recommendation: Plan A first.** Stabilize MCP-native coordination
before any facade or backend. Plan B and Plan C are gated and
deferred, not parallel tracks.

### Plan A: MCP-native task coordinator (recommended first)

Harden what v0.4.4 already is: six-to-eight worker tools, bounded
waits, idempotent launch, approval gating, verify-before-accept.

- v0.5.0: structured audit plus metrics with redaction tests; split
  liveness (`GET /health`) from authenticated readiness (OpenCode
  reachability, registry writability).
- v0.5.1: per-principal identity, RBAC (directory roots, tool allow
  policy, deny by default), quotas, abort propagation.
- v0.5.2: leases plus heartbeats for live tasks, server-side expiry
  sweep, exactly-once approval under concurrency (prove with a
  contention test).
- Gate to proceed past Plan A: benchmarks in section 8 green for two
  consecutive nightly runs; zero redaction failures; abort
  propagation proven live.

### Plan B: thin A2A facade after MCP contracts stabilize

Only after Plan A gates pass. A minimal translation layer (Agent Card
plus task send/subscribe/cancel mapped onto the worker tools),
stateless, no second source of truth.

- v0.5.3: read-only Agent Card generated from `server.json` plus
  `worker_catalog`; no task surface yet.
- v0.5.4: task facade (send, status, cancel) mapped 1:1 onto
  `worker_run`/`worker_status`/`worker_cleanup`; push mapped onto
  `worker_wait` semantics where the client cannot long-poll.
- Gate: run the [A2A TCK](https://github.com/a2aproject/a2a-tck)
  against the facade and record results; any claimed endpoint must
  pass. Until then, repeat: **no A2A compatibility claim**.
  Reference samples: [a2a-samples](https://github.com/a2aproject/a2a-samples).
  Spec: [A2A specification](https://a2a-protocol.org/latest/specification/),
  [whats-new v1](https://a2a-protocol.org/latest/whats-new-v1/).

### Plan C: durable backend only on evidence

Only if file-registry, crash, or multi-node evidence justifies it:
registry corruption under concurrency, restarts losing task state, or
a second bridge node becoming real.

- v0.6.0: crash/restart and upgrade drills green; registry backup and
  restore drilled; watchdog plus restart policy documented.
- Optional v0.7.0: pluggable durable store (SQLite first, never a
  second protocol first), migration with rollback, multi-node lock
  story.
- Gate: at least one recorded incident class from the list above with
  a failing-then-passing drill. Without that evidence, Plan C stays
  shelved.

### Release mapping

| Release | Plan | Exit gate |
| --- | --- | --- |
| v0.5.0 | A: observability | Metrics plus readiness with tests; redaction canary passes |
| v0.5.1 | A: identity/RBAC/quotas | Multi-principal isolation test passes; audit redacted |
| v0.5.2 | A: leases/heartbeats | Expiry sweep plus approval exact-once under contention |
| v0.5.3 | B: Agent Card (read-only) | Card validates; no task surface; no compat claim |
| v0.5.4 | B: task facade | A2A TCK run recorded; only passing endpoints claimed |
| v0.6.0 | C: crash/upgrade drills | Restart plus upgrade plus rollback drilled on staging |
| v0.7.0 (optional) | C: durable store | Migration with rollback proven; shelved without evidence |

Rollback posture for every release: redeploy the previous tag from a
clean worktree plus smoke; restore the pre-upgrade `TASK_STATE_PATH`
backup after JSON validation; unset or restore the previous env file
for config rollback. Record every rollback in the release notes with
cause and tag. Full procedure: `docs/operations.md` sections 1 and 5.

## 8. Benchmark and evaluation plan

Cheap CI runs on every change; nightly and live tests run on schedule
or by hand against the operator's own deployment. Network-free CI
never contacts a live bridge.

| Dimension | What is measured | Cheap CI | Nightly / live |
| --- | --- | --- | --- |
| Contract and conformance | Auth (401), body limits (413), tool counts, wire shape | `uv run pytest` transport/auth/contract tests | `scripts/smoke.sh` plus Inspector against own deployment |
| Launch-to-first-output latency | p50/p95 from `worker_run` return to first output byte | Simulated harness with recorded fixtures | Disposable free-worker runs, one harness per supported path |
| Completion latency | p50/p95 from `worker_run` to terminal state | Not in CI (needs execution) | Nightly sampled runs per task class (docs, code, verify) |
| Wait efficiency | `worker_wait` call count per task vs legacy sleep-poll baseline | Unit: change-detection and deadline behavior | Count calls per completed task; target fewer calls, same freshness |
| Context and token budget | Output chars fetched per task (`max_output_chars`, `include_output=false` state-only waits) | Boundedness assertions | Bytes per task class; state-only wait share |
| Duplicate-session rate | Sessions created per `requestID` under concurrent retries | Concurrency unit test: same ID, same inputs | Concurrent retry drill; target exactly one session |
| Cleanup and stale recovery | Stale detection precision; record removal on delete; 404-delete idempotency | `test_worker_lifecycle_stale.py` and lifecycle tests | Stale drill plus restart drill |
| Approval exact-once | One session per approved task under duplicate decide/resume | `test_approval_resume.py` contention cases | Live duplicate-resume drill |
| Artifact and evidence completeness | `worker_verify` bundle present; diff-check clean; commit linked | Shape tests on verify output | Manual review sample per release |
| Crash, restart, upgrade | Registry survival, backup/restore, rollback time | Backup/restore unit drill | Staging restart plus upgrade drill per Plan C gate |
| Live smoke per harness | One `tools/list` plus `worker_catalog` per supported harness | Doc-shape checks (`test_adoption_proof.py`) | One live path per harness, recorded client version; unrecorded rows stay Unverified |

Targets are set per release in the PR that introduces the benchmark,
not in this guide, so numbers stay honest about the hardware they
were measured on. Every benchmark report records bridge version,
OpenCode version, host class, and sample size.

## 9. Security, privacy, and adoption rules

Binding rules, not advice. Violations block merge.

- No prompt or token storage in task state or logs. Prompts are
  hashed for dedup, never persisted. Audit records carry tool name,
  task ID, directory, outcome, and error class only.
- Strict directory allowlist. `ALLOWED_DIRECTORIES` is canonicalized
  by realpath; sibling-prefix and symlink escapes are tested and
  denied. Workers operate only inside their assigned directory.
- The safe endpoint excludes `exec_run`. `/worker-mcp` never serves
  it. `/mcp` lists it for compatibility but it fails closed unless
  `ENABLE_EXEC_RUN=true` is set explicitly in the deployment env
  file.
- Per-principal auth, RBAC, quotas, and audit are **planned** (Plan A
  v0.5.1). Today one shared Bearer token is root-equivalent: do not
  share deployments across trust boundaries without identity.
- No tracking by default. No analytics, no telemetry export, no
  external calls. OTel tracing is planned (section 6, row 11) and
  stays off unless an operator enables it.
- Truthful bring-your-own-bridge wording. Never claim vendor
  approval, registry approval, hosting, OAuth flows, or user
  credentials. Every harness row without a recorded manual run at a
  client version is Unverified at end-to-end level. See
  `docs/compatibility.md`.

## 10. Checklists

### How to extend this project (contributors)

1. Read `AGENTS.md`, `docs/roadmap.md`, this guide, then the spec doc
   for your area (`docs/tool-api.md`, `docs/operations.md`).
2. Take one task with explicit ownership paths. One branch plus one
   worktree per worker from `origin/master`. Never share a checkout.
3. Confirm the model per section 11 before `worker_run`. Paid use
   needs explicit per-task authorization recorded in the task.
4. Keep the six-tool worker shape. New MCP capabilities need a pain
   statement, before/after measurements, tests, fallback, and docs
   (roadmap phase 6).
5. Redact by construction: no prompts, tokens, or private host data
   in state, logs, or docs. Add a canary test if you touch logging.
6. Run the full gate before reporting done: `uv sync`, `uv run
   pytest`, `uv run ruff check src tests`, `uv run ruff format
   --check src tests`, `git diff --check`, plus JSON validation for
   manifests. Read-only format check; never `ruff format` in write
   mode here.
7. Report taskID plus model plus directory, files changed, checks
   with pass/fail, evidence, and open follow-ups. One conventional
   docs or code commit. No push, no PR without authorization.

### Definition of done (worker tasks)

- [ ] Ownership paths respected; no file outside scope touched.
- [ ] Fresh worktree from `origin/master`; dirty production checkout
  untouched.
- [ ] Checks pass: `pytest`, `ruff check`, `ruff format --check`,
  `git diff --check` (plus JSON validation where relevant).
- [ ] `worker_verify` shows `ok=true`, clean diff check, and only
  intended files changed.
- [ ] Independent evidence quoted (diff refs, command output); no
  success claimed from a summary.
- [ ] `worker_cleanup` run (or record kept deliberately with a
  reason); merged worktrees removed after integration.
- [ ] Follow-ups and limitations reported honestly.

## 11. Model policy for this project

- Start with the connected free model
  `opencode/muse-spark-1.3-contributor-free`. Confirm it in
  `worker_catalog` before launching.
- If the free model is absent or unavailable in `worker_catalog`, or
  `worker_run` cannot start on it, **one explicit retry** with
  `opencode-go/muse-spark-1.3-contributor` is pre-authorized for this
  project. Record the fallback and the reason in the task report.
- Never use Copilot or any other paid model.
- Never switch models after start. Never run duplicate parallel
  retries of the same `requestID`.
- Distinguish the two failure classes from section 5.5: catalog/start
  unavailability (nothing started; the one retry above applies) versus
  post-start worker failure (a session exists; recover, do not
  re-launch in parallel).
