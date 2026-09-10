# MCP Registry publication

The root `server.json` is the publication metadata for the official MCP
Registry. It describes this repository and its Streamable HTTP transport.
It advertises the optional community demo endpoint operated by ManuOtel
(`https://opencode-mcp.manuotel.com/worker-mcp`, `/worker-mcp` only); it
does not grant access and it contains no token. Users must supply their
own token. Self-host for production.

The remote URL is `https://opencode-mcp.manuotel.com/worker-mcp`. Never
commit a private endpoint, bearer token, or credential. The demo endpoint
requires its own token; production users must self-host with their own
`OPENCODE_MCP_BEARER_TOKEN`.

## Two separate things

- The public registry entry points users to this repository, its plugins, and
  its installation documentation.
- For production, a self-hosted endpoint runs the bridge and requires that
  operator's own `OPENCODE_MCP_BEARER_TOKEN`.

Production users must run OpenCode and the bridge themselves. The registry
must never be used to distribute access credentials.

## Publication checklist

1. Choose the release version and update `pyproject.toml`, both plugin
   manifests, the Claude marketplace entry, `server.json`, and `CHANGELOG.md`.
2. Confirm the advertised remote is
   `https://opencode-mcp.manuotel.com/worker-mcp` (`/worker-mcp` only).
   Confirm that it is HTTPS, owned by the publisher, and does not contain
   credentials or a token.
3. Run `python3 -m json.tool server.json`,
   `python3 -m json.tool glama.json`, and the full repository checks.
4. Run `scripts/smoke.sh` against the operator's own endpoint.
5. Run the official registry validation dry run. Do not publish from an
   unreviewed branch or from a dirty production checkout.
6. Authenticate the correct `io.github.ManuOtel` namespace before publishing.

The official registry validates namespace and package ownership in addition to
the generic `server.json` schema. A schema-valid example is not proof that a
release is ready to publish.

## Maintainer checklist

- Bump together: `pyproject.toml`, both plugin manifests, the Claude
  marketplace entry, `server.json` version, and `CHANGELOG.md`.
- Validate metadata: `python3 -m json.tool server.json`,
  `python3 -m json.tool glama.json`, plus the schema checks in
  `AGENTS.md` (never commit tokens or private endpoints).
- Endpoint checks against your own deployment only: `GET /health` is
  open; `POST /worker-mcp` and `POST /mcp` without a token return
  401; the advertised remote is HTTPS with no credentials in the URL.
- Advertise `/worker-mcp` (five worker tools, no shell). Mention
  `/mcp` only for legacy clients that need the full catalog.

## Glama

The root `glama.json` claims maintainership for `ManuOtel` under the
current schema (`https://glama.ai/mcp/schemas/server.json`, only
field `maintainers`). After merge, re-run the Claim ownership flow
on Glama to sync. Display name, description, and categories are
managed in the Glama web UI, not in the file.

## Smithery

URL publishing needs no checked-in file. The owner publishes at
`https://smithery.ai/new` (or
`smithery mcp publish "<https-url>"`) from their own public HTTPS
endpoint. This bridge uses a static Bearer token rather than OAuth,
so the Smithery scan of an auth-required endpoint needs manual
handling. Do not add invented `smithery.yaml` fields to this repo.

Discovery support (no OAuth server): the bridge serves truthful RFC
9728 protected-resource metadata with no secrets at
`GET /.well-known/oauth-protected-resource` plus the path-inserted
`/mcp` and `/worker-mcp` children, and 401s on `/mcp` and
`/worker-mcp` carry a `WWW-Authenticate: Bearer ... resource_metadata`
pointer at the matching metadata URL. The metadata intentionally
omits `authorization_servers` because the bridge operates none; it
only describes the static-Bearer resource so scanners get a valid
metadata shape instead of an auth error. Limitation: this does NOT
enable an OAuth login flow, and a full Smithery scan of the protected
endpoint still needs the operator to supply the Bearer token out of
band (or run a real OAuth authorization server, which is out of
scope). Auth is unchanged: `/mcp` and `/worker-mcp` still require
the Bearer token, and only GET/HEAD on the metadata paths bypass
it.

See the [official registry API documentation](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md)
and the [generic server.json specification](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md)
for current registry requirements.
