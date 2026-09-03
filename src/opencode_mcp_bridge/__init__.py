"""Opencode MCP bridge.

Exposes a local opencode serve/web instance as MCP tools over Streamable HTTP
so ChatGPT (Developer Mode connector) can list models, drive coding sessions,
and run terminal commands on this server.

Modules:
    config: environment-based settings, no secrets in code.
    opencode_client: async HTTP client for the opencode REST API (Basic auth).
    server: FastMCP server, Bearer auth middleware, tool definitions.
"""
