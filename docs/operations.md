# Operations runbook (host systemd and Docker)

Operator checklist for release, deploy, verify, rotate, roll back,
and inspect this bridge. Docs only; no code or policy change here.

Conventions used below:

- `https://<your-domain>` is your own edge URL (placeholder only).
- `http://127.0.0.1:8087` is the local origin default (`MCP_HOST`/`MCP_PORT`).
- `BASE` is the origin or edge base you are checking.
- `MCP_BEARER_TOKEN` and `MCP_BEARER_TOKEN_SECONDARY` are env-var
  references only. Never paste real tokens, passwords, or
  `Authorization` header values into docs, issues, or logs.
- Release dir `/opt/opencode-mcp-bridge` and env file
  `/etc/opencode-mcp-bridge/env` match
  `deploy/opencode-mcp-bridge.service`. Docker paths match
  `docker-compose.yml`. Adjust only by renaming the placeholder consistently.

See also: `README.md` (local run and config table), `SECURITY.md`
(rotation on leak), `docs/tool-api.md` (endpoint catalog),
`deploy/opencode-mcp-bridge.service`,
`deploy/traefik-opencode-mcp.yaml`, `scripts/smoke.sh`.

## 1. Clean release checkout (never overwrite a dirty tree)

Deploy from a fresh checkout or a separate worktree. Never `git pull`
over a live or dirty release dir.

```bash
git fetch origin
git rev-parse origin/master
git status --short  # must be empty; if not, stop and resolve first
```

Fresh host release:

```bash
sudo mkdir -p /opt/opencode-mcp-bridge /etc/opencode-mcp-bridge
sudo git clone <repo-url> /opt/opencode-mcp-bridge
cd /opt/opencode-mcp-bridge
git checkout <tag-or-sha>
```

Worktree update for the next release (keeps the live dir untouched
until cutover):

```bash
git fetch origin
git worktree add /tmp/bridge-release-<tag-or-sha> <tag-or-sha>
cd /tmp/bridge-release-<tag-or-sha>
```

Pre-deploy gate: `git status --short` must be empty in the source and
the target. If either is dirty, stop. Do not copy a dirty tree over
`/opt/opencode-mcp-bridge`.

## 2. Persistent systemd configuration (host mode)

The unit file is `deploy/opencode-mcp-bridge.service`. Persistent state:

- Env file: `/etc/opencode-mcp-bridge/env`, mode `0640`,
  owner `root`, group `opencode-mcp` (matches
  `install -m 640 -o root -g opencode-mcp` below and
  `deploy/opencode-mcp-bridge.service`; group read lets the
  `opencode-mcp` service user read it, `0600` would block it).
  Holds `MCP_BEARER_TOKEN`,
  `OPENCODE_SERVER_PASSWORD`, and optional rotation/second-level vars.
  Never commit this file.
- Working dir: `/opt/opencode-mcp-bridge`, owned `root:root`.
- State dir: `/var/lib/opencode-mcp-bridge` (created by
  `StateDirectory=`), owner `opencode-mcp:opencode-mcp`, mode `0750`.
  Default `TASK_STATE_PATH` lives here. If overridden, point
  `ReadWritePaths=` at that directory instead.
- Service user: `opencode-mcp` (non-root, `NoNewPrivileges=true`).

Install or refresh:

```bash
cd /opt/opencode-mcp-bridge && uv sync --frozen --no-dev
sudo chown -R root:root /opt/opencode-mcp-bridge
sudo install -m 640 -o root -g opencode-mcp /path/to/env /etc/opencode-mcp-bridge/env
sudo cp deploy/opencode-mcp-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opencode-mcp-bridge
```

Config change only (env file edited):

```bash
sudo systemctl daemon-reload
sudo systemctl restart opencode-mcp-bridge
sudo systemctl is-enabled opencode-mcp-bridge
sudo systemctl status opencode-mcp-bridge --no-pager
```

Docker mode instead: fill `.env` (never commit), then
`docker compose up -d` after reviewing `docker-compose.yml`. The
origin stays bound to loopback; TLS terminates upstream.

## 3. Pre/post-deploy checks

Run unauthenticated checks first, then authenticated Streamable HTTP
checks. `scripts/smoke.sh` automates the same sequence:

```bash
BASE="http://127.0.0.1:8087" MCP_BEARER_TOKEN="<paste-token-here>" ./scripts/smoke.sh
```

Against your edge, set `BASE="https://<your-domain>"` with the same
token variable. Manual equivalents:

```bash
curl -fsS "$BASE/health"
```

Expect HTTP 200 with a minimal body and no token required. This is the
only unauthenticated endpoint.

```bash
for path in mcp worker-mcp; do
  curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/$path" \
    -H 'Content-Type: application/json' -d '{}'
done
```

Expect `401` on both `/mcp` and `/worker-mcp` without a Bearer token.
If either returns anything else, stop.

```bash
for path in mcp worker-mcp; do
  curl -fsS -X POST "$BASE/$path" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
done
```

Expect a valid `initialize` result on both paths.

```bash
for path in mcp worker-mcp; do
  curl -fsS -X POST "$BASE/$path" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
done
```

Expect 16 tools on `/mcp` and 5 tools on `/worker-mcp`.
Then call `worker_catalog` over `/worker-mcp` (defaults: free plus
connected only) and confirm the configured default model is listed
first before routing work.

Post-deploy: repeat health, both 401 checks, both `initialize` calls,
both `tools/list` counts, and one `worker_catalog` call. Any mismatch
is a failed deploy; roll back per section 5.

## 4. Safe bearer rotation (primary plus secondary)

`MCP_BEARER_TOKEN` is root-equivalent. `MCP_BEARER_TOKEN_SECONDARY`
holds at most one overlap token. Blank or duplicate secondary values
fail startup closed. Both tokens grant `/mcp` and `/worker-mcp`;
comparison is constant-time and values are never logged. Full policy:
`SECURITY.md`.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

1. Generate a new token into a local variable or password manager.
   Never print it into shared logs.
2. Set it as `MCP_BEARER_TOKEN_SECONDARY` in
   `/etc/opencode-mcp-bridge/env` (host) or `.env` (Docker) and
   restart: `sudo systemctl restart opencode-mcp-bridge` or
   `docker compose up -d`.
3. Verify both tokens work (health plus one authenticated
   `tools/list` per token), then move clients to the new token.
4. Promote the new value to `MCP_BEARER_TOKEN`, unset the secondary
   line, and restart again.
5. Verify the old token now returns `401` on both `/mcp` and
   `/worker-mcp`. Never commit either value.

On leak, treat it as full compromise of that token and rotate at once
in the same order.

## 5. Rollback

Keep the previous release dir, worktree, or image tagged until the new
release passes section 3.

Host systemd (never `git checkout` inside the live or dirty
`/opt/opencode-mcp-bridge`; same clean-release rule as section 1):

```bash
git -C /opt/opencode-mcp-bridge rev-parse HEAD  # read-only evidence only
git fetch origin
git worktree add /tmp/bridge-release-<previous-tag-or-sha> <previous-tag-or-sha>
cd /tmp/bridge-release-<previous-tag-or-sha>
git status --short  # must be empty; if not, stop
git rev-parse HEAD
```

Cut over the persistent `/opt/opencode-mcp-bridge` path from the
clean worktree, preserving the failed checkout for forensics:

```bash
cd /tmp/bridge-release-<previous-tag-or-sha> && uv sync --frozen --no-dev
sudo systemctl stop opencode-mcp-bridge
sudo mv /opt/opencode-mcp-bridge "/opt/opencode-mcp-bridge-failed-$(date -u +%Y%m%dT%H%M%SZ)"
sudo mv /tmp/bridge-release-<previous-tag-or-sha> /opt/opencode-mcp-bridge
sudo chown -R root:root /opt/opencode-mcp-bridge
sudo systemctl daemon-reload
sudo systemctl restart opencode-mcp-bridge
```

Then repeat the full section 3 checks. If they still fail, restore
`/etc/opencode-mcp-bridge/env` and `TASK_STATE_PATH` from backup and
restart again. Do not roll forward with a dirty checkout.

Docker:

```bash
docker compose down
docker image tag opencode-mcp-bridge:latest opencode-mcp-bridge:<previous-tag-or-sha>
docker compose up -d
docker compose ps
```

Then repeat section 3. Keep the tasks volume
(`opencode-mcp-tasks`) intact; it holds `tasks.json`.

## 6. Logs and status

Host systemd:

```bash
sudo systemctl status opencode-mcp-bridge --no-pager
journalctl -u opencode-mcp-bridge --since "30 min ago" --no-pager
curl -fsS http://127.0.0.1:8087/health
```

Docker:

```bash
docker compose ps
docker compose logs --since 30m bridge
```

Redact before sharing: strip `Authorization` headers, bearer tokens,
passwords, private URLs/paths, and full session text. Structured bridge
logs never include token values; keep it that way when pasting.

## 7. Endpoint warning: `/mcp` versus `/worker-mcp`

WARNING: the full `/mcp` endpoint may expose opt-in `exec_run`.
`exec_run` is listed for backward compatibility but fails closed unless
the deployment env file sets `ENABLE_EXEC_RUN=true`. When enabled, plus
open directories, anyone with the Bearer token has an unsandboxed shell
where the bridge runs: full host shell under host systemd (as the
non-root `opencode-mcp` user), container-scoped shell under Docker (as
the non-root bridge user). Enable it only where a shell is intended;
prefer session and worker tools for code edits.

`/worker-mcp` never exposes `exec_run`. It serves exactly the five
worker tools (`worker_catalog`, `worker_run`, `worker_status`,
`worker_verify`, `worker_cleanup`), so a leaked worker token cannot
become a direct shell through this endpoint. Use `/worker-mcp` for
worker clients; reserve `/mcp` for legacy full-catalog use.

Both endpoints share the same Bearer token and rotation procedure.
`/health` stays open for reverse-proxy checks.

Request-body limit: `MCP_MAX_BODY_BYTES` (default 1048576, 1 MiB) applies
to `/mcp` and `/worker-mcp` on both declared `Content-Length` and
streamed/chunked/unknown-length bodies. Oversized requests return generic
413 before any tool runs; absent, malformed, or under-declared lengths are
still counted with bounded coalesced buffering (one message, identical
bytes) and do not bypass the limit. Auth runs first, so missing tokens
stay 401. `/health` is exempt.

Optional browser-origin policy: set `MCP_ALLOWED_ORIGINS` in the
deployment env file (same `0640` file as the tokens) as comma-separated
exact origins (`scheme://host[:port]`, http/https, no path/query/fragment;
a single trailing slash is stripped). Unset or blank disables the policy.
When enabled, it applies only to `/mcp` and `/worker-mcp` after auth: a
present `Origin` must match exactly, `Origin`-absent requests fall back to
deriving the `Referer` origin (malformed `Referer` is rejected), and absent
`Origin` plus absent `Referer` stays allowed for CLI/SDK clients. Missing
tokens still return 401; disallowed browser requests return generic 403.
Verify after enabling (replace the origin with one of your configured
values for the allowed case):

```bash
for path in mcp worker-mcp; do
  curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/$path" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Origin: https://allowed.example' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
  curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/$path" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Origin: https://evil.example' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
done
```

Expect `200` for the configured origin and generic `403` for the other,
on both paths. Requests without `Origin`/`Referer` must still return
`200` with a valid token, and requests without a token must still return
`401` regardless of `Origin`.
