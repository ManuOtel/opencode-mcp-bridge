# Security Policy

## Supported code

Only the current `master` branch of `ManuOtel/opencode-mcp-bridge` receives
security fixes.
Tagged versions and old commits do not receive backports.
The project has no long-term support branch.
Fixes land as normal commits on `master` after review and verification.

Response timing is not guaranteed.
The project has no paid security team and no fixed SLA.

## Scope

In scope are faults in this repository in the following areas:

- Bearer-token authentication on `/mcp` and `/worker-mcp`.
- Token rotation with `MCP_BEARER_TOKEN_SECONDARY`.
- The `exec_run` opt-in gate (`ENABLE_EXEC_RUN`).
- Tool access boundaries between `/mcp` and `/worker-mcp`.
- Unsafe handling of paths, commands, or logs by the bridge code.

Not in scope are the following items:

- The OpenCode server itself and its own faults.
- MCP client apps such as Codex, Claude Code, or Copilot products.
- Operator infrastructure such as TLS, reverse proxy, firewall, DNS, or host
  hardening.
- The maintainer demo endpoint and third-party deployments.
- Social engineering, physical access, or compromised operator hosts.

## How to report

Report a security fault in private through GitHub Security Advisories:

<https://github.com/ManuOtel/opencode-mcp-bridge/security/advisories/new>

Do not open a public issue for a security fault.
Do not post proof-of-concept exploits in public spaces.

## What to include

Include the following facts in the private report:

- A short description of the fault and its impact.
- The affected endpoint (`/mcp` or `/worker-mcp`) and bridge version.
- Reproducible steps, in numbered order.
- Redacted logs or error text, with tokens removed.
- The client name and deployment mode, if relevant (self-hosted service or
  container).

## What not to disclose

- Do not include bearer tokens, passwords, or `Authorization` headers.
- Do not include private URLs, private paths, or personal data.
- Do not include full session text with sensitive data.
- Do not publish the report or the fix details before a fix is available.

## If a secret leaks

Treat a leaked `MCP_BEARER_TOKEN` as a full compromise of that token.
Rotate it at once with zero downtime in this order:

1. Generate a new token.
2. Set it as `MCP_BEARER_TOKEN_SECONDARY` and restart the bridge.
3. Move clients to the new token.
4. Promote it to `MCP_BEARER_TOKEN`, unset the secondary value, and restart.

Verify that `POST /mcp` and `POST /worker-mcp` without a token return 401.
Verify that the old token no longer grants access.
Never commit the new token to the repository.
