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
HEALTH_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 --max-filesize 512000 "$HEALTH_URL") || fail "health request failed"
echo "health_status=$HEALTH_CODE"
[ "$HEALTH_CODE" = "200" ] || fail "expected 200 from health endpoint, got $HEALTH_CODE"

echo "== POST tools/list without token (expect 401)"
UNAUTH_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 --max-filesize 512000 -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}') || fail "unauthenticated request failed"
echo "unauth_status=$UNAUTH_CODE"
[ "$UNAUTH_CODE" = "401" ] || fail "expected 401 on unauthenticated POST, got $UNAUTH_CODE"

echo "== POST tools/list with token (expect 200 and five worker tools)"
RESP=$(mktemp)
NAMES=$(mktemp)
HEADF=$(mktemp)
CURL_EXIT=$(mktemp)
cleanup() {
    rm -f "$RESP" "$NAMES" "$HEADF" "$CURL_EXIT"
}
trap cleanup EXIT INT TERM

CAP=512000
LIMIT=512001
printf '0' > "$CURL_EXIT"
# Bounded fetch: the body streams through head so at most LIMIT bytes ever
# reach disk, even for chunked responses without Content-Length.
# --max-filesize is a second bound inside curl itself. curl exit is saved
# to a file because POSIX sh has no PIPESTATUS; 23 is head closing the
# pipe early (short write) and 63 is the --max-filesize abort.
# set +e is scoped to the subshell so a failing curl still reaches printf.
( set +e; curl -sS -D "$HEADF" -o - --max-time 20 --max-filesize "$CAP" -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'; printf '%s' "$?" > "$CURL_EXIT" ) | head -c "$LIMIT" > "$RESP"
CURL_STATUS=$(cat "$CURL_EXIT")
case "$CURL_STATUS" in
    0) ;;
    63) fail "tools/list response too large (over $CAP bytes, cap $CAP)" ;;
    23)
        SIZE_PROBE=$(wc -c < "$RESP" | tr -d ' ')
        if [ "${SIZE_PROBE:-0}" -ge "$LIMIT" ]; then
            fail "tools/list response too large (over $CAP bytes, cap $CAP)"
        fi
        fail "authenticated request short write (curl exit 23)"
        ;;
    *) fail "authenticated request failed (curl exit $CURL_STATUS)" ;;
esac

AUTH_CODE=$(sed -n '1s/^HTTP[^ ]* \([0-9][0-9]*\).*/\1/p' "$HEADF")
[ -n "${AUTH_CODE:-}" ] || fail "authenticated request failed (no HTTP status)"
echo "auth_status=$AUTH_CODE"
[ "$AUTH_CODE" = "200" ] || fail "expected 200 on authenticated tools/list, got $AUTH_CODE"

SIZE=$(wc -c < "$RESP" | tr -d ' ')
echo "response_bytes=$SIZE"
[ "$SIZE" -gt 0 ] || fail "empty tools/list response"
[ "$SIZE" -le "$CAP" ] || fail "tools/list response too large ($SIZE bytes, cap $CAP)"

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
