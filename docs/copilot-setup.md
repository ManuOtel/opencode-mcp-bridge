# Copilot-family setup

This guide covers three distinct cases. Do not conflate them. Each case has its own steps and limits.

- Case 1: GitHub Copilot cloud agent and code review, through repository MCP settings.
- Case 2: Microsoft Copilot Studio, through an MCP tool on your own agent.
- Case 3: Microsoft 365 Copilot, through a declarative agent or an admin-approved extension.

This repo does not host OpenCode for you. You host your own bridge and your own OpenCode server. Then you connect a Copilot product to your own bridge URL.

## Own-bridge-first rule

CAUTION: Use only your own bridge URL and your own token. The generic steps never point at another person's server.

The bridge URL has the form `https://<your-domain>/worker-mcp`. The token is the value of `MCP_BEARER_TOKEN` on your bridge host. If you do not operate a bridge, stop here and install one first. See `README.md` and `docs/client-setup.md`.

This repo never hosts a shared OpenCode server for users. There is no generic default URL.

## Case 1: GitHub Copilot cloud agent and code review

Repository MCP settings give Copilot cloud agent and Copilot code review access to MCP tools. One configuration covers both. A repository administrator enters the JSON configuration in repository settings.

Copilot calls configured tools autonomously. It does not ask for approval first. Allowlist only the tools that you accept.

This path supports MCP tools only. It does not support MCP prompts. It does not support MCP resources. It does not support a remote MCP server that uses OAuth.

### Prerequisites

- You are a repository administrator.
- Your bridge is reachable from GitHub over HTTPS.
- You hold the Bearer token for your own bridge.

### Procedure: store the URL and the token

1. Open repository Settings, then Copilot, then MCP servers.
2. Open the Agents secrets or Agents variables page for the repository or the organization.
3. Add a secret or a variable with the name `COPILOT_MCP_BRIDGE_URL`. Set the value to your own bridge URL, for example `https://<your-domain>/worker-mcp`.
4. Add a secret with the name `COPILOT_MCP_BRIDGE_TOKEN`. Set the value to your own Bearer token.
5. Verify that both names start with `COPILOT_MCP_`. If a name lacks this prefix, Copilot cannot read it.

### Procedure: add the MCP configuration

1. Open repository Settings, then Copilot, then MCP servers.
2. Paste one JSON block from below into the MCP configuration field.
3. Select Save MCP configuration.
4. If code review must not call these tools, open Copilot code review settings and disable MCP tools for code review.

### Read-only option (safest start)

This option exposes three read-only worker tools. Copilot can list models, poll state, and verify evidence. It cannot start or delete a worker session through this bridge.

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "type": "http",
      "url": "${COPILOT_MCP_BRIDGE_URL}",
      "tools": ["worker_catalog", "worker_status", "worker_verify"],
      "headers": {
        "Authorization": "Bearer ${COPILOT_MCP_BRIDGE_TOKEN}"
      }
    }
  }
}
```

Use this option first. It is the correct default for code review use.

### Change-enabled option (full worker access)

This option exposes all five worker tools. Copilot can start (`worker_run`) and delete (`worker_cleanup`) worker sessions on the bridge host.

```json
{
  "mcpServers": {
    "opencode-bridge": {
      "type": "http",
      "url": "${COPILOT_MCP_BRIDGE_URL}",
      "tools": ["worker_catalog", "worker_run", "worker_status", "worker_verify", "worker_cleanup"],
      "headers": {
        "Authorization": "Bearer ${COPILOT_MCP_BRIDGE_TOKEN}"
      }
    }
  }
}
```

Tradeoff: the change-enabled option lets Copilot run async coding work through your OpenCode server. It also lets Copilot consume compute and change files without a per-call approval. Use the read-only option if that risk is not acceptable. Use `/worker-mcp` in the URL in both options, because that endpoint never exposes `exec_run`.

## Case 2: Microsoft Copilot Studio

In Copilot Studio you connect your agent to an existing Streamable HTTP MCP server. You enter your own bridge URL and your own Bearer token. Copilot Studio then reads the tool list from the bridge.

This bridge uses Bearer token authentication. It does not use OAuth in this path. It serves Streamable HTTP at `/mcp` (full catalog) and `/worker-mcp` (five worker tools only).

### Procedure: connect the bridge as an MCP tool

Use the MCP onboarding wizard (recommended path in the Microsoft guide).

1. Sign in to Copilot Studio and open your agent.
2. Go to the Tools page for your agent.
3. Select Add a tool.
4. Select New tool.
5. Select Model Context Protocol. The MCP onboarding wizard appears.
6. Fill in Server name, for example `opencode-bridge`.
7. Fill in Server description, for example `Run background coding tasks on my own OpenCode bridge`.
8. Fill in Server URL with your own bridge address, for example `https://<your-domain>/worker-mcp`.
9. Select API key as the authentication type.
10. Select Header as the Type of API key.
11. Enter `Authorization` as the header name.
12. Select Create. The Add tool dialog appears.
13. Select Create a new connection for your MCP server, then select Add to agent.
14. Verify that the tool list shows the five `worker_*` tools.

Notes:

- Use `/worker-mcp` for the five worker tools. Use `/mcp` only for legacy full-catalog access.
- Enter your own URL and your own token. There is no shared bridge to select.
- If the URL, the token, or the tool list changes later, edit the tool entry and refresh the connection.

## Case 3: Microsoft 365 Copilot

Microsoft 365 Copilot has no one-command personal plugin install for this bridge. The linked Microsoft docs describe two agent approaches: declarative agents and custom engine agents.

A common route is a Copilot Studio agent that is published to your organization catalog. Per the Microsoft agents admin guide, publication to the organization catalog involves admin review and approval in Copilot Control System. Availability to users then depends on tenant policies and the channels selected for the agent. This repo cannot grant those approvals.

If you need this route, build a Copilot Studio agent per Case 2 above. Then follow the Microsoft publish and admin approval path for your organization.

## Official documentation

- [GitHub: Configure MCP servers for your repository](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers).
- [Microsoft Copilot Studio: Connect your agent to an existing MCP server](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent).
- [Microsoft Copilot Studio: Add an MCP server to your agent as a tool](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/tools-add-mcp-server).
- [Microsoft 365 Copilot: Agents overview (declarative agents and custom engine agents)](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/agents-overview).
- [Microsoft 365 Copilot: Agents admin guide (approval and governance)](https://learn.microsoft.com/en-us/copilot/microsoft-365/agent-essentials/m365-agents-admin-guide).
