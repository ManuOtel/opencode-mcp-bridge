#!/bin/sh
# Transport smoke check for a deployed Streamable HTTP bridge.
# Usage: MCP_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/smoke.sh
# MCP_URL is the explicit endpoint (for example https://<your-domain>/worker-mcp).
# Takes no command-line arguments; the token is env-only and never printed.
# Needs: curl, jq, plus standard POSIX tools (mktemp, grep, sed, sort, wc).
# Writes only non-credential response bodies to temp files; prints counts,
# never full server responses, tokens, or Authorization headers.
set -eu

usage() {
    cat <<'USAGE'
Usage: MCP_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/smoke.sh

Env (both required, no defaults):
  MCP_URL                     explicit endpoint (for example https://<your-domain>/worker-mcp)
  OPENCODE_MCP_BEARER_TOKEN   bearer token (never passed as an argument)

Checks: GET /health is 200; unauthenticated POST to MCP_URL is 401;
authenticated tools/list succeeds with exactly the five worker_* tools
and without exec_run. Response parsing is size-bounded and summarized.
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ "$#" -gt 0 ]; then
    echo "error: this script takes no arguments; set MCP_URL and OPENCODE_MCP_BEARER_TOKEN in the environment" >&2
    usage >&2
    exit 2
fi

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: required command not found: $1" >&2
        return 1
    fi
}

need_cmd curl || exit 1
need_cmd jq || exit 1
need_cmd mktemp || exit 1
need_cmd grep || exit 1
need_cmd sed || exit 1
need_cmd sort || exit 1
need_cmd wc || exit 1
need_cmd head || exit 1
need_cmd tr || exit 1

if [ -z "${MCP_URL:-}" ]; then
    echo "error: MCP_URL is required (export your explicit endpoint, for example https://<your-domain>/worker-mcp)" >&2
    exit 1
fi

if [ -z "${OPENCODE_MCP_BEARER_TOKEN:-}" ]; then
    echo "error: OPENCODE_MCP_BEARER_TOKEN is required (export it; value is never echoed)" >&2
    exit 1
fi

MCP_NO_SLASH=${MCP_URL%/}
case "$MCP_NO_SLASH" in
    */*) MCP_ROOT=${MCP_NO_SLASH%/*} ;;
    *) MCP_ROOT=$MCP_NO_SLASH ;;
esac
HEALTH_URL="$MCP_ROOT/health"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

echo "== GET /health (expect 200, no token)"
HEALTH_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 "$HEALTH_URL") || fail "health request failed"
echo "health_status=$HEALTH_CODE"
[ "$HEALTH_CODE" = "200" ] || fail "expected 200 from health endpoint, got $HEALTH_CODE"

echo "== POST tools/list without token (expect 401)"
UNAUTH_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}') || fail "unauthenticated request failed"
echo "unauth_status=$UNAUTH_CODE"
[ "$UNAUTH_CODE" = "401" ] || fail "expected 401 on unauthenticated POST, got $UNAUTH_CODE"

echo "== POST tools/list with token (expect 200 and five worker tools)"
RESP=$(mktemp)
NAMES=$(mktemp)
cleanup() {
    rm -f "$RESP" "$NAMES"
}
trap cleanup EXIT INT TERM

AUTH_CODE=$(curl -sS -o "$RESP" -w "%{http_code}" --max-time 20 -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}') || fail "authenticated request failed"
echo "auth_status=$AUTH_CODE"
[ "$AUTH_CODE" = "200" ] || fail "expected 200 on authenticated tools/list, got $AUTH_CODE"

SIZE=$(wc -c < "$RESP" | tr -d ' ')
echo "response_bytes=$SIZE"
[ "$SIZE" -gt 0 ] || fail "empty tools/list response"
[ "$SIZE" -le 512000 ] || fail "tools/list response too large ($SIZE bytes, cap 512000)"

: > "$NAMES"
jq -r '.result.tools[]?.name? // empty' "$RESP" 2>/dev/null >> "$NAMES" || true
grep '^data: ' "$RESP" 2>/dev/null | head -n 100 | sed 's/^data: //' | while IFS= read -r line; do
    printf '%s\n' "$line" | jq -r '.result.tools[]?.name? // empty' 2>/dev/null || true
done >> "$NAMES" || true
sort -u "$NAMES" -o "$NAMES"
COUNT=$(grep -c . "$NAMES" || true)
COUNT=$(printf '%s' "$COUNT" | tr -d ' ')
echo "tool_count=$COUNT"
[ "$COUNT" = "5" ] || fail "expected exactly 5 worker tools, got $COUNT"

for want in worker_catalog worker_cleanup worker_run worker_status worker_verify; do
    grep -qx "$want" "$NAMES" || fail "missing expected tool: $want"
done
if grep -qx 'exec_run' "$NAMES"; then
    fail "exec_run must not be exposed on the worker endpoint"
fi

SUMMARY=$(tr '\n' ' ' < "$NAMES" | head -c 300)
echo "tools: $SUMMARY"
echo OK
