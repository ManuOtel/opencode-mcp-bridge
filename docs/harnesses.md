# Extended harness setup

Bring your own bridge: your OpenCode server plus your bridge deployment
plus your token plus your `https://<your-domain>/worker-mcp`. Generic
installs never point at another person's server. The maintainer demo
endpoint (`https://opencode-mcp.manuotel.com/worker-mcp`,
`/worker-mcp` only) is opt-in only, requires its own token, and is not
for production. Never commit a token, password, or private URL.

`/worker-mcp` is the recommended endpoint: it serves the eight worker
tools only (`worker_catalog`, `worker_run`, `worker_wait`,
`worker_status`, `worker_verify`, `worker_cleanup`, `worker_decide`,
`worker_resume`) and never exposes `exec_run`, so a leaked token cannot
become a direct shell. `/mcp` is the legacy full catalog (19 tools):
use it only when you explicitly need it. `exec_run` there stays listed
but fails closed unless the bridge operator sets `ENABLE_EXEC_RUN=true`.

Compatibility is protocol-level (MCP over Streamable HTTP with a Bearer
header) unless an end-to-end test is documented in this repo. Client
config keys differ per product; confirm key names in the linked official
docs before pasting. Status labels and the validation boundary live in
[compatibility.md](compatibility.md): automated wire checks prove the
bridge speaks MCP correctly, but they do not prove any vendor product
accepts the config. Every harness below is protocol-level unless marked
Unverified.

Codex and Claude Code setup stays concise in the
[README](../README.md#codex-and-claude-code) and in full detail in
[client-setup.md](client-setup.md). Copilot-family products have their
own guide at [copilot-setup.md](copilot-setup.md).

## ChatGPT Developer Mode

Developer Mode ON, then Connectors, Create connector, URL mode with
`https://<your-domain>/worker-mcp` plus your Bearer token, then Scan
Tools. Select `https://<your-domain>/mcp` only when you explicitly need
the full legacy catalog or `exec_run`.
Remote MCP connectors need an eligible plan and workspace, and may need
admin approval. Availability depends on your account, not on this repo.

Status: Unverified with a static Bearer header; official docs list
OAuth, No Authentication, and Mixed Authentication. Confirm the auth
method in the official docs before use.
Official docs:
https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta ;
https://developers.openai.com/api/docs/guides/developer-mode

## Cursor

Add to `.cursor/mcp.json` in your project (key names per
https://cursor.com/docs/context/mcp):

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "url": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

Status: protocol-level, syntax from official docs.

## VS Code

Add to `.vscode/mcp.json` (workspace) or the user `mcp.json`.
VS Code uses `servers` (not `mcpServers`) with `type: http`, and
`inputs` for secrets instead of hardcoded tokens. Key names per
https://code.visualstudio.com/docs/agents/reference/mcp-configuration:

```json
{
  "servers": {
    "opencode-bridge": {
      "type": "http",
      "url": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${input:opencode-bridge-token}"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "opencode-bridge-token",
      "description": "Bearer token for your own bridge (MCP_BEARER_TOKEN)",
      "password": true
    }
  ]
}
```

Status: protocol-level, key names from official docs. Unverified
end-to-end; see [compatibility.md](compatibility.md) for the status
boundary.

## Gemini CLI

Add to `~/.gemini/settings.json` (key names per
https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html):

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "httpUrl": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

Status: protocol-level, syntax from official docs.

## OpenHands

CLI Bearer path (key names per
https://docs.openhands.dev/openhands/usage/cli/mcp-servers):

```bash
openhands mcp add opencode-bridge --transport http \
  --header "Authorization: Bearer <paste-token-here>" \
  "https://<your-domain>/worker-mcp"
```

Replace `<paste-token-here>` with the value of `MCP_BEARER_TOKEN` on
your bridge host. Do not commit the real token. The TOML settings path
(`https://docs.openhands.dev/openhands/usage/settings/mcp-settings`)
documents `shttp_servers` with `url` plus `api_key`, not a generic
`Authorization` header.

Status: Unverified end-to-end; confirm the auth field for your
OpenHands build before use. See [compatibility.md](compatibility.md).

## Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`. Windsurf uses `serverUrl`
(not `url`) for remote servers and supports `${env:VAR}` interpolation
in `headers`. Official docs: https://docs.windsurf.com/windsurf/cascade/mcp

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "serverUrl": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${env:OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

Refresh the server list in Cascade after saving.

Status: protocol-level, key names from official docs.

## Cline

Open MCP Servers, Configure tab, Configure MCP Servers
(`cline_mcp_settings.json`), or use the Remote Servers tab with
Transport Type Streamable HTTP. Official docs:
https://docs.cline.bot/mcp/mcp-overview

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "type": "streamableHttp",
      "url": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

Set `"type": "streamableHttp"` explicitly. Omitting it falls back to
legacy SSE transport.

Status: protocol-level, key names from official docs.

## Roo Code

Client-specific shape; confirm key names in the Roo Code docs for your
version. Minimal standard form:

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "url": "https://<your-domain>/worker-mcp",
      "headers": {
        "Authorization": "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

Status: protocol-level, client-specific shape.

## Pi

Install the adapter, then add the bridge to the shared
`~/.config/mcp/mcp.json` (key names per
https://pi.dev/packages/pi-mcp-adapter):

```bash
pi install npm:pi-mcp-adapter
```

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "url": "https://<your-domain>/worker-mcp",
      "auth": "bearer",
      "bearerTokenEnv": "OPENCODE_MCP_BEARER_TOKEN",
      "includeTools": ["worker_catalog", "worker_run", "worker_wait", "worker_status", "worker_verify", "worker_cleanup", "worker_decide", "worker_resume"],
      "lifecycle": "lazy"
    }
  }
}
```

The token stays in `OPENCODE_MCP_BEARER_TOKEN`; only the variable name
is stored in the file. Servers are lazy by default and connect on first
tool call. There is no one-click plugin for this bridge; do not claim
one.

Status: protocol-level, syntax from official docs.

## Hermes Agent

Hermes uses YAML `mcp_servers` entries (key names per
https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference
and https://github.com/hermes-agent-org/hermes/blob/main/website/docs/guides/use-mcp-with-hermes.md):

```yaml
mcp_servers:
  opencode-bridge:
    url: "https://<your-domain>/worker-mcp"
    headers:
      Authorization: "Bearer ${OPENCODE_MCP_BEARER_TOKEN}"
    tools:
      include: [worker_catalog, worker_run, worker_wait, worker_status, worker_verify, worker_cleanup, worker_decide, worker_resume]
      resources: false
      prompts: false
```

Hermes resolves `${VAR}` (or `${env:VAR}`) references from its active
profile secret scope, falling back to the process environment. Put the
token in `~/.hermes/.env`; an unset variable keeps its literal
placeholder. Reload servers with `/reload-mcp` after changing config.

Status: protocol-level, syntax from official docs.

## MCP Inspector (debugging)

```bash
npx @modelcontextprotocol/inspector
```

Select Streamable HTTP transport, enter
`https://<your-domain>/worker-mcp`, and add the `Authorization: Bearer`
header in the Inspector UI. Never use a real token on a machine you do
not control. Docs: https://github.com/modelcontextprotocol/inspector

Status: debugging only, not a production client.

You can also smoke-test the deployment without a client:
`./scripts/smoke.sh` (see the script header).
