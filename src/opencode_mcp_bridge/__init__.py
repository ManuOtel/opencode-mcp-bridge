"""Opencode MCP bridge.

Exposes a local opencode serve/web instance as MCP tools over Streamable HTTP
so any MCP-compatible harness can list models, drive coding sessions,
and run terminal commands on the bridge host.

Modules:
    config: environment-based settings, no secrets in code.
    opencode_client: async HTTP client for the opencode REST API (Basic auth).
    server: FastMCP server, Bearer auth middleware, tool definitions.
"""
