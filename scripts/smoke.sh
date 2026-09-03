#!/usr/bin/env bash
# Smoke test: health open, /mcp requires Bearer, tool list works with token.
# Usage: MCP_BEARER_TOKEN=... BASE=https://opencode-mcp.manuotel.com ./scripts/smoke.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8087}"
TOKEN="${MCP_BEARER_TOKEN:-}"

echo "== GET /health (expect 200, no token)"
curl -fsS "$BASE/health" | head -c 500; echo

echo "== POST /mcp without token (expect 401)"
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/mcp" -H 'Content-Type: application/json' -d '{}')
echo "status=$code"
[ "$code" = "401" ] || { echo "FAIL: expected 401"; exit 1; }

if [ -z "$TOKEN" ]; then
  echo "SKIP: authenticated checks (set MCP_BEARER_TOKEN)"
  exit 0
fi

echo "== MCP initialize + tools/list with token"
curl -fsS -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}} ' | head -c 800; echo
echo OK
