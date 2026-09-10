# Roadmap: release-quality coordinator plugin

This is the implementation roadmap for `opencode-mcp-bridge` as a
release-quality coordinator plugin. The bridge coordinates and verifies;
OpenCode workers do repository work in isolated worktrees.

Baseline is `origin/master` at v0.2.0 plus the merged hardening after it.
Read phases in order. Do not skip a gate to start the next phase.

## 0. Completed foundation (done, do not rebuild)

The following ships and has tests. Later phases assume it.

- Auth: static Bearer token on `/mcp` and `/worker-mcp`, constant-time
  compare, no token logging. Secondary rotation token
  (`MCP_BEARER_TOKEN_SECONDARY`) for zero-downtime rotation, fail-closed
  on blank or duplicate values. Only `GET /health` is unauthenticated.
- Worker API: five worker tools (`worker_catalog`, `worker_run`,
  `worker_status`, `worker_verify`, `worker_cleanup`) on `/worker-mcp`;
  full 16-tool catalog stays on `/mcp` for backward compatibility.
  `exec_run` is listed for compatibility but fails closed unless
  `ENABLE_EXEC_RUN=true`. `/worker-mcp` never exposes `exec_run`.
- Task reliability: idempotent `worker_run` by `requestID` (same inputs
  return `deduplicated=true`, conflicting reuse fails before side
  effects), session recreation on 404, atomic bounded task registry
  (`TASK_STATE_PATH`, max 500 records), cross-process file lock with
  bounded timeout, no prompts or secrets stored.
- Model recommendations: `worker_catalog` defaults to free plus
  connected, conservative free detection (explicit `free` marker only),
  configured free default first, paid OpenCode Go fallback marked
  `requires_explicit_request=true`. The bridge never auto-selects paid.
- Verification: `worker_verify` returns status plus a bounded read-only
  git bundle (fixed args only: `status --short`, `diff --stat`,
  `diff --check`, `diff --name-only`, `log -1 --oneline`, no shell).
- Onboarding and installers: `docs/client-setup.md` (Codex, Claude Code),
  `docs/copilot-setup.md` (Copilot family), `docs/tool-api.md`,
  `scripts/install-client.sh` with required `OPENCODE_MCP_URL`, per-name
  registration, and `--dry-run` mode. No fallback to another person's
  server. Maintainer demo endpoint is opt-in only.
- Client plugins: Codex plugin (`.codex-plugin/plugin.json`, bundled
  `.mcp.json`, marketplace `.agents/plugins/marketplace.json`) and
  Claude Code plugin (`plugins/claude-code`, marketplace
  `.claude-plugin/marketplace.json`) with worker skills.
- Operations documentation: secure deployment runbook (reverse proxy
  with TLS, systemd versus Docker scope for `exec_run`, env file `0600`,
  token rotation steps). Truthful client setup (no claim without a
  tested install path).
- Origin policy: optional `MCP_ALLOWED_ORIGINS` exact-origin allowlist
  (`scheme://host[:port]`, http/https only, no inference from Host or
  request URL). Empty means no policy (backward compatible).
- Body limits: configurable declared limit `MCP_MAX_BODY_BYTES`
  (default 1 MiB, positive integer, fail-closed) enforced on declared
  and streamed bodies, so chunked requests cannot bypass the cap.

## 1. Streamable HTTP compatibility and interop tests

Goal: prove the bridge speaks MCP Streamable HTTP correctly against
Codex, Claude Code, and one independent client (Inspector or ChatGPT
connector path).

Why it matters: clients differ in session handling, accept headers,
and error paths. An untested transport breaks installs silently.

Prerequisites: foundation in section 0; pinned `fastmcp`, `uvicorn`,
and `httpx` versions; existing dual-endpoint auth tests.

Implementation slices:

1. Document the exact wire contract: POST `/mcp` and POST
   `/worker-mcp`, required headers, auth failure shape (401), body
   limit failure shape (413), stateless session behavior.
2. Add transport regression tests: no token returns 401 on both paths;
   wrong token returns 401; oversize declared and streamed bodies
   return 413; `GET /health` stays open with minimal payload.
3. Add live interop scripts: `scripts/smoke.sh` plus Inspector steps
   for `/worker-mcp` (five tools listed) and `/mcp` (16 tools listed,
   `exec_run` fails closed by default).
4. Record tested client versions in `docs/client-setup.md`. Remove or
   fix any client row without a passing path.

Tests and evidence: `uv run pytest` transport and auth tests;
`./scripts/smoke.sh` output; Inspector screenshots or logs linked in
the PR; `curl` 401 and 413 transcripts.

Security and privacy risks: verbose error bodies can leak paths or
versions; auth probes can brute-force weak tokens. Keep errors
minimal, keep token generation at 48 random bytes, recommend TLS plus
reverse-proxy rate policy.

Definition of done: wire contract is documented; automated 401 and 413
tests pass on both endpoints; smoke plus Inspector pass; every claimed
client has a tested install path or the claim is removed.

## 2. Registry metadata and publication readiness

Goal: be ready to publish correct metadata to the official MCP
Registry without confusing a public listing with a private endpoint.

Why it matters: a registry entry advertises software, not access.
Users must install their own bridge. A wrong entry invites token
sharing and abuse reports.

Prerequisites: phase 1 contract; stable version tag; license file;
validated plugin manifests.

Implementation slices:

1. Add `server.json` (MCP Registry metadata): package identity,
   version, description, license, repository, transport declaration.
   Validate it in CI with `python3 -m json.tool`.
2. Separate two concepts in docs: (a) public registry entry pointing
   at this repo and its install docs, (b) private self-hosted endpoint
   (`https://<your-domain>/worker-mcp` plus personal Bearer token).
   State that the registry never carries URLs or tokens.
3. Add a publication checklist: version bump, tag, manifest check,
   smoke check, registry validate or publish dry run.
4. Keep Codex (`.codex-plugin/plugin.json`, `.mcp.json`) and Claude
   (`.claude-plugin/marketplace.json`) manifests coherent with the
   registry version. CI fails on mismatch.

Tests and evidence: JSON validation of `server.json` and both plugin
manifests; version coherence test (as in `test_release_coherence.py`);
a dry-run publish log attached to the PR.

Security and privacy risks: publishing an endpoint URL or token by
mistake; typosquatting by a similar package name. Review metadata by
diff; reserve the correct package name early.

Definition of done: `server.json` validates; docs state the
public-entry versus private-endpoint split; version coherence test
passes; no URL or secret appears in registry metadata.

## 3. Optional OAuth and protected-resource metadata

Goal: offer standards-compliant OAuth plus protected-resource metadata
as an option, while static Bearer token stays the simple self-hosted
default.

Why it matters: some enterprise clients expect OAuth discovery. Most
self-hosters want one token and no identity server. Both must work
without breaking existing installs.

Prerequisites: phase 1 contract; reverse proxy with TLS; a decision
on OAuth issuer scope (single deployment versus shared issuer).

Implementation slices:

1. Keep static Bearer as default. Gate OAuth behind explicit config
   (for example `MCP_AUTH_MODE=bearer|oauth`), default `bearer`.
2. Add protected-resource metadata endpoint (RFC 9728 style):
   resource identifier, authorization server pointer, supported
   scopes. Serve it unauthenticated with no sensitive data.
3. Add OAuth validation (issuer, audience, expiry, scope) before the
   existing Bearer check when OAuth mode is on. Reject failures
   closed with minimal errors.
4. Document when to use which: single-user self-host uses Bearer;
   team or enterprise use uses OAuth. Include rotation and revocation
   steps for each.

Tests and evidence: unit tests for metadata shape and token
validation; integration tests for both modes; 401 transcripts for
expired, wrong-audience, and out-of-scope tokens; backward-compat
test proving Bearer mode still works unchanged.

Security and privacy risks: open redirect, token leakage in logs,
accepting foreign issuers, scope confusion. Pin issuer and audience,
redact tokens everywhere, request minimal scopes.

Definition of done: Bearer default unchanged and tested; OAuth mode
passes validation tests; metadata endpoint exposes no secrets; docs
tell operators exactly when to pick each mode.

## 4. Multi-user identity, RBAC, quotas, cancellation, audit

Goal: draw hard boundaries when more than one human uses the bridge:
who called what, what they may touch, how much they may spend, and how
to stop and prove it.

Why it matters: a shared static token is root-equivalent. Without
identity and limits, one user can read another task, exhaust models,
or hide destructive calls.

Prerequisites: phase 3 auth decision; directory allowlist; task
registry format; structured logging baseline.

Implementation slices:

1. Identity: map each caller to a stable principal (OAuth subject or
   per-user token). Keep single-token mode for one operator; require
   identity for shared deployments.
2. Authorization and RBAC: per-principal directory roots, tool allow
   policy (for example readers cannot call `worker_run` or
   `worker_cleanup`), and a deny-by-default rule. Reuse canonical
   realpath checks already used for `ALLOWED_DIRECTORIES`.
3. Quotas and cancellation: per-principal run limits, output caps,
   and explicit abort (`worker_cleanup action=abort`) propagation to
   OpenCode sessions. Fail closed on timeout.
4. Audit: append-only record of who ran what, where, and when (tool
   name, task ID, directory, outcome, error class). Never store
   prompts, tokens, or credentials.

Tests and evidence: unit tests for allow and deny paths, quota
exhaustion, abort propagation, and audit redaction; multi-principal
integration test showing isolation; sample redacted audit log in the
PR.

Security and privacy risks: cross-principal task read, privilege
escalation through directory traversal, audit log itself becoming a
secret store. Test sibling-prefix and symlink cases; redact by
construction, not by filter.

Definition of done: shared deployments require identity; RBAC tests
pass; quotas and abort work; audit log proves who did what with no
secrets stored.

## 5. Operations: metrics, readiness, redaction, recovery, upgrades

Goal: run the bridge as a boring service: observable, restartable,
and upgradeable without surprises.

Why it matters: coordinators fail at night. Without readiness
separation and safe upgrades, deploys cause false outages or dirty
production checkouts.

Prerequisites: phases 1 and 4; systemd and Docker deploy files;
existing minimal `/health`.

Implementation slices:

1. Metrics: structured counters (tool calls, outcomes, error class,
   duration) with no prompts, paths beyond the allowed root, or
   tokens. Expose on localhost or behind auth, never public.
2. Health and readiness split: `GET /health` stays minimal liveness
   (open, `{"ok": true|false}`); add authenticated readiness that
   checks OpenCode reachability and registry writability.
3. Log redaction: central redactor for tokens, passwords, and
   `Authorization` headers; failing test if a token fixture appears
   in output.
4. Watchdog and backups: restart policy, registry backup before
   writes on upgrade, documented restore of `TASK_STATE_PATH`.
5. Upgrade and rollback procedure: deploy only from a clean release
   worktree at a tag; `uv sync --frozen`, `pytest`, smoke test;
   rollback is redeploy of the previous tag plus registry restore.

Tests and evidence: metrics endpoint test; liveness versus readiness
test; redaction test with canary tokens; backup and restore drill
log; upgrade and rollback run on staging.

Security and privacy risks: metrics labels leaking directories or
user IDs; readiness endpoint leaking backend details; backups
containing secrets. Bind metrics to localhost or auth; keep payloads
minimal; encrypt or restrict backup files (`0600`).

Definition of done: metrics, split health, redaction, watchdog, and
backup exist with tests; upgrade and rollback procedure is documented
and was drilled once.

## 6. Newer MCP capabilities (Tasks, Skills, MCP Apps)

Goal: adopt newer MCP capabilities only where they remove real pain
in this coordinator, without bloating worker context.

Why it matters: new spec features add surface and context weight.
The worker endpoint stays at five tools for a reason.

Prerequisites: phases 1 and 2; a written pain statement per proposal
(for example polling cost, skill distribution, rich client UI).

Implementation slices:

1. Evaluate MCP Tasks (or equivalent async primitive) against the
   current `worker_run` plus `worker_status` poll loop. Adopt only if
   it cuts polling or fixes cancellation. Keep the five-tool shape.
2. Evaluate MCP Skills distribution against the current Codex and
   Claude plugin skills. Adopt only if it simplifies install without
   duplicating `skills/` content.
3. Evaluate MCP Apps (rich UI) against the current text-only verify
   flow. Adopt only if diff review materially improves and stays
   read-only by default.
4. Each adoption ships as its own phase-1-style slice: spec note,
   tests, docs, and a rollback flag.

Tests and evidence: before and after measurements (context bytes,
poll count, install steps); interop test on at least one client that
supports the feature plus graceful fallback on one that does not.

Security and privacy risks: richer clients execute more content;
skills can smuggle instructions; background tasks outlive their
scope. Keep least privilege, pin skill sources, bound task lifetime.

Definition of done: each adopted capability has a pain statement,
measurements, tests, fallback behavior, and docs; rejected ideas are
recorded with reasons; worker context did not grow without proof.

## Non-goals and decisions

- The bridge coordinates and verifies. Workers do repository work.
  No code edits through the coordinator except the task record.
- No universal shared personal server as a default. Every install
  points at the operator's own bridge and token. The maintainer demo
  stays opt-in and never appears as a default.
- No credentials in git. `.env.example` documents names only. `.env`,
  tokens, and passwords stay local; history with secrets is rotated,
  not rewritten silently.
- No paid provider unless selected or authorized. Default stays free
  (`opencode/muse-spark-1.3-contributor-free`); paid fallback needs
  explicit request per task.
- No claim of client support without a tested install path. Docs list
  only clients with passing smoke or Inspector runs at a recorded
  version.
- No production deploy from a dirty checkout. Deploy only from a
  clean release worktree at a tag. `git status --porcelain` must be
  empty before deploy.
- No scope creep in worker tasks. If scope must expand, the worker
  stops and reports to the coordinator.

## Sequential worker protocol

One focused task at a time. Parallel workers never share a checkout.

1. Coordinator opens one task with explicit ownership paths (for
   example `docs/roadmap.md` only) plus model guidance (free default,
   paid only if authorized).
2. Worker creates a fresh worktree from `origin/master` (or a named
   correction branch when fixing review feedback):
   `git fetch origin` then
   `git worktree add -b <branch> /tmp/opencode/<name> origin/master`.
   Never work in the dirty production checkout
   (`/opt/opencode-mcp-bridge`) and never reuse another worker's
   worktree.
3. Worker implements only the assigned paths, with a small
   conventional commit (for example `docs(roadmap): add release
   roadmap`). One logical change per commit.
4. Worker runs checks before reporting done: `uv sync`,
   `uv run pytest`, `uv run ruff check src tests`,
   `uv run ruff format --check src tests`, `git diff --check`, plus
   JSON validation (`python3 -m json.tool`) when manifests changed.
   Read-only format check only; never run `ruff format` in write
   mode here.
5. Worker pushes the branch and opens a PR against `master` with
   branch, commit, files changed, commands run, and evidence.
6. An independent verifier reviews the diff, reruns tests, and runs a
   live check where the change needs it (smoke, Inspector, installer
   dry run). The author does not self-approve.
7. Coordinator merges passing PRs one at a time (sequential merge,
   rebase when needed), then deploys from a clean release worktree at
   the tag, runs smoke tests, and removes merged worktrees.

## Conflict and recovery rules

- `git fetch` first. Rebase the worker branch on `origin/master`
  before merge. The coordinator resolves conflicts, never the worker
  acting alone on a shared branch.
- Never force-push a shared branch. If a worktree is stale or
  broken, discard it and cut a fresh one from `origin/master`.
- On worker failure (`error` or `unknown` state), use the recovery
  skill: inspect `worker_status`, check the diff and tests, recover
  or recreate the session, never claim success from a summary alone.
- Lock timeouts fail closed: retry later, do not duplicate sessions
  or edit the registry by hand.
- Leaked token: rotate immediately with the secondary-token overlap
  procedure, then revoke the old value. Never commit the new value.

## Rollback rules

- Code rollback is redeploy of the previous tag from a clean
  worktree, plus smoke tests. No partial file copies to production.
- Registry rollback restores the pre-upgrade `TASK_STATE_PATH`
  backup. Validate JSON shape before restart.
- Config rollback unsets the new variable or restores the previous
  env file, then restarts. Fail-closed settings stay fail-closed.
- Record every rollback in the release notes with cause and tag.

## Release checklist

1. `git status --porcelain` is empty in the release worktree.
2. Pinned checks pass: `uv sync --frozen`, `uv run pytest`,
   `uv run ruff check src tests`, `uv run ruff format --check
   src tests`, `git diff --check`.
3. Manifests validate: `server.json` (when added),
   `.codex-plugin/plugin.json`, `.mcp.json`, Claude marketplace
   JSON; version coherence test passes.
4. Smoke passes: `/health`, 401 without token, 413 on oversize body,
   five tools on `/worker-mcp`, 16 tools on `/mcp`.
5. Docs list only tested install paths with client versions.
6. Tag, merge sequentially, deploy from the clean tag worktree,
   smoke again, clean up merged worktrees.

## Worker prompt template

```text
Task: <one focused change, e.g. docs(roadmap): add release roadmap>
Ownership: <exact paths, e.g. docs/roadmap.md only>
Model: free default (opencode/muse-spark-1.3-contributor-free); paid
  only if the coordinator authorized it for this task.
Steps:
1. git fetch origin; create a fresh worktree from origin/master:
   git worktree add -b <branch> /tmp/opencode/<name> origin/master.
   Do not touch /opt/opencode-mcp-bridge.
2. Edit only the ownership paths. Keep changes minimal and in scope.
3. Run: uv sync; uv run pytest; uv run ruff check src tests;
   uv run ruff format --check src tests; git diff --check.
   Validate JSON with python3 -m json.tool when manifests changed.
4. Commit conventionally, push the branch, open a PR against master.
Report: taskID + model + directory, files changed, checks with
pass/fail, evidence (diff refs, command output), open follow-ups.
Do not claim success without evidence.
```
