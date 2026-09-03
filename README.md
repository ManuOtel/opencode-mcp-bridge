# opencode-mcp-bridge

MCP bridge exposing a local `opencode serve`/`opencode web` instance to ChatGPT
via Streamable HTTP (`POST https://opencode-mcp.manuotel.com/mcp`).

Full-access build: ChatGPT can work in any directory and run terminal commands.
Protected by a static Bearer token on the MCP side and Basic auth to opencode.
