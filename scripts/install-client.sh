#!/usr/bin/env bash
# Register your own OpenCode bridge worker MCP on Codex and/or Claude Code.
# Usage: OPENCODE_MCP_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/install-client.sh [codex|claude|both] [--name <name>]
set -euo pipefail

DEFAULT_NAME="opencode"
MODE="${1:-}"
NAME="$DEFAULT_NAME"

usage() {
  cat <<'USAGE'
Usage: OPENCODE_MCP_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/install-client.sh [codex|claude|both] [--name <name>]

Modes: codex, claude, both (required, first argument).
Options: --name <name> (optional MCP server name, default: opencode).
Env: OPENCODE_MCP_URL (required, your own bridge URL, e.g. https://<your-domain>/worker-mcp),
     OPENCODE_MCP_BEARER_TOKEN (required, never echoed).
USAGE
}

if [ "$MODE" = "-h" ] || [ "$MODE" = "--help" ]; then
  usage
  exit 0
fi

if [ "$MODE" != "codex" ] && [ "$MODE" != "claude" ] && [ "$MODE" != "both" ]; then
  usage >&2
  echo "error: first argument must be one of: codex, claude, both" >&2
  exit 2
fi
shift

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name)
      if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        echo "error: --name requires a value" >&2
        exit 2
      fi
      NAME="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "${OPENCODE_MCP_BEARER_TOKEN:-}" ]; then
  echo "error: OPENCODE_MCP_BEARER_TOKEN is required (export it; value is never echoed)" >&2
  exit 1
fi

if [ -z "${OPENCODE_MCP_URL:-}" ]; then
  echo "error: OPENCODE_MCP_URL is required (export your own bridge URL, e.g. https://<your-domain>/worker-mcp)" >&2
  exit 1
fi
MCP_URL="$OPENCODE_MCP_URL"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required command not found: $1" >&2
    return 1
  fi
}

case "$MODE" in
  codex|both)
    need_cmd codex || exit 1
    ;;
esac
case "$MODE" in
  claude|both)
    need_cmd claude || exit 1
    ;;
esac

if [ "$MODE" = "codex" ] || [ "$MODE" = "both" ]; then
  codex mcp add "$NAME" --url "$MCP_URL" --bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN
fi

if [ "$MODE" = "claude" ] || [ "$MODE" = "both" ]; then
  echo "warning: Claude Code HTTP header configuration may persist the token locally" >&2
  claude mcp add --transport http "$NAME" "$MCP_URL" --header "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN"
fi

echo "done: registered '$NAME' at $MCP_URL for mode '$MODE'"
