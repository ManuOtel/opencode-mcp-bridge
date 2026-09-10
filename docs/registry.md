# MCP Registry publication

The root `server.json` is a checked-in metadata example for the official MCP
Registry. It describes this repository and its Streamable HTTP transport.
It is not a hosted service and it does not grant access to a bridge.

The remote URL is `https://example.invalid/worker-mcp` on purpose. Replace it
with the operator's real HTTPS endpoint only in a publication-specific change.
Never commit a private endpoint, bearer token, or shared deployment address.

## Two separate things

- The public registry entry points users to this repository, its plugins, and
  its installation documentation.
- A private self-hosted endpoint runs the bridge and requires that operator's
  own `OPENCODE_MCP_BEARER_TOKEN`.

Users must run OpenCode and the bridge themselves. The registry must never be
used to distribute access credentials.

## Publication checklist

1. Choose the release version and update `pyproject.toml`, both plugin
   manifests, the Claude marketplace entry, `server.json`, and `CHANGELOG.md`.
2. Replace the example remote only in the publication input. Confirm that it
   is HTTPS, owned by the publisher, and does not contain credentials.
3. Run `python3 -m json.tool server.json` and the full repository checks.
4. Run `scripts/smoke.sh` against the operator's own endpoint.
5. Run the official registry validation dry run. Do not publish from an
   unreviewed branch or from a dirty production checkout.
6. Authenticate the correct `io.github.manuotel` namespace before publishing.

The official registry validates namespace and package ownership in addition to
the generic `server.json` schema. A schema-valid example is not proof that a
release is ready to publish.

See the [official registry API documentation](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md)
and the [generic server.json specification](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md)
for current registry requirements.
