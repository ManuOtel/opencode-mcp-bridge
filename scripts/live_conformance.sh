#!/bin/sh
# Live conformance release gate for one bridge endpoint.
# Usage: OPENCODE_MCP_LIVE_WORKER_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/live_conformance.sh
# Takes no command-line arguments; the token is env-only and never printed.
# Reports: endpoint, revision, worker tool_count, full tool_count, test result, test time.
# Needs: curl, jq, git, uv (or pytest fallback), plus POSIX tools.
# The worker endpoint stays separate from the full endpoint: the gate
# expects exactly the six worker_* tools with no exec_run on the worker
# URL, and a wider catalog containing exec_run on the full URL.
set -eu

usage() {
    cat <<'USAGE'
Usage: OPENCODE_MCP_LIVE_WORKER_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/live_conformance.sh

Env (no arguments; tokens are env-only and never echoed):
  OPENCODE_MCP_LIVE_WORKER_URL  explicit worker endpoint (required; MCP_URL accepted as alias)
                                local example: http://127.0.0.1:8087/worker-mcp
                                deployed example: https://<your-domain>/worker-mcp
  OPENCODE_MCP_BEARER_TOKEN     bearer token (required; MCP_BEARER_TOKEN accepted as alias)
  OPENCODE_MCP_LIVE_FULL_URL    explicit full endpoint (optional; default: sibling /mcp)
  OPENCODE_MCP_LIVE_DIRECTORY   server-side directory for the disposable run (optional)
  OPENCODE_MCP_LIVE_WAIT_S      worker_wait timeout for the live run (optional, default 10)

Gate: GET /health is 200; worker tools/list is exactly six worker tools
with no exec_run; full tools/list is wider with exec_run; then the
opt-in live pytest harness runs one disposable free-worker lifecycle.
Prints endpoint, revision, tool counts, test result, and test time.
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ "$#" -gt 0 ]; then
    echo "error: this script takes no arguments; set env vars per --help" >&2
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
need_cmd git || exit 1
need_cmd mktemp || exit 1

WORKER_URL="${OPENCODE_MCP_LIVE_WORKER_URL:-${MCP_URL:-}}"
TOKEN="${OPENCODE_MCP_BEARER_TOKEN:-${MCP_BEARER_TOKEN:-}}"
FULL_URL="${OPENCODE_MCP_LIVE_FULL_URL:-}"

if [ -z "$WORKER_URL" ]; then
    echo "error: OPENCODE_MCP_LIVE_WORKER_URL (or MCP_URL) is required" >&2
    exit 1
fi
if [ -z "$TOKEN" ]; then
    echo "error: OPENCODE_MCP_BEARER_TOKEN is required (export it; value is never echoed)" >&2
    exit 1
fi

if [ -z "$FULL_URL" ]; then
    case "$WORKER_URL" in
        */worker-mcp) FULL_URL="${WORKER_URL%/worker-mcp}/mcp" ;;
        */worker-mcp/) FULL_URL="${WORKER_URL%/worker-mcp/}/mcp" ;;
    esac
fi

REVISION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
GATE_START="$(date +%s)"

WORKER_NO_SLASH=${WORKER_URL%/}
case "$WORKER_NO_SLASH" in
    */*) WORKER_ROOT=${WORKER_NO_SLASH%/*} ;;
    *) WORKER_ROOT=$WORKER_NO_SLASH ;;
esac
HEALTH_URL="$WORKER_ROOT/health"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

echo "endpoint=$WORKER_URL"
echo "full_endpoint=${FULL_URL:-unknown}"
echo "revision=$REVISION"

echo "== GET /health (expect 200, no token)"
HEALTH_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 "$HEALTH_URL") || fail "health request failed"
echo "health_status=$HEALTH_CODE"
[ "$HEALTH_CODE" = "200" ] || fail "expected 200 from health endpoint, got $HEALTH_CODE"

list_tool_names() {
    _url="$1"
    _tmp="$2"
    curl -sS --max-time 20 -X POST "$_url" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -H "Authorization: Bearer $TOKEN" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' > "$_tmp" || return 1
    jq -r '.result.tools[]?.name? // empty' "$_tmp" 2>/dev/null || true
    grep '^data: ' "$_tmp" 2>/dev/null | sed 's/^data: //' | while IFS= read -r line; do
        printf '%s\n' "$line" | jq -r '.result.tools[]?.name? // empty' 2>/dev/null || true
    done || true
}

TMP_DIR=$(mktemp -d)
RESP="$TMP_DIR/response"
NAMES="$TMP_DIR/names"
FULL_RESP="$TMP_DIR/full-response"
FULL_NAMES="$TMP_DIR/full-names"
TEST_OUT="$TMP_DIR/pytest.out"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

echo "== POST tools/list on worker endpoint (expect six worker tools)"
: > "$NAMES"
list_tool_names "$WORKER_URL" "$RESP" | sort -u > "$NAMES" || fail "worker tools/list request failed"
TOOL_COUNT=$(grep -c . "$NAMES" || true)
TOOL_COUNT=$(printf '%s' "$TOOL_COUNT" | tr -d ' ')
echo "tool_count=$TOOL_COUNT"
[ "$TOOL_COUNT" = "6" ] || fail "expected exactly 6 worker tools, got $TOOL_COUNT"
for want in worker_catalog worker_cleanup worker_run worker_status worker_verify worker_wait; do
    grep -qx "$want" "$NAMES" || fail "missing expected worker tool: $want"
done
if grep -qx 'exec_run' "$NAMES"; then
    fail "exec_run must not be exposed on the worker endpoint"
fi

FULL_COUNT="unknown"
if [ -n "$FULL_URL" ]; then
    echo "== POST tools/list on full endpoint (expect wider catalog with exec_run)"
    : > "$FULL_NAMES"
    if list_tool_names "$FULL_URL" "$FULL_RESP" | sort -u > "$FULL_NAMES"; then
        FULL_COUNT=$(grep -c . "$FULL_NAMES" || true)
        FULL_COUNT=$(printf '%s' "$FULL_COUNT" | tr -d ' ')
        echo "full_tool_count=$FULL_COUNT"
        grep -qx 'exec_run' "$FULL_NAMES" || fail "full endpoint must list exec_run"
        for want in worker_catalog worker_cleanup worker_run worker_status worker_verify worker_wait; do
            grep -qx "$want" "$FULL_NAMES" || fail "full endpoint missing worker tool: $want"
        done
    else
        echo "full_tool_count=$FULL_COUNT"
        echo "warning: full endpoint tools/list failed; continuing with worker gate only" >&2
    fi
else
    echo "full_tool_count=$FULL_COUNT"
fi

echo "== live pytest harness (one disposable free-worker run)"
TEST_START="$(date +%s)"
TEST_RESULT="FAIL"
export OPENCODE_MCP_LIVE_WORKER_URL="$WORKER_URL"
export OPENCODE_MCP_BEARER_TOKEN="$TOKEN"
if [ -n "$FULL_URL" ]; then
    export OPENCODE_MCP_LIVE_FULL_URL="$FULL_URL"
fi
if command -v uv >/dev/null 2>&1; then
    TEST_CMD="uv run pytest tests/test_live_conformance.py -q"
else
    TEST_CMD="pytest tests/test_live_conformance.py -q"
fi
# shellcheck disable=SC2086
if $TEST_CMD > "$TEST_OUT" 2>&1; then
    TEST_RESULT="PASS"
else
    TEST_RESULT="FAIL"
fi
TEST_END="$(date +%s)"
TEST_TIME_S=$((TEST_END - TEST_START))
GATE_END="$(date +%s)"
GATE_TIME_S=$((GATE_END - GATE_START))

grep -v -i "bearer" "$TEST_OUT" | tail -n 20 || true
echo "endpoint=$WORKER_URL"
echo "revision=$REVISION"
echo "tool_count=$TOOL_COUNT"
echo "full_tool_count=$FULL_COUNT"
echo "test_result=$TEST_RESULT"
echo "test_time_s=$TEST_TIME_S"
echo "gate_time_s=$GATE_TIME_S"
[ "$TEST_RESULT" = "PASS" ] || fail "live conformance harness failed"
echo OK
