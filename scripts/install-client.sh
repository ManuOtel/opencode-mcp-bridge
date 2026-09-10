#!/usr/bin/env bash
# Register your own OpenCode bridge worker MCP on Codex and/or Claude Code.
# Usage: OPENCODE_MCP_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/install-client.sh [codex|claude|both] [--name <name>] [--dry-run]
set -euo pipefail

DEFAULT_NAME="opencode"
MODE="${1:-}"
NAME="$DEFAULT_NAME"
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage: OPENCODE_MCP_URL=... OPENCODE_MCP_BEARER_TOKEN=... ./scripts/install-client.sh [codex|claude|both] [--name <name>] [--dry-run]

Modes: codex, claude, both (required, first argument).
Options: --name <name> (optional MCP server name, default: opencode).
         --dry-run (validate inputs and print the planned registration without invoking codex/claude).
Env: OPENCODE_MCP_URL (required, your own bridge URL, e.g. https://<your-domain>/worker-mcp),
     OPENCODE_MCP_BEARER_TOKEN (required, never echoed).
URL must start with http:// or https:// and must end with /mcp or /worker-mcp.
Codex stores a bearer-token env-var reference. Claude stores a
Bearer ${OPENCODE_MCP_BEARER_TOKEN} reference (never the token value).
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
    --dry-run)
      DRY_RUN=1
      shift
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

case "$MCP_URL" in
  http://*|https://*)
    ;;
  *)
    echo "error: OPENCODE_MCP_URL must start with http:// or https://" >&2
    exit 1
    ;;
esac

case "$MCP_URL" in
  */worker-mcp|*/mcp)
    ;;
  *)
    echo "error: OPENCODE_MCP_URL must end with /worker-mcp or /mcp" >&2
    exit 1
    ;;
esac

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required command not found: $1" >&2
    return 1
  fi
}

if [ "$DRY_RUN" = "1" ]; then
  echo "dry-run: mode='$MODE' name='$NAME' url='$MCP_URL'"
  echo "dry-run: OPENCODE_MCP_BEARER_TOKEN will be referenced (value never printed)"
  echo "dry-run: no changes made (codex/claude not invoked)"
  exit 0
fi

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
  echo "warning: Claude Code stores a Bearer \${OPENCODE_MCP_BEARER_TOKEN} reference; keep the variable exported" >&2
  claude mcp add --transport http --header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}' "$NAME" "$MCP_URL"
fi

echo "done: registered '$NAME' at $MCP_URL for mode '$MODE'"
