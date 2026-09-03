#!/usr/bin/env bash
# Smoke test: health open, /mcp + /worker-mcp require Bearer, init + catalog work.
# Usage: MCP_BEARER_TOKEN=... BASE=https://mcp.example.com ./scripts/smoke.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8087}"
TOKEN="${MCP_BEARER_TOKEN:-}"

echo "== GET /health (expect 200, no token)"
curl -fsS "$BASE/health" | head -c 500; echo

for path in mcp worker-mcp; do
  echo "== POST /$path without token (expect 401)"
  code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/$path" -H 'Content-Type: application/json' -d '{}')
  echo "status=$code"
  [ "$code" = "401" ] || { echo "FAIL: expected 401 on /$path"; exit 1; }
done

if [ -z "$TOKEN" ]; then
  echo "SKIP: authenticated checks (set MCP_BEARER_TOKEN)"
  exit 0
fi

for path in mcp worker-mcp; do
  echo "== MCP initialize on /$path with token"
  curl -fsS -X POST "$BASE/$path" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}} ' | head -c 800; echo
done

check_catalog() {
  path="$1"; expected="$2"
  echo "== tools/list on /$path (expect $expected tools)"
  tmp=$(mktemp)
  curl -fsS -X POST "$BASE/$path" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' > "$tmp"
  count=$(python3 - "$tmp" <<'PY'
import json, sys
text = open(sys.argv[1]).read()
names = set()
for line in text.splitlines():
    if line.startswith("data: "):
        try:
            payload = json.loads(line[6:])
        except Exception:
            continue
        for tool in payload.get("result", {}).get("tools", []):
            if tool.get("name"):
                names.add(tool["name"])
try:
    payload = json.loads(text)
    for tool in payload.get("result", {}).get("tools", []):
        if isinstance(tool, dict) and tool.get("name"):
            names.add(tool["name"])
except Exception:
    pass
print(len(names))
PY
)
  echo "tools=$count expected=$expected"
  rm -f "$tmp"
  [ "$count" = "$expected" ] || { echo "FAIL: wrong tool count on /$path"; exit 1; }
}

check_catalog mcp 16
check_catalog worker-mcp 5
echo OK
