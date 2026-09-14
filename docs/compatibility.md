# Harness compatibility and first-call proof

Bring your own bridge: your OpenCode server plus your bridge deployment
plus your token plus your `https://<your-domain>/worker-mcp`. Generic
installs never point at another person's server. The maintainer demo
endpoint (`https://opencode-mcp.manuotel.com/worker-mcp`,
`/worker-mcp` only) is opt-in only, requires its own token, and is not
for production. Never commit a token, password, or private URL.

## Status labels

- `Automated`: proven by `uv run pytest` in normal CI with no network.
  Covers the wire contract (auth, body limits, tool counts) and the
  doc/config shape checks in `tests/test_adoption_proof.py`.
- `Manual`: a human ran the client against their own bridge and recorded
  the client version plus `tools/list` and `worker_catalog` output.
  No manual runs are recorded in this increment.
- `Unverified`: config syntax taken from the linked official docs only.
  No end-to-end run is claimed. Confirm key names in the official docs
  before pasting; vendors rename keys without notice.

Runtime validation boundary: automated checks prove the bridge speaks
MCP Streamable HTTP correctly (401 without a token, 413 on oversize
bodies, six worker tools on `/worker-mcp` with no `exec_run`, full
catalog on `/mcp`). They do not prove any vendor product accepts the
config, lists tools, or calls them. Every harness row below is
protocol-level unless a manual run is recorded with a client version.

No approval is claimed: no registry approval, no vendor approval, no
OAuth flow, no hosting, and no user credentials. The bridge uses a
static Bearer token. Where a vendor doc lists only OAuth or no-auth
paths, the row says so explicitly.

## Compatibility matrix

| Harness | Config keys (your own URL and token) | First-call path | Status | Source |
| --- | --- | --- | --- | --- |
| OpenAI Codex CLI | `codex mcp add <name> --url <url> --bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN` | `tools/list` on `/worker-mcp`, then `worker_catalog` | Automated (helper `--dry-run` and doc shape in CI); end-to-end Unverified | https://developers.openai.com/codex/cli/reference |
| Claude Code | `.mcp.json` `mcpServers` with `type: http`, `url`, `headers.Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}`; or `claude mcp add --transport http --header` with the `${VAR}` reference form | `tools/list` on `/worker-mcp`, then `worker_catalog` | Automated (helper `--dry-run` and doc shape in CI); end-to-end Unverified | https://docs.anthropic.com/en/docs/claude-code/mcp |
| ChatGPT custom MCP / connectors (Developer Mode) | Remote MCP connector, URL mode with your `https://<your-domain>/worker-mcp` | Connector Scan Tools, then call `worker_catalog` | Unverified; auth boundary: official developer-mode docs list OAuth, No Authentication, and Mixed Authentication, not a static Bearer header. Confirm the auth method in the official docs before use. Needs an eligible plan and workspace, plus admin approval where required. Availability depends on your account, not on this repo. | https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta ; https://developers.openai.com/api/docs/guides/developer-mode ; https://developers.openai.com/plugins/deploy/connect-chatgpt |
| Cursor | Project `.cursor/mcp.json` (or global `~/.cursor/mcp.json`) `mcpServers` with `url` plus `headers.Authorization` | `tools/list` on `/worker-mcp`, then `worker_catalog` | Automated (doc shape in CI); end-to-end Unverified | https://cursor.com/docs/mcp ; https://cursor.com/docs/context/mcp |
| VS Code | `.vscode/mcp.json` (workspace) or user `mcp.json` with `servers` (not `mcpServers`), `type: http`, `url`, `headers`; secrets via `inputs` with `${input:<id>}` or env file, never hardcoded | `tools/list` on `/worker-mcp`, then `worker_catalog` | Automated (doc shape in CI); end-to-end Unverified | https://code.visualstudio.com/docs/agents/reference/mcp-configuration ; https://code.visualstudio.com/docs/agent-customization/mcp-servers |
| Gemini CLI | `~/.gemini/settings.json` (or project `.gemini/settings.json`) `mcpServers` with `httpUrl` plus `headers` | `tools/list` on `/worker-mcp`, then `worker_catalog` | Automated (doc shape in CI); end-to-end Unverified | https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html ; https://github.com/google-gemini/gemini-cli/blob/HEAD/docs/tools/mcp-server.md |
| OpenHands | CLI: `openhands mcp add <name> --transport http --header "Authorization: Bearer <token>" <url>`; file/TOML path uses `shttp_servers` with `url` plus `api_key` and optional `timeout` | `tools/list` on `/worker-mcp`, then `worker_catalog` | Unverified; auth boundary: the TOML settings path documents `url` plus `api_key`, not a generic `Authorization` header. The CLI `--header` form is the Bearer path. Confirm the auth field for your OpenHands build in the official docs before use. | https://docs.openhands.dev/openhands/usage/settings/mcp-settings ; https://docs.openhands.dev/openhands/usage/cli/mcp-servers ; https://docs.openhands.dev/openhands/usage/cli/command-reference |
| Pi / Hermes | Pi: `pi-mcp-adapter` plus shared `~/.config/mcp/mcp.json` with `url`, `auth: bearer`, `bearerTokenEnv: OPENCODE_MCP_BEARER_TOKEN`, `includeTools` (six `worker_*`), `lifecycle: lazy`. Hermes: YAML `mcp_servers` with `url`, `headers.Authorization`, `tools.include` (six `worker_*`) | `tools/list` on `/worker-mcp`, then `worker_catalog` | Automated (doc shape in CI); end-to-end Unverified | https://pi.dev/packages/pi-mcp-adapter ; https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference ; https://github.com/hermes-agent-org/hermes/blob/main/website/docs/guides/use-mcp-with-hermes.md |

Full per-client blocks stay in `README.md` (Harness setup) and
`docs/client-setup.md` (Codex, Claude Code). This file is the matrix
plus the first-call contract; it does not duplicate those blocks.

## Clean install check (deterministic, network-free)

```bash
export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
./scripts/install-client.sh both --dry-run
```

Validates both variables, the `http(s)://` scheme, and the
`/mcp` or `/worker-mcp` suffix without invoking any client CLI and
without network. Covered by `tests/test_client_onboarding.py` and
`tests/test_adoption_proof.py`. Never prints the token value.

## First call against your own bridge (`/worker-mcp` only)

Replace `<your-domain>` with your bridge host. The token stays in the
environment and never lands in a file or a log.

```bash
export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"

# 1. Transport: exactly the worker tools, never exec_run.
curl -fsS -X POST "$OPENCODE_MCP_URL" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# Expect six tools with worker_wait on a v0.3.0 bridge
# (five without worker_wait on older v0.2.x bridges):
# worker_catalog, worker_run, worker_wait,
# worker_status, worker_verify, worker_cleanup.
```

```bash
# 2. Catalog: free default first, paid fallback explicit only.
curl -fsS -X POST "$OPENCODE_MCP_URL" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"worker_catalog","arguments":{}}}'

# Expect the configured free default
# (opencode/muse-spark-1.3-contributor-free) first.
# The paid fallback needs an explicit per-task request.
```

Unauthenticated `POST` to either endpoint returns generic `401`.
Oversize bodies return generic `413`. `GET /health` is the only open
endpoint. Use `/mcp` only for legacy full-catalog clients; it may list
opt-in `exec_run`. See `docs/operations.md` section 3 and
`docs/tool-api.md`.

## Opt-in live check (your own deployment only)

Network-free CI never runs this. Run it by hand against your own
endpoint when you need end-to-end proof:

```bash
export MCP_URL="https://<your-domain>/worker-mcp"
export OPENCODE_MCP_BEARER_TOKEN="<paste-token-here>"
./scripts/smoke.sh
```

Checks `GET /health` is 200, unauthenticated `POST` is 401, and
authenticated `tools/list` returns exactly the worker tools with no
`exec_run`. It prints counts and tool names only, never the token or
full responses. Same pattern as `docs/operations.md` section 3.

## Registry proof (metadata only, no approval claimed)

- `server.json` is schema-shaped remote Streamable HTTP metadata for
  `io.github.ManuOtel/opencode-mcp-bridge` advertising
  `https://opencode-mcp.manuotel.com/worker-mcp` (`/worker-mcp` only)
  with a required secret `Authorization` header. It supplies no token
  and grants no access. Status: not submitted and not approved by this
  change; publication still needs a human owner login per
  `docs/registry.md`.
- `glama.json` claims maintainership for `ManuOtel` only. Display
  fields live in the Glama web UI, not in this repo.
- Smithery needs no checked-in file; URL publishing is a dashboard/CLI
  flow and a protected endpoint still needs the operator to supply the
  Bearer token out of band. No OAuth login flow is claimed.

Full checklist: `docs/registry.md`. A schema-valid file is not proof
that a release is ready to publish.
