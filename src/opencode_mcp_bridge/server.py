"""FastMCP server bridging MCP clients to local opencode.

Transport: Streamable HTTP at POST /mcp (full 19-tool catalog, stateless)
and POST /worker-mcp (eight worker_* tools only, stateless). Works with
ChatGPT, Claude Code, Codex, and other MCP-compatible harnesses.
Auth: static Bearer token on every /mcp and /worker-mcp request, with an
optional secondary rotation token for overlap (see README rotation steps);
Basic auth to opencode. Health: GET /health is open minimal liveness
(process alive, no OpenCode, registry, log, or metrics dependency).
Readiness: GET /ready needs the Bearer token and checks OpenCode plus
the task registry read-only (never creates state). Metrics: GET
/metrics needs the Bearer token and returns bounded counters for the
full tool catalog plus infra subsystems.
Discovery: GET /.well-known/oauth-protected-resource (+ /mcp and
/worker-mcp children) is open RFC 9728 metadata with no secrets and no
authorization server; 401s on /mcp and /worker-mcp point at it via
WWW-Authenticate. GET /.well-known/mcp/server-card.json is open static
metadata for scanners blocked by the auth wall (Smithery fallback).

Run:
    python -m opencode_mcp_bridge.server
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from opencode_mcp_bridge import observability
from opencode_mcp_bridge.config import (
    Settings,
    _realpath_str,
    accepted_bearer_tokens,
    load_settings,
)
from opencode_mcp_bridge.config import _is_within_root as _within_root
from opencode_mcp_bridge.opencode_client import OpencodeClient, OpencodeError

WORKER_INSTRUCTIONS = (
    "Worker-first bridge to self-hosted opencode. "
    "Use worker_catalog to pick a model, worker_run to start background work, "
    "worker_wait to wait bounded server-side, worker_status for an immediate "
    "snapshot, worker_verify to check git state, "
    "worker_decide/worker_resume for approval-gated risky work, "
    "worker_cleanup to abort/delete. Legacy session/message/diff/exec tools "
    "are advanced compatibility only."
)

mcp = FastMCP(
    "opencode-bridge",
    instructions=WORKER_INSTRUCTIONS,
)

worker_mcp = FastMCP(
    "opencode-bridge-worker",
    instructions=WORKER_INSTRUCTIONS,
)

# Shared worker tools use stacked @mcp.tool + @worker_mcp.tool. Each
# decorator snapshots ToolMeta via Tool.from_function at decorate time, so
# each server keeps its own copy even though fn.__fastmcp__ ends as the
# outer decorator's meta.

_settings: Settings | None = None
_client: OpencodeClient | None = None


def get_settings() -> Settings:
    """Load and cache settings.

    Returns:
        Cached Settings.

    Raises:
        RuntimeError: If required env vars are missing.
    """
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def get_client() -> OpencodeClient:
    """Create and cache the opencode client.

    Returns:
        Shared OpencodeClient instance.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = OpencodeClient(
            base_url=settings.opencode_base_url,
            username=settings.opencode_username,
            password=settings.opencode_password,
            default_directory=settings.default_directory,
            default_provider_id=settings.default_provider_id,
            default_model_id=settings.default_model_id,
        )
    return _client


@mcp.custom_route("/health", methods=["GET"])
@worker_mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    """Open minimal liveness probe with no dependencies.

    Unauthenticated by design; always 200 {"ok": true} when the process
    serves HTTP. Never touches OpenCode, the task registry, settings,
    logs, or metrics counters, and never exposes versions, URLs, paths,
    or exception text. Public probe traffic cannot corrupt operator
    metrics because this endpoint records nothing.

    Args:
        request: Starlette request (unused).

    Returns:
        Minimal JSON liveness response.
    """
    return JSONResponse({"ok": True})


def _registry_writable() -> bool:
    """Check the task registry loads and its directory is writable.

    Strictly read-only: never creates directories or files. Missing
    files count as available (empty registry) only when the parent
    directory already exists and is writable. A missing parent fails
    closed (and stays missing). Any corrupt, unreadable, or unwritable
    state returns False. Never raises and never returns paths or
    exception text.

    Returns:
        True when the registry is usable, else False.
    """
    try:
        _load_task_state()
    except Exception:  # noqa: BLE001 - readiness reports 503, never detail
        return False
    try:
        parent = _task_state_path().parent
    except Exception:  # noqa: BLE001 - readiness reports 503, never detail
        return False
    try:
        if not parent.is_dir():
            return False
    except OSError:
        return False
    return os.access(str(parent), os.W_OK)


@mcp.custom_route("/ready", methods=["GET"])
@worker_mcp.custom_route("/ready", methods=["GET"])
async def ready_check(request: Request) -> Response:
    """Authenticated readiness probe for OpenCode plus the task registry.

    Requires the Bearer token via middleware (same as /mcp). Returns
    200 {"ok": true} only when OpenCode answers and the registry loads
    with a writable directory; else 503 {"ok": false,
    "error": "unavailable"}. Never exposes paths, secrets, prompts,
    raw IDs, or exception text.

    Args:
        request: Starlette request (unused).

    Returns:
        Minimal JSON readiness response.
    """
    start = time.perf_counter()
    observability.emit(
        event=observability.EVENT_READINESS,
        tool=observability.TOOL_READINESS,
        outcome=observability.OUTCOME_STARTED,
    )
    try:
        await get_client().health()
    except Exception:  # noqa: BLE001 - readiness reports 503, never detail
        observability.emit(
            event=observability.EVENT_READINESS,
            tool=observability.TOOL_READINESS,
            outcome=observability.OUTCOME_FAILED,
            duration_ms=observability.duration_ms_since(start),
        )
        return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)
    if not _registry_writable():
        observability.emit(
            event=observability.EVENT_READINESS,
            tool=observability.TOOL_READINESS,
            outcome=observability.OUTCOME_FAILED,
            duration_ms=observability.duration_ms_since(start),
        )
        return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)
    observability.emit(
        event=observability.EVENT_READINESS,
        tool=observability.TOOL_READINESS,
        outcome=observability.OUTCOME_SUCCEEDED,
        duration_ms=observability.duration_ms_since(start),
    )
    return JSONResponse({"ok": True})


@mcp.custom_route("/metrics", methods=["GET"])
@worker_mcp.custom_route("/metrics", methods=["GET"])
async def metrics_endpoint(request: Request) -> Response:
    """Authenticated bounded internal counters as JSON.

    Requires the Bearer token via middleware (same as /mcp). Keys are
    fixed event|tool|outcome triples only; values are plain ints. Never
    contains raw identifiers, paths, prompts, tokens, or exception
    details. No external telemetry or network calls.

    Args:
        request: Starlette request (unused).

    Returns:
        JSON payload with ok plus the bounded metrics map.
    """
    observability.emit(
        event=observability.EVENT_METRICS,
        tool=observability.TOOL_METRICS,
        outcome=observability.OUTCOME_SUCCEEDED,
    )
    return JSONResponse({"ok": True, "metrics": observability.snapshot()})


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_providers() -> dict[str, Any]:
    """List opencode providers and models. Call this first for the model picker.

    Returns:
        Dict with providers [{providerID, name, modelIDs, connected}] and default map.
    """
    return await get_client().list_providers()


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_agents(directory: str | None = None) -> list[dict[str, Any]]:
    """List available opencode agents (e.g. plan, build).

    Args:
        directory: Working directory. Defaults to the server default.

    Returns:
        Agent list with name/mode/description.
    """
    effective = _authorize_optional_directory(directory)
    return await get_client().list_agents(effective)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def create_session(
    title: str | None = None,
    directory: str | None = None,
) -> dict[str, Any]:
    """Create a new opencode session.

    Args:
        title: Session title.
        directory: Working directory (must be within allowed directories).

    Returns:
        Created session returned by opencode. Select agent, provider, and model on send_message.
    """
    effective = _authorize_optional_directory(directory)
    session = await get_client().create_session(title, effective)
    return OpencodeClient._simplify_session(session)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def send_message(
    sessionID: str,
    message: str | None = None,
    providerID: str | None = None,
    modelID: str | None = None,
    agent: str | None = None,
    directory: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Send a prompt to a session and wait for the assistant reply.

    Args:
        sessionID: Session ID from create_session.
        message: The message text for the agent.
        prompt: Backward-compatible alias for message. Supply exactly one.
        providerID: Optional model override provider.
        modelID: Optional model override model.
        agent: Optional agent override.
        directory: Working directory override.

    Returns:
        Dict with sessionID, messageID, text, and model info.
    """
    if (message is None) == (prompt is None):
        raise ValueError("Exactly one of message or prompt must be supplied")
    effective = _authorize_optional_directory(directory)
    return await get_client().send_message(
        sessionID, message if message is not None else prompt, providerID, modelID, agent, effective
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_sessions(directory: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """List recent opencode sessions.

    Args:
        directory: Filter directory.
        limit: Max sessions (1-100).

    Returns:
        Simplified session dicts.
    """
    effective = _authorize_optional_directory(directory)
    return await get_client().list_sessions(effective, max(1, min(limit, 100)))


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_session(sessionID: str, directory: str | None = None) -> dict[str, Any]:
    """Get one session by ID.

    Args:
        sessionID: Session ID.
        directory: Working directory override.

    Returns:
        Simplified session dict.
    """
    effective = _authorize_optional_directory(directory)
    return await get_client().get_session(sessionID, effective)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_messages(
    sessionID: str, directory: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List messages in a session (user prompts and assistant replies).

    Args:
        sessionID: Session ID.
        directory: Working directory override.
        limit: Max messages (1-200).

    Returns:
        List of {id, role, text, time} dicts.
    """
    effective = _authorize_optional_directory(directory)
    return await get_client().list_messages(sessionID, effective, max(1, min(limit, 200)))


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def abort_session(sessionID: str, directory: str | None = None) -> dict[str, Any]:
    """Abort a running session.

    Args:
        sessionID: Session ID.
        directory: Working directory override.

    Returns:
        Dict with sessionID and aborted=True.
    """
    effective = _authorize_optional_directory(directory)
    await get_client().abort_session(sessionID, effective)
    return {"sessionID": sessionID, "aborted": True}


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def delete_session(sessionID: str, directory: str | None = None) -> dict[str, Any]:
    """Delete a session and all its data. Use to clean up test sessions.

    Args:
        sessionID: Session ID.
        directory: Working directory override.

    Returns:
        Dict with sessionID and deleted=True.
    """
    effective = _authorize_optional_directory(directory)
    await get_client().delete_session(sessionID, effective)
    return {"sessionID": sessionID, "deleted": True}


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_diff(
    sessionID: str, messageID: str | None = None, directory: str | None = None
) -> list[dict[str, Any]]:
    """Get file diffs produced by a session.

    Args:
        sessionID: Session ID.
        messageID: Optional message to scope the diff.
        directory: Working directory override.

    Returns:
        File diff list from opencode.
    """
    effective = _authorize_optional_directory(directory)
    return await get_client().get_diff(sessionID, messageID, effective)


WORKER_OUTPUT_DEFAULT_CHARS = 12000
WORKER_OUTPUT_MAX_CHARS = 50000
WORKER_CATALOG_DEFAULT_LIMIT = 20
WORKER_CATALOG_MAX_LIMIT = 100
FALLBACK_PAID_PROVIDER_ID = "opencode-go"
FALLBACK_PAID_MODEL_ID = "muse-spark-1.3-contributor"
FALLBACK_PAID_NAME = "Muse Spark 1.3 Contributor"
WORKER_VERIFY_DEFAULT_CHARS = 12000
WORKER_VERIFY_GIT_MAX_CHARS = 8000
WORKER_VERIFY_GIT_MAX_FILES = 50
WORKER_VERIFY_GIT_TIMEOUT_S = 15
WORKER_VERIFY_COMMIT_MAX_CHARS = 300
WORKER_CLEANUP_WARNING_MAX_CHARS = 300
TASK_MAX_RECORDS = 500
TASK_REQUEST_ID_MAX_CHARS = 128
TASK_DIRECTORY_MAX_CHARS = 500
TASK_TITLE_MAX_CHARS = 200
TASK_AGENT_MAX_CHARS = 100
TASK_LOCK_TIMEOUT_S = 10.0
TASK_LOCK_POLL_S = 0.02
TASK_STALE_AFTER_DEFAULT_S = 600
TASK_STALE_REASON_MAX_CHARS = 200
WORKER_STALE_RECOVERY_HINT = (
    "Stale worker: running with empty output past the startup timeout. "
    "Run worker_cleanup action=delete for this taskID only."
)
WORKER_WAIT_DEFAULT_TIMEOUT_S = 30.0
WORKER_WAIT_MIN_TIMEOUT_S = 1.0
WORKER_WAIT_MAX_TIMEOUT_S = 120.0
WORKER_WAIT_POLL_S = 0.5
WORKER_APPROVAL_DEFAULT_TTL_S = 3600
WORKER_APPROVAL_MIN_TTL_S = 60
WORKER_APPROVAL_MAX_TTL_S = 86400
WORKER_APPROVAL_TOKEN_HEX_BYTES = 16
WORKER_RISKY_ACTION_MAX_CHARS = 100
WORKER_APPROVAL_ID_PREFIX = "apr_"
APPROVAL_STATE_REQUIRED = "approval_required"
APPROVAL_STATE_APPROVED = "approved"
APPROVAL_STATE_REJECTED = "rejected"
APPROVAL_STATE_EXPIRED = "expired"
APPROVAL_STATE_RESUMED = "resumed"
APPROVAL_TERMINAL_STATES = frozenset(
    {APPROVAL_STATE_REJECTED, APPROVAL_STATE_EXPIRED, APPROVAL_STATE_RESUMED}
)
WORKER_APPROVAL_EXPIRED_HINT = (
    "Approval expired before resume. Re-run worker_run for this task only."
)
WORKER_APPROVAL_RESUME_HINT = (
    "Approved. Resume this taskID only with worker_resume and the same inputs."
)

# Stable bounded contracts exposed via FastMCP output_schema (supported in
# installed FastMCP 4.x: @mcp.tool(output_schema={...}) must be an object
# schema). Dict returns stay backward compatible: existing keys are never
# removed or renamed, contract keys are additive only, and text rendering
# is unchanged.
WORKER_RUN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taskID": {"type": "string"},
        "sessionID": {"type": ["string", "null"]},
        "state": {"type": "string"},
        "providerID": {"type": "string"},
        "modelID": {"type": "string"},
        "directory": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "agent": {"type": ["string", "null"]},
        "requestID": {"type": ["string", "null"]},
        "deduplicated": {"type": "boolean"},
        "timed_out": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
        "approval_state": {"type": ["string", "null"]},
        "approval_token": {"type": ["string", "null"]},
        "risky_action": {"type": ["string", "null"]},
        "expires_at": {"type": ["number", "null"]},
    },
    "additionalProperties": True,
}
WORKER_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taskID": {"type": "string"},
        "sessionID": {"type": ["string", "null"]},
        "state": {"type": "string"},
        "status": {"type": ["string", "null"]},
        "messageID": {"type": ["string", "null"]},
        "output": {"type": ["string", "null"]},
        "output_chars": {"type": "integer"},
        "total_chars": {"type": "integer"},
        "truncated_chars": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "directory": {"type": "string"},
        "stale": {"type": "boolean"},
        "stale_reason": {"type": ["string", "null"]},
        "recovery_hint": {"type": ["string", "null"]},
        "timed_out": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
        "approval_state": {"type": ["string", "null"]},
        "risky_action": {"type": ["string", "null"]},
        "expires_at": {"type": ["number", "null"]},
    },
    "additionalProperties": True,
}
WORKER_WAIT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taskID": {"type": "string"},
        "sessionID": {"type": ["string", "null"]},
        "state": {"type": "string"},
        "status": {"type": ["string", "null"]},
        "messageID": {"type": ["string", "null"]},
        "output": {"type": ["string", "null"]},
        "output_chars": {"type": "integer"},
        "total_chars": {"type": "integer"},
        "truncated_chars": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "directory": {"type": "string"},
        "stale": {"type": "boolean"},
        "stale_reason": {"type": ["string", "null"]},
        "recovery_hint": {"type": ["string", "null"]},
        "timed_out": {"type": "boolean"},
        "changed": {"type": "boolean"},
        "elapsed_s": {"type": "number"},
        "timeout_s": {"type": "number"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
        "approval_state": {"type": ["string", "null"]},
        "risky_action": {"type": ["string", "null"]},
        "expires_at": {"type": ["number", "null"]},
    },
    "additionalProperties": True,
}
WORKER_VERIFY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taskID": {"type": "string"},
        "sessionID": {"type": "string"},
        "state": {"type": "string"},
        "status": {"type": ["string", "null"]},
        "messageID": {"type": ["string", "null"]},
        "output": {"type": ["string", "null"]},
        "output_chars": {"type": "integer"},
        "total_chars": {"type": "integer"},
        "truncated_chars": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "directory": {"type": "string"},
        "stale": {"type": "boolean"},
        "stale_reason": {"type": ["string", "null"]},
        "recovery_hint": {"type": ["string", "null"]},
        "verification": {"type": "object"},
        "timed_out": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
        "approval_state": {"type": ["string", "null"]},
        "risky_action": {"type": ["string", "null"]},
        "expires_at": {"type": ["number", "null"]},
    },
    "additionalProperties": True,
}
WORKER_CLEANUP_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taskID": {"type": "string"},
        "sessionID": {"type": ["string", "null"]},
        "action": {"type": "string"},
        "aborted": {"type": "boolean"},
        "deleted": {"type": "boolean"},
        "directory": {"type": "string"},
        "cleanup_warning": {"type": ["string", "null"]},
        "state": {"type": "string"},
        "timed_out": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
    },
    "additionalProperties": True,
}
WORKER_DECIDE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taskID": {"type": "string"},
        "sessionID": {"type": ["string", "null"]},
        "state": {"type": "string"},
        "approval_state": {"type": "string"},
        "decision": {"type": ["string", "null"]},
        "risky_action": {"type": ["string", "null"]},
        "directory": {"type": "string"},
        "expires_at": {"type": ["number", "null"]},
        "decided_at": {"type": ["number", "null"]},
        "deduplicated": {"type": "boolean"},
        "timed_out": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
    },
    "additionalProperties": True,
}
WORKER_RESUME_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taskID": {"type": "string"},
        "sessionID": {"type": ["string", "null"]},
        "state": {"type": "string"},
        "approval_state": {"type": "string"},
        "risky_action": {"type": ["string", "null"]},
        "directory": {"type": "string"},
        "providerID": {"type": "string"},
        "modelID": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "agent": {"type": ["string", "null"]},
        "requestID": {"type": ["string", "null"]},
        "deduplicated": {"type": "boolean"},
        "timed_out": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
    },
    "additionalProperties": True,
}
WORKER_CATALOG_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "models": {"type": "array"},
        "default": {"type": "object"},
        "total": {"type": "integer"},
        "recommendations": {"type": "array"},
        "timed_out": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "evidence": {"type": "object"},
    },
    "additionalProperties": True,
}


def _clamp_worker_wait_timeout(value: Any) -> float:
    """Clamp a worker_wait timeout to a finite bounded range.

    Args:
        value: Requested timeout in seconds (None means the default).

    Returns:
        Bounded timeout in seconds within [MIN, MAX].

    Raises:
        ValueError: If the value is not a finite number.
    """
    import math

    if value is None:
        return WORKER_WAIT_DEFAULT_TIMEOUT_S
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError("timeout_s must be a number of seconds")
    if not math.isfinite(numeric):
        raise ValueError("timeout_s must be a finite number of seconds")
    return max(WORKER_WAIT_MIN_TIMEOUT_S, min(numeric, WORKER_WAIT_MAX_TIMEOUT_S))


def _next_action_for_state(state: str, timed_out: bool) -> str:
    """Return coordinator guidance for a worker state.

    Args:
        state: Mapped worker state.
        timed_out: True when a bounded wait expired without a change.

    Returns:
        Stable next_action string (worker_wait/worker_verify/
        worker_status/worker_cleanup).
    """
    if state == "stale":
        return "worker_cleanup"
    if state == "idle":
        return "worker_verify"
    if state == "error":
        return "worker_verify"
    if state == "unknown":
        return "worker_status"
    if state == APPROVAL_STATE_REQUIRED:
        return "worker_decide"
    if state == APPROVAL_STATE_APPROVED:
        return "worker_resume"
    if state in (APPROVAL_STATE_REJECTED, APPROVAL_STATE_EXPIRED):
        return "worker_run"
    if state == APPROVAL_STATE_RESUMED:
        return "worker_wait"
    if timed_out:
        return "worker_wait"
    return "worker_wait" if state == "running" else "worker_status"


def _retryable_for_state(state: str, timed_out: bool, stale: bool = False) -> bool:
    """Return whether the coordinator can usefully retry/wait again.

    Args:
        state: Mapped worker state.
        timed_out: True when a bounded wait expired without a change.
        stale: True when the snapshot was classified stale.

    Returns:
        True for running (including timed-out waits) and error; False
        for idle, stale, and unknown terminal snapshots.
    """
    if stale or state in ("idle", "unknown", "stale"):
        return False
    if state in ("running", "error"):
        return True
    if state in (APPROVAL_STATE_REQUIRED, APPROVAL_STATE_APPROVED, APPROVAL_STATE_RESUMED):
        return True
    if state in (APPROVAL_STATE_REJECTED, APPROVAL_STATE_EXPIRED):
        return False
    return bool(timed_out)


def _error_code_for_snapshot(state: str, status: Any, message_id: Any) -> str | None:
    """Return a stable error code for snapshots that need one.

    Only genuinely missing tasks get a code today: unknown state with no
    raw status and no assistant message means the session is absent from
    OpenCode. All other states return None (no error).

    Args:
        state: Mapped worker state.
        status: Raw status value (may be None).
        message_id: Assistant message ID or None.

    Returns:
        "task_not_found" for absent tasks, else None.
    """
    if state == "unknown" and status is None and message_id is None:
        return "task_not_found"
    if state == APPROVAL_STATE_REJECTED:
        return "approval_rejected"
    if state == APPROVAL_STATE_EXPIRED:
        return "approval_expired"
    return None


def _worker_evidence(
    status: Any, message_id: Any, output_chars: int, total_chars: int
) -> dict[str, Any]:
    """Build concise bounded evidence for a worker snapshot.

    Args:
        status: Raw status value (bounded to a short string).
        message_id: Assistant message ID or None.
        output_chars: Bounded output length.
        total_chars: Full output length.

    Returns:
        Small dict with no prompt text, paths, or secrets.
    """
    raw = status if isinstance(status, str) else (str(status) if status is not None else None)
    return {
        "status": _bound_text(raw, 64) if raw is not None else None,
        "messageID": message_id,
        "output_chars": int(output_chars or 0),
        "total_chars": int(total_chars or 0),
    }


_TASK_LOCK: asyncio.Lock | None = None
_TASK_LOCK_LOOP: Any = None


def _get_task_lock() -> asyncio.Lock:
    """Return the process-wide lock serializing task registry mutations.

    The lock is re-created if the running event loop changed, so sequential
    asyncio.run calls in tests each get a usable lock while concurrent
    coroutines on one loop share it.

    Returns:
        Shared asyncio lock for task registry sequences.
    """
    global _TASK_LOCK, _TASK_LOCK_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _TASK_LOCK is None or (loop is not None and _TASK_LOCK_LOOP is not loop):
        _TASK_LOCK = asyncio.Lock()
        _TASK_LOCK_LOOP = loop
    return _TASK_LOCK


def _task_lock_path() -> Path:
    """Return the sibling advisory lock file for the task registry."""
    state = _task_state_path()
    return state.parent / (state.name + ".lock")


def _require_fcntl() -> Any:
    """Return the fcntl module or fail closed on unsupported platforms.

    Returns:
        The fcntl module.

    Raises:
        RuntimeError: If advisory locking is unavailable. The message
            carries the registry path only, never prompts or secrets.
    """
    try:
        import fcntl as fcntl_mod
    except ImportError:
        raise RuntimeError(
            f"task registry lock unavailable on this platform for {_task_state_path()}"
        )
    if not all(hasattr(fcntl_mod, name) for name in ("LOCK_EX", "LOCK_NB", "LOCK_UN")):
        raise RuntimeError(
            f"task registry lock unavailable on this platform for {_task_state_path()}"
        )
    return fcntl_mod


@contextmanager
def _held_task_file_lock(timeout_s: float | None = None):  # type: ignore[no-untyped-def]
    """Hold an advisory exclusive file lock (sync, bounded, fail-closed).

    Dependency-free cross-process mutual exclusion for TASK_STATE_PATH
    readers/writers that share one filesystem. Uses fcntl.flock on a
    sibling ``<tasks>.lock`` file with non-blocking retries so waits are
    bounded by ``timeout_s``. The lock file is kept (never unlinked) to
    avoid unlink races; locking is advisory so all registry writers must
    cooperate through this helper.

    Args:
        timeout_s: Max seconds to wait before failing closed.

    Raises:
        RuntimeError: If fcntl is unavailable, the lock file cannot be
            created, or the timeout expires. Messages carry the path
            only, never prompts or secrets.
    """
    if timeout_s is None:
        timeout_s = TASK_LOCK_TIMEOUT_S
    fcntl_mod = _require_fcntl()
    lock_path = _task_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"task registry lock at {lock_path} is unwritable: {exc.strerror or 'I/O error'}"
        )
    try:
        handle = open(lock_path, "a+b")  # noqa: SIM115 - held open for flock
    except OSError as exc:
        raise RuntimeError(
            f"task registry lock at {lock_path} is unwritable: {exc.strerror or 'I/O error'}"
        )
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl_mod.flock(handle.fileno(), fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
                break
            except (BlockingIOError, PermissionError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"task registry at {_task_state_path()} is busy: lock timeout; retry later"
                    )
                time.sleep(TASK_LOCK_POLL_S)
        yield handle
    finally:
        with suppress(Exception):
            fcntl_mod.flock(handle.fileno(), fcntl_mod.LOCK_UN)
        with suppress(Exception):
            handle.close()


@asynccontextmanager
async def _locked_task_registry(timeout_s: float | None = None):  # type: ignore[no-untyped-def]
    """Hold the asyncio lock plus the cross-process file lock.

    Ordering is always asyncio-first then file lock; release is reverse.
    The file lock is held across the full load/check/create/save/prompt
    sequence (including slow OpenCode network calls) because that is what
    the existing correctness model requires: the requestID dedup check,
    liveness probe, session creation, atomic save, and failure cleanup
    must be one atomic unit across processes, otherwise two bridges can
    create duplicate requestID sessions or clobber each other's records
    via load-modify-save races. Waiters are bounded by ``timeout_s`` and
    fail closed (RuntimeError, path only) instead of duplicating work.

    Args:
        timeout_s: Max seconds to wait for the file lock.

    Raises:
        RuntimeError: On unsupported platforms, unwritable lock files,
            or lock timeout. No prompts or secrets in the message.
    """
    if timeout_s is None:
        timeout_s = TASK_LOCK_TIMEOUT_S
    lock = _get_task_lock()
    async with lock:
        fcntl_mod = _require_fcntl()
        lock_path = _task_lock_path()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"task registry lock at {lock_path} is unwritable: {exc.strerror or 'I/O error'}"
            )
        try:
            handle = open(  # noqa: SIM115, ASYNC230 - held open for flock; fast local open
                lock_path, "a+b"
            )
        except OSError as exc:
            raise RuntimeError(
                f"task registry lock at {lock_path} is unwritable: {exc.strerror or 'I/O error'}"
            )
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                try:
                    fcntl_mod.flock(handle.fileno(), fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
                    break
                except (BlockingIOError, PermissionError, OSError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"task registry at {_task_state_path()} is busy: lock timeout; retry later"
                        )
                    await asyncio.sleep(TASK_LOCK_POLL_S)
            try:
                yield
            finally:
                with suppress(Exception):
                    fcntl_mod.flock(handle.fileno(), fcntl_mod.LOCK_UN)
        finally:
            with suppress(Exception):
                handle.close()


def _canonical_task_dir(directory: str | None) -> str:
    """Normalize a directory for fingerprinting.

    None and empty mean the server default and hash identically. Trailing
    separators are stripped so equivalent spellings do not conflict.

    Args:
        directory: Requested directory or None.

    Returns:
        Canonical directory string.
    """
    if directory is None:
        return ""
    cleaned = directory.strip()
    if not cleaned:
        return ""
    if len(cleaned) > 1:
        cleaned = cleaned.rstrip("/")
    return cleaned or ""


def _validate_task_directory(directory: str, *, what: str = "directory") -> None:
    """Reject an over-long directory before any side effect.

    Args:
        directory: Requested or effective directory path.
        what: Field name used in the error message.

    Raises:
        ValueError: If the path exceeds TASK_DIRECTORY_MAX_CHARS.
    """
    if len(directory) > TASK_DIRECTORY_MAX_CHARS:
        raise ValueError(f"{what} must be at most {TASK_DIRECTORY_MAX_CHARS} chars")


def _authorize_directory(directory: str, *, what: str = "directory") -> str:
    """Authorize a directory against ALLOWED_DIRECTORIES canonically.

    Length is checked first, then the path is resolved with realpath
    semantics (symlinks, dot segments, traversal) before comparison.
    A path equal to a root or below a root is allowed; sibling-prefix
    matches are rejected because comparison uses path relativity,
    not string prefixes.

    Args:
        directory: Requested directory path.
        what: Field name used in the error message.

    Returns:
        Canonical authorized path string.

    Raises:
        ValueError: If the path is outside all allowed roots.
    """
    _validate_task_directory(directory, what=what)
    cleaned = directory.strip()
    if not cleaned:
        return _authorize_optional_directory(None, what=what)
    candidate_real = _realpath_str(cleaned)
    settings = get_settings()
    for root in settings.allowed_directories:
        if _within_root(candidate_real, _realpath_str(root)):
            return candidate_real
    raise ValueError(f"{what} is not within allowed directories")


def _authorize_optional_directory(directory: str | None, *, what: str = "directory") -> str:
    """Authorize an optional directory, falling back to the default.

    Args:
        directory: Requested directory or None (server default).
        what: Field name used in the error message.

    Returns:
        Canonical authorized path string.

    Raises:
        ValueError: If the effective path is outside allowed roots.
    """
    if directory is None or not directory.strip():
        settings = get_settings()
        _validate_task_directory(settings.default_directory, what=what)
        candidate_real = _realpath_str(settings.default_directory)
        for root in settings.allowed_directories:
            if _within_root(candidate_real, _realpath_str(root)):
                return candidate_real
        raise ValueError(f"{what} is not within allowed directories")
    return _authorize_directory(directory, what=what)


WORKER_TOOL_NAMES = frozenset(
    {
        "worker_run",
        "worker_status",
        "worker_wait",
        "worker_verify",
        "worker_cleanup",
        "worker_catalog",
        "worker_decide",
        "worker_resume",
    }
)
ALL_TOOL_NAMES = frozenset(
    {
        "list_providers",
        "list_agents",
        "create_session",
        "send_message",
        "list_sessions",
        "get_session",
        "list_messages",
        "abort_session",
        "delete_session",
        "get_diff",
        "worker_run",
        "worker_status",
        "worker_wait",
        "worker_catalog",
        "exec_run",
        "worker_verify",
        "worker_cleanup",
        "worker_decide",
        "worker_resume",
    }
)


def _map_worker_state(status: Any) -> str:
    """Map a raw opencode session status to a stable worker state.

    Busy means the worker is active; retry means a retry is scheduled so
    the worker is still active. Error-like types map to error, anything
    missing or unrecognized maps to unknown.

    Args:
        status: Raw status dict (e.g. {type: busy}) or type string.

    Returns:
        One of running, idle, error, unknown.
    """
    raw_type = status.get("type") if isinstance(status, dict) else status
    if not isinstance(raw_type, str):
        return "unknown"
    normalized = raw_type.strip().lower()
    if normalized == "busy":
        return "running"
    if normalized == "idle":
        return "idle"
    if normalized == "retry":
        return "running"
    if "error" in normalized or "fail" in normalized:
        return "error"
    return "unknown"


def _is_free_model(model_id: Any, name: Any, cost: Any = None) -> bool:
    """Check whether a model counts as free (conservative).

    Only an explicit "free" marker in the model ID or name counts,
    case-insensitive. Zero cost metadata alone never counts as free:
    cost metadata is kept in catalog output but does not infer billing
    entitlement, per the no-paid/no-Copilot policy.

    Args:
        model_id: Model ID string.
        name: Human-readable model name.
        cost: Ignored cost metadata (kept for backward compatibility).

    Returns:
        True only when the ID/name contains "free" (case-insensitive).
    """
    _ = cost
    return "free" in f"{model_id or ''} {name or ''}".lower()


def _bound_text(value: Any, cap: int) -> str:
    """Bound an arbitrary value to a short string.

    Args:
        value: Value to stringify.
        cap: Max chars.

    Returns:
        Bounded string.
    """
    text = value if isinstance(value, str) else str(value)
    return text[:cap]


_GIT_ALLOWED_ARGS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("status", "--short"),
        ("diff", "--stat"),
        ("diff", "--check"),
        ("diff", "--name-only"),
        ("log", "-1", "--oneline"),
    }
)


async def _run_git(directory: str, args: list[str]) -> tuple[int | None, str]:
    """Run one fixed git command without a shell.

    Each invocation carries a narrowly scoped ``-c safe.directory=<dir>``
    entry so repositories owned by another UID (for example the human user
    when the bridge runs as the ``opencode-mcp`` service account) do not
    fail with Git's "dubious ownership" error. The scope is exactly the
    inspected directory: never ``*``, never a global/system config write,
    and never caller-provided commands (``args`` must be one of the fixed
    verification tuples).

    Args:
        directory: Repository directory passed as git -C target.
        args: Fixed git subcommand arguments (never caller-provided commands).

    Returns:
        Tuple of exit code (None on spawn/timeout failure or rejected
        arguments) and bounded output combining stdout and stderr.
    """
    if tuple(args) not in _GIT_ALLOWED_ARGS:
        return None, "unsupported git arguments"
    safe_value = f"safe.directory={directory}"
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            safe_value,
            "-C",
            directory,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "git executable not found"
    except (NotADirectoryError, PermissionError, OSError) as exc:
        return None, _bound_text(f"cannot run git: {exc}", WORKER_VERIFY_GIT_MAX_CHARS)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), WORKER_VERIFY_GIT_TIMEOUT_S)
    except TimeoutError:
        with suppress(Exception):
            process.kill()
            await process.communicate()
        return None, f"timed out after {WORKER_VERIFY_GIT_TIMEOUT_S}s"
    output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).rstrip("\n")
    output = output.rstrip("\r")
    return process.returncode, _bound_text(output, WORKER_VERIFY_GIT_MAX_CHARS)


def _parse_status_files(status_short: str) -> list[str]:
    """Extract file paths from git status --short output.

    The two leading status columns are positional and must not be stripped:
    " M file" (unstaged), "M  file" (staged), "?? file" (untracked), and
    "R  old -> new" (rename, resolves to the new path).

    Args:
        status_short: Raw status --short text.

    Returns:
        Bounded sorted file list.
    """
    files: set[str] = set()
    for line in status_short.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:]
        if not raw_path.strip():
            continue
        path = raw_path.strip().strip('"')[:300]
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()[:300]
        if path:
            files.add(path)
        if len(files) >= WORKER_VERIFY_GIT_MAX_FILES:
            break
    return sorted(files)[:WORKER_VERIFY_GIT_MAX_FILES]


async def _collect_verification(directory: str) -> dict[str, Any]:
    """Collect a bounded read-only git verification bundle.

    Runs only fixed git arguments via create_subprocess_exec, never a shell.
    Each git call carries a narrowly scoped ``-c safe.directory=<dir>`` so
    cross-UID checkouts verify without any global safe.directory change.

    Args:
        directory: Repository directory to inspect.

    Returns:
        Compact verification dict with ok flag, bounded git outputs,
        changed files, latest commit evidence, and an error field when
        the directory is unusable. latest_commit is informational only;
        it describes the directory HEAD and is never attributed to the task.
    """
    target = _bound_text(directory, 500)
    path = Path(target)
    if not path.exists():
        return {
            "ok": False,
            "directory": target,
            "status_short": "",
            "diff_stat": "",
            "diff_check": {"exit_code": None, "output": ""},
            "changed_files": [],
            "changed_count": 0,
            "latest_commit": "",
            "error": "directory not found",
        }
    if not path.is_dir():
        return {
            "ok": False,
            "directory": target,
            "status_short": "",
            "diff_stat": "",
            "diff_check": {"exit_code": None, "output": ""},
            "changed_files": [],
            "changed_count": 0,
            "latest_commit": "",
            "error": "not a directory",
        }
    status_code, status_out = await _run_git(target, ["status", "--short"])
    if status_code != 0:
        return {
            "ok": False,
            "directory": target,
            "status_short": status_out,
            "diff_stat": "",
            "diff_check": {"exit_code": None, "output": ""},
            "changed_files": [],
            "changed_count": 0,
            "latest_commit": "",
            "error": "not a git repository or git failed",
        }
    _, stat_out = await _run_git(target, ["diff", "--stat"])
    check_code, check_out = await _run_git(target, ["diff", "--check"])
    _, names_out = await _run_git(target, ["diff", "--name-only"])
    log_code, log_out = await _run_git(target, ["log", "-1", "--oneline"])
    latest_commit = (
        _bound_text(log_out.strip(), WORKER_VERIFY_COMMIT_MAX_CHARS) if log_code == 0 else ""
    )
    changed: set[str] = set(_parse_status_files(status_out))
    for line in names_out.splitlines():
        name = line.strip().strip('"')[:300]
        if name:
            changed.add(name)
        if len(changed) >= WORKER_VERIFY_GIT_MAX_FILES:
            break
    changed_files = sorted(changed)[:WORKER_VERIFY_GIT_MAX_FILES]
    return {
        "ok": True,
        "directory": target,
        "status_short": status_out,
        "diff_stat": stat_out,
        "diff_check": {"exit_code": check_code, "output": check_out},
        "changed_files": changed_files,
        "changed_count": len(changed_files),
        "latest_commit": latest_commit,
        "error": None,
    }


def _task_state_path() -> Path:
    """Return the configured JSON path for durable task records."""
    override = os.environ.get("TASK_STATE_PATH", "").strip()
    if override:
        return Path(override)
    try:
        return Path(get_settings().task_state_path)
    except RuntimeError:
        return Path("/var/lib/opencode-mcp-bridge/tasks.json")


def _normalize_request_id(request_id: str | None) -> str | None:
    """Validate an optional request ID before any side effect.

    Args:
        request_id: Caller-supplied idempotency key.

    Returns:
        Stripped request ID, or None when omitted.

    Raises:
        ValueError: If the ID is empty or over the bounded length.
    """
    if request_id is None:
        return None
    cleaned = request_id.strip()
    if not cleaned:
        raise ValueError("requestID must not be empty")
    if len(cleaned) > TASK_REQUEST_ID_MAX_CHARS:
        raise ValueError(f"requestID must be at most {TASK_REQUEST_ID_MAX_CHARS} chars")
    return cleaned


def _fingerprint_task(
    message: str,
    directory: str | None,
    title: str | None,
    agent: str | None,
    provider_id: str,
    model_id: str,
    requires_approval: bool = False,
    risky_action: str | None = None,
) -> str:
    """Hash task inputs to detect conflicting requestID reuse.

    The message text is hashed, never stored, so retries store no prompt.
    Title and agent are bounded with the same caps used for stored
    records, so an over-cap value hashes exactly like the value the
    registry keeps and resume matching cannot drift from storage.
    Pass the authorized canonical directory (not the raw spelling) so
    symlink and dot-segment spellings of one directory hash identically.

    Args:
        message: Task prompt text.
        directory: Authorized canonical directory (None means server default).
        title: Optional session title (bounded to TASK_TITLE_MAX_CHARS).
        agent: Optional agent override (bounded to TASK_AGENT_MAX_CHARS).
        provider_id: Resolved provider ID.
        model_id: Resolved model ID.
        requires_approval: Whether the task pauses for approval.
        risky_action: Optional bounded risky-action descriptor.

    Returns:
        Hex SHA256 fingerprint of the canonical inputs.
    """
    title_key = _bound_text(title or "", TASK_TITLE_MAX_CHARS) if title else ""
    agent_key = _bound_text(agent or "", TASK_AGENT_MAX_CHARS) if agent else ""
    canonical = json.dumps(
        {
            "message": message,
            "directory": _canonical_task_dir(directory),
            "title": title_key,
            "agent": agent_key,
            "providerID": provider_id,
            "modelID": model_id,
            "requires_approval": bool(requires_approval),
            "risky_action": risky_action or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_risky_action(risky_action: str | None) -> str | None:
    """Validate an optional risky-action descriptor before any side effect.

    Args:
        risky_action: Caller-supplied action name or None.

    Returns:
        Stripped descriptor, or None when omitted/blank.

    Raises:
        ValueError: If the descriptor is over the bounded length.
    """
    if risky_action is None:
        return None
    cleaned = risky_action.strip()
    if not cleaned:
        return None
    if len(cleaned) > WORKER_RISKY_ACTION_MAX_CHARS:
        raise ValueError(f"risky_action must be at most {WORKER_RISKY_ACTION_MAX_CHARS} chars")
    return cleaned


def _normalize_decision(decision: str | None) -> str:
    """Normalize an approval decision to approve or reject.

    Args:
        decision: Raw decision input.

    Returns:
        Either approve or reject.

    Raises:
        ValueError: If the decision is missing or not approve/reject.
    """
    if decision is None:
        raise ValueError("decision must be either 'approve' or 'reject'")
    cleaned = decision.strip().lower()
    if cleaned in ("approve", "approved"):
        return "approve"
    if cleaned in ("reject", "rejected"):
        return "reject"
    raise ValueError("decision must be either 'approve' or 'reject'")


def _normalize_approval_token(token: str | None) -> str:
    """Validate a presented approval token before any state change.

    Args:
        token: Presented approval token.

    Returns:
        Stripped token.

    Raises:
        ValueError: If the token is missing, blank, or over-bounded.
    """
    if token is None:
        raise ValueError("approval_token must not be empty")
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("approval_token must not be empty")
    if len(cleaned) > 128:
        raise ValueError("approval_token must be at most 128 chars")
    return cleaned


def _approval_ttl_s() -> int:
    """Return the bounded approval expiry window.

    Reads WORKER_APPROVAL_TTL_S via settings; falls back to the compiled
    default when settings are unavailable. Never raises.

    Returns:
        TTL in seconds, always positive and bounded.
    """
    try:
        value = get_settings().worker_approval_ttl_s
    except RuntimeError:
        return WORKER_APPROVAL_DEFAULT_TTL_S
    if isinstance(value, bool) or not isinstance(value, int):
        return WORKER_APPROVAL_DEFAULT_TTL_S
    if not (WORKER_APPROVAL_MIN_TTL_S <= value <= WORKER_APPROVAL_MAX_TTL_S):
        return WORKER_APPROVAL_DEFAULT_TTL_S
    return value


def _new_approval_token() -> str:
    """Generate a bounded random approval token.

    Returns:
        Hex token string (never empty, transport-safe).
    """
    import secrets as _secrets

    return _secrets.token_hex(WORKER_APPROVAL_TOKEN_HEX_BYTES)


def _new_approval_task_id() -> str:
    """Generate a transport-safe pending approval task ID.

    Returns:
        ID with the apr_ prefix plus random hex, never an opencode ses_.
    """
    import secrets as _secrets

    return f"{WORKER_APPROVAL_ID_PREFIX}{_secrets.token_hex(8)}"


def _is_approval_record(record: Any) -> bool:
    """Check whether a registry record is an approval-gated task.

    Args:
        record: Stored record or None.

    Returns:
        True when the record carries an approval_state field.
    """
    return isinstance(record, dict) and isinstance(record.get("approval_state"), str)


def _approval_expires_at(record: dict[str, Any]) -> float | None:
    """Return the expiry epoch for an approval record, or None.

    Args:
        record: Stored approval record.

    Returns:
        Epoch seconds or None when missing/unusable.
    """
    expires = record.get("expires_at")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    try:
        return float(expires)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_approval_expired(record: dict[str, Any], now: float | None = None) -> bool:
    """Check whether a pending approval passed its expiry.

    Only approval_required and approved states can expire; terminal
    states never re-expire.

    Args:
        record: Stored approval record.
        now: Epoch override for deterministic tests.

    Returns:
        True when the record is pending and past expires_at.
    """
    state = record.get("approval_state")
    if state not in (APPROVAL_STATE_REQUIRED, APPROVAL_STATE_APPROVED):
        return False
    expires = _approval_expires_at(record)
    if expires is None:
        return False
    current = now if now is not None else time.time()
    try:
        return float(current) >= float(expires)
    except (TypeError, ValueError, OverflowError):
        return False


def _expire_approval_record(
    tasks: dict[str, dict[str, Any]], task_id: str, now: float | None = None
) -> dict[str, Any] | None:
    """Mark a pending approval expired when past its deadline.

    Args:
        tasks: Mutable registry map (caller saves after).
        task_id: Approval task ID.
        now: Epoch override for deterministic tests.

    Returns:
        The expired record, or None when no transition happened.
    """
    record = tasks.get(task_id)
    if not isinstance(record, dict) or not _is_approval_record(record):
        return None
    if not _is_approval_expired(record, now):
        return None
    current = now if now is not None else time.time()
    record["approval_state"] = APPROVAL_STATE_EXPIRED
    record["decided_at"] = float(current)
    return record


def _match_approval_token(stored: Any, presented: str) -> bool:
    """Constant-time compare a stored approval token with the presented one.

    Args:
        stored: Stored token value.
        presented: Normalized presented token.

    Returns:
        True only on an exact match.
    """
    if not isinstance(stored, str) or not stored:
        return False
    try:
        return hmac.compare_digest(stored, presented)
    except (TypeError, ValueError):
        return False


def _build_approval_record(
    task_id: str,
    request_id: str | None,
    fingerprint: str,
    directory: Any,
    title: Any,
    agent: Any,
    provider_id: str,
    model_id: str,
    approval_token: str,
    risky_action: str | None,
    expires_at: float,
    created_at: float | None = None,
) -> dict[str, Any]:
    """Build a paused approval record with no prompt or secrets.

    Args:
        task_id: Pending approval task ID (apr_ prefix, not a session).
        request_id: Normalized request ID or None.
        fingerprint: Input hash for same-task resume matching.
        directory: Effective directory, stored exactly.
        title: Optional title.
        agent: Optional agent.
        provider_id: Resolved provider.
        model_id: Resolved model.
        approval_token: Random matching token for decide/resume.
        risky_action: Bounded descriptor or None.
        expires_at: Expiry epoch seconds.
        created_at: Epoch override, defaults to now.

    Returns:
        Bounded record dict safe for JSON persistence.
    """
    dir_text = directory or ""
    title_text = _bound_text(title or "", TASK_TITLE_MAX_CHARS) if title else None
    agent_text = _bound_text(agent or "", TASK_AGENT_MAX_CHARS) if agent else None
    return {
        "taskID": task_id,
        "requestID": request_id,
        "fingerprint": fingerprint,
        "directory": dir_text,
        "title": title_text,
        "agent": agent_text,
        "providerID": provider_id,
        "modelID": model_id,
        "created_at": created_at if created_at is not None else time.time(),
        "approval_state": APPROVAL_STATE_REQUIRED,
        "approval_token": approval_token,
        "risky_action": risky_action,
        "expires_at": float(expires_at),
        "decided_at": None,
        "decision": None,
        "resume_sessionID": None,
    }


def _load_task_state() -> dict[str, dict[str, Any]]:
    """Load durable task records keyed by taskID.

    Missing files return empty. Corrupt JSON, wrong shapes, or unreadable
    files raise a safe RuntimeError (path only, never file contents or
    secrets) so callers fail closed instead of duplicating sessions.

    Returns:
        Map of taskID to record dicts.

    Raises:
        RuntimeError: If the registry file is corrupt, mis-shaped, or
            unreadable.
    """
    path = _task_state_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"task registry at {path} is unreadable: {exc.strerror or 'I/O error'}")
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"task registry at {path} is corrupt: invalid JSON")
    if not isinstance(data, dict):
        raise RuntimeError(  # noqa: TRY004 - registry contract requires RuntimeError
            f"task registry at {path} is corrupt: expected a JSON object"
        )
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        raise RuntimeError(  # noqa: TRY004 - registry contract requires RuntimeError
            f"task registry at {path} is corrupt: expected a tasks object"
        )
    cleaned: dict[str, dict[str, Any]] = {}
    for task_id, record in tasks.items():
        if not isinstance(task_id, str) or not isinstance(record, dict):
            raise RuntimeError(  # noqa: TRY004 - registry contract requires RuntimeError
                f"task registry at {path} is corrupt: bad record shape"
            )
        cleaned[task_id] = record
    return cleaned


def _save_task_state(tasks: dict[str, dict[str, Any]]) -> None:
    """Persist task records atomically with bounded size.

    Oldest records are evicted first when over TASK_MAX_RECORDS. Writes
    go to a uniquely named temp file (O_EXCL via tempfile.mkstemp) in the
    same directory, are fsynced, then atomically moved over the registry
    with os.replace. Unique names keep concurrent writers from interleaving
    bytes into one shared temp file; the parent directory is fsynced
    best-effort so the rename survives a crash. The registry file stays
    owner-only (0600): mkstemp creates the temp file 0600, the mode is
    reasserted explicitly, and os.replace carries the temp inode over the
    destination, so a pre-existing lax file cannot survive a save. This
    matters because approval records carry bearer-equivalent
    approval_token values: keep TASK_STATE_PATH readable only by the
    bridge account (see SECURITY.md).

    Args:
        tasks: Map of taskID to record dicts.

    Raises:
        RuntimeError: If the registry cannot be written. The message
            carries the path only, never prompts or secrets.
    """
    bounded = dict(list(tasks.items())[-TASK_MAX_RECORDS:]) if tasks else {}
    path = _task_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"task registry at {path} is unwritable: {exc.strerror or 'I/O error'}")
    payload = json.dumps({"version": 1, "tasks": bounded})
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tasks.", suffix=".tmp")
    except OSError as exc:
        raise RuntimeError(f"task registry at {path} is unwritable: {exc.strerror or 'I/O error'}")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        with suppress(OSError, AttributeError):
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise RuntimeError(f"task registry at {path} is unwritable: {exc.strerror or 'I/O error'}")
    finally:
        with suppress(OSError):
            os.unlink(tmp_name)


def _find_task_by_request(
    tasks: dict[str, dict[str, Any]], request_id: str
) -> dict[str, Any] | None:
    """Find a stored record by request ID.

    Args:
        tasks: Map of taskID to records.
        request_id: Normalized request ID.

    Returns:
        Matching record or None.
    """
    for record in tasks.values():
        if record.get("requestID") == request_id:
            return record
    return None


async def _recorded_session_alive(client: Any, task_id: str, directory: Any) -> bool:
    """Check whether a recorded session still exists in OpenCode.

    A 404 (or an empty lookup result) means the session is gone and a retry
    may recreate it. Any other lookup failure returns True so retries stay
    side-effect-free instead of risking a duplicate session.

    Args:
        client: Opencode client.
        task_id: Recorded session/task ID.
        directory: Recorded directory for the directory-scoped lookup.

    Returns:
        True when the session is present or its state is uncertain.
    """
    try:
        session = await client.get_session(task_id, directory)
    except OpencodeError as exc:
        return exc.status != 404
    except Exception:  # noqa: BLE001 - uncertain state must not trigger recreation
        return True
    return isinstance(session, dict) and bool(session.get("id"))


def _build_task_record(
    task_id: str,
    request_id: str | None,
    fingerprint: str,
    directory: Any,
    title: Any,
    agent: Any,
    provider_id: str,
    model_id: str,
    created_at: float | None = None,
) -> dict[str, Any]:
    """Build a bounded record with no prompt, secrets, or credentials.

    Args:
        task_id: Session/task ID.
        request_id: Normalized request ID or None.
        fingerprint: Input hash for conflict detection.
        directory: Effective directory to recover later, stored exactly
            (over-long paths are rejected before creation, never truncated).
        title: Optional title.
        agent: Optional agent.
        provider_id: Resolved provider.
        model_id: Resolved model.
        created_at: Epoch seconds for stale classification. Defaults to now.
            Legacy records without this field are never classified stale.

    Returns:
        Bounded record dict safe for JSON persistence.
    """
    dir_text = directory or ""
    title_text = _bound_text(title or "", TASK_TITLE_MAX_CHARS) if title else None
    agent_text = _bound_text(agent or "", TASK_AGENT_MAX_CHARS) if agent else None
    return {
        "taskID": task_id,
        "requestID": request_id,
        "fingerprint": fingerprint,
        "directory": dir_text,
        "title": title_text,
        "agent": agent_text,
        "providerID": provider_id,
        "modelID": model_id,
        "created_at": created_at if created_at is not None else time.time(),
    }


def _remove_task_record(task_id: str) -> None:
    """Remove one task record.

    Callers mutating the registry must hold _locked_task_registry so
    concurrent worker_run sequences in this process and in sibling
    bridge processes sharing TASK_STATE_PATH cannot interleave between
    this load and save.

    Args:
        task_id: Task ID to drop.

    Raises:
        RuntimeError: If the registry is corrupt or unwritable.
    """
    tasks = _load_task_state()
    if task_id in tasks:
        del tasks[task_id]
        _save_task_state(tasks)


def _task_stale_after_s() -> int:
    """Return the bounded startup/progress timeout for stale classification.

    Reads the configured TASK_STALE_AFTER_S via settings; falls back to
    the compiled default when settings are unavailable (e.g. missing env
    in unit tests). Never raises.

    Returns:
        Timeout in seconds, always positive and bounded.
    """
    try:
        value = get_settings().task_stale_after_s
    except RuntimeError:
        return TASK_STALE_AFTER_DEFAULT_S
    if isinstance(value, bool) or not isinstance(value, int):
        return TASK_STALE_AFTER_DEFAULT_S
    if value <= 0:
        return TASK_STALE_AFTER_DEFAULT_S
    return value


def _task_age_s(record: dict[str, Any] | None, now: float | None = None) -> float | None:
    """Return the age of a task record in seconds, or None when unknown.

    Legacy records without a numeric created_at are never classified
    stale, preserving backward compatibility.

    Args:
        record: Stored task record or None.
        now: Epoch seconds override for deterministic tests.

    Returns:
        Age in seconds (>= 0), or None when the record has no usable timestamp.
    """
    if not isinstance(record, dict):
        return None
    created = record.get("created_at")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    current = now if now is not None else time.time()
    try:
        age = float(current) - float(created)
    except (TypeError, ValueError, OverflowError):
        return None
    if age < 0:
        return 0.0
    return age


def _classify_task_stale(
    record: dict[str, Any] | None,
    state: str,
    has_output: bool,
    include_output: bool,
    now: float | None = None,
) -> tuple[bool, str | None]:
    """Classify a running worker with empty output as stale when overdue.

    Only state == running with no assistant output and output actually
    fetched counts; idle/error/unknown and tasks with any output are
    never stale. Records without timestamps (legacy) are never stale.
    The reason carries only elapsed/limit seconds, never prompts, paths,
    tokens, or backend text.

    Args:
        record: Stored task record or None.
        state: Mapped worker state before staleness.
        has_output: True when any assistant output exists.
        include_output: False means output was not fetched, so no claim.
        now: Epoch seconds override for deterministic tests.

    Returns:
        Tuple of (is_stale, bounded reason or None).
    """
    if not include_output or state != "running" or has_output:
        return False, None
    age = _task_age_s(record, now)
    if age is None:
        return False, None
    limit = _task_stale_after_s()
    if age < limit:
        return False, None
    reason = f"running with empty output for {int(age)}s (limit {int(limit)}s); recovery needed"
    return True, _bound_text(reason, TASK_STALE_REASON_MAX_CHARS)


@mcp.tool(
    output_schema=WORKER_RUN_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_RUN_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def worker_run(
    message: str,
    directory: str | None = None,
    title: str | None = None,
    agent: str | None = None,
    providerID: str | None = None,
    modelID: str | None = None,
    requestID: str | None = None,
    requires_approval: bool = False,
    risky_action: str | None = None,
) -> dict[str, Any]:
    """Start a background worker: create a session and prompt it without waiting.

    Model overrides, request IDs, and directory lengths are validated
    before anything is created, so invalid input has no side effects. When
    requestID repeats with the same inputs, the existing task returns with
    deduplicated=true and no second session is created. If the recorded
    session no longer exists in OpenCode (registry survived while the
    session did not, e.g. after cleanup or server data loss), the retry
    transparently recreates it and returns deduplicated=false. Uncertain
    liveness (non-404 lookup failures) keeps the stored task with no side
    effects rather than risking a duplicate. Conflicting reuse of a
    requestID fails before any session is created. Every task is recorded
    in TASK_STATE_PATH JSON (no prompt or credentials); if the async prompt
    fails, the record is removed and the new session is deleted
    best-effort, then the original error is re-raised. A registry save
    failure also deletes the new session and raises instead of reporting
    success. The read/check/create/save/prompt sequence holds the
    asyncio lock plus a bounded cross-process file lock so concurrent
    retries in this process and in sibling bridge processes sharing
    TASK_STATE_PATH cannot duplicate sessions or clobber records. The
    file lock is held across slow OpenCode calls because the dedup
    check, liveness probe, creation, save, and failure cleanup must be
    one atomic unit; waiters time out fail-closed instead of
    duplicating work.

    Approval-gated risky work: pass requires_approval=True or a bounded
    risky_action descriptor to pause before any OpenCode call. The task
    is recorded as approval_required with a matching approval_token and
    an expires_at deadline (WORKER_APPROVAL_TTL_S, bounded); no session
    is created and no prompt is sent until worker_decide approves and
    worker_resume starts the same task with the same inputs. By default
    (requires_approval=False and no risky_action) behavior is unchanged
    and no risky production action is enabled.

    Pass the returned directory to worker_status when it differs from the
    configured default: status and messages are directory-scoped. The
    directory is also recoverable from the saved record when omitted.

    Args:
        message: Task prompt for the worker.
        directory: Working directory (must be within allowed directories,
            up to TASK_DIRECTORY_MAX_CHARS).
        title: Session title.
        agent: Optional agent override.
        providerID: Optional model override provider.
        modelID: Optional model override model.
        requestID: Optional idempotency key. Omit to keep legacy behavior.
        requires_approval: When true, pause as approval_required without
            any OpenCode side effect until decide plus resume.
        risky_action: Optional bounded descriptor for the risky action.
            Any non-blank value also pauses like requires_approval.

    Returns:
        Compact dict with taskID (= sessionID), sessionID, state,
        providerID, modelID, directory, title, agent, requestID, and
        deduplicated flag, plus the stable wait-friendly contract
        (timed_out=False, retryable, next_action, error_code, evidence).
        Approval pauses return state=approval_required with approval_state,
        approval_token (decide/resume only), risky_action, and expires_at.
        Use worker_wait to wait for progress without client polling and
        worker_status for an immediate snapshot.
    """
    _obs_start = time.perf_counter()
    _obs_request = observability.redact_request_id(requestID)
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_run",
        outcome=observability.OUTCOME_STARTED,
        request_id=_obs_request,
    )
    try:
        if not message or not message.strip():
            raise ValueError("message must not be empty")
        normalized_request = _normalize_request_id(requestID)
        normalized_risky = _normalize_risky_action(risky_action)
        approval_needed = bool(requires_approval) or normalized_risky is not None
        authorized_dir = _authorize_optional_directory(directory)
        client = get_client()
        resolved_provider, resolved_model = client.resolve_model(providerID, modelID)
        fingerprint = _fingerprint_task(
            message,
            authorized_dir,
            title,
            agent,
            resolved_provider,
            resolved_model,
            approval_needed,
            normalized_risky,
        )
        if approval_needed:
            async with _locked_task_registry():
                stored_tasks = _load_task_state()
                if normalized_request is not None:
                    existing = _find_task_by_request(stored_tasks, normalized_request)
                    if existing is not None:
                        if existing.get("fingerprint") != fingerprint:
                            raise ValueError("requestID was already used with different inputs")
                        if _is_approval_record(existing):
                            existing_dir = existing.get("directory")
                            if isinstance(existing_dir, str) and existing_dir.strip():
                                _authorize_directory(existing_dir)
                            task_id = existing.get("taskID")
                            transitioned = _expire_approval_record(
                                stored_tasks, task_id if isinstance(task_id, str) else ""
                            )
                            if transitioned is not None:
                                _save_task_state(stored_tasks)
                                existing = transitioned
                            state_now = (
                                existing.get("approval_state")
                                if isinstance(existing, dict)
                                else None
                            )
                            if state_now == APPROVAL_STATE_EXPIRED:
                                if isinstance(task_id, str):
                                    stored_tasks.pop(task_id, None)
                            elif state_now in (
                                APPROVAL_STATE_REQUIRED,
                                APPROVAL_STATE_APPROVED,
                                APPROVAL_STATE_REJECTED,
                                APPROVAL_STATE_RESUMED,
                            ):
                                _obs_result = {
                                    "taskID": task_id,
                                    "sessionID": existing.get("resume_sessionID"),
                                    "state": state_now,
                                    "providerID": existing.get("providerID", resolved_provider),
                                    "modelID": existing.get("modelID", resolved_model),
                                    "directory": existing.get("directory", ""),
                                    "title": existing.get("title"),
                                    "agent": existing.get("agent"),
                                    "requestID": normalized_request,
                                    "deduplicated": True,
                                    "timed_out": False,
                                    "retryable": state_now
                                    not in (APPROVAL_STATE_REJECTED, APPROVAL_STATE_EXPIRED),
                                    "next_action": (
                                        "worker_resume"
                                        if state_now == APPROVAL_STATE_APPROVED
                                        else "worker_decide"
                                        if state_now == APPROVAL_STATE_REQUIRED
                                        else "worker_wait"
                                        if state_now == APPROVAL_STATE_RESUMED
                                        else "worker_run"
                                    ),
                                    "error_code": (
                                        "approval_expired"
                                        if state_now == APPROVAL_STATE_EXPIRED
                                        else "approval_rejected"
                                        if state_now == APPROVAL_STATE_REJECTED
                                        else None
                                    ),
                                    "evidence": {
                                        "status": state_now,
                                        "messageID": None,
                                        "output_chars": 0,
                                        "total_chars": 0,
                                    },
                                    "approval_state": state_now,
                                    "approval_token": existing.get("approval_token"),
                                    "risky_action": existing.get("risky_action"),
                                    "expires_at": existing.get("expires_at"),
                                }
                                observability.emit(
                                    event=observability.EVENT_WORKER,
                                    tool="worker_run",
                                    outcome=observability.OUTCOME_SUCCEEDED,
                                    duration_ms=observability.duration_ms_since(_obs_start),
                                    request_id=_obs_request,
                                    task_id=observability.safe_task_id(task_id),
                                )
                                return _obs_result
                            else:
                                raise ValueError("requestID was already used with different inputs")
                        else:
                            raise ValueError("requestID was already used with different inputs")
                now = time.time()
                expires_at = now + float(_approval_ttl_s())
                task_id = _new_approval_task_id()
                while task_id in stored_tasks:
                    task_id = _new_approval_task_id()
                token = _new_approval_token()
                record = _build_approval_record(
                    task_id,
                    normalized_request,
                    fingerprint,
                    authorized_dir,
                    title,
                    agent,
                    resolved_provider,
                    resolved_model,
                    token,
                    normalized_risky,
                    expires_at,
                    created_at=now,
                )
                stored_tasks[task_id] = record
                _save_task_state(stored_tasks)
                _obs_result = {
                    "taskID": task_id,
                    "sessionID": None,
                    "state": APPROVAL_STATE_REQUIRED,
                    "providerID": resolved_provider,
                    "modelID": resolved_model,
                    "directory": authorized_dir,
                    "title": title,
                    "agent": agent,
                    "requestID": normalized_request,
                    "deduplicated": False,
                    "timed_out": False,
                    "retryable": True,
                    "next_action": "worker_decide",
                    "error_code": None,
                    "evidence": {
                        "status": APPROVAL_STATE_REQUIRED,
                        "messageID": None,
                        "output_chars": 0,
                        "total_chars": 0,
                    },
                    "approval_state": APPROVAL_STATE_REQUIRED,
                    "approval_token": token,
                    "risky_action": normalized_risky,
                    "expires_at": expires_at,
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_run",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    request_id=_obs_request,
                    task_id=observability.safe_task_id(task_id),
                )
                return _obs_result
        async with _locked_task_registry():
            stored_tasks = _load_task_state()
            if normalized_request is not None:
                existing = _find_task_by_request(stored_tasks, normalized_request)
                if existing is not None:
                    if existing.get("fingerprint") != fingerprint:
                        raise ValueError("requestID was already used with different inputs")
                    existing_dir = existing.get("directory")
                    if isinstance(existing_dir, str) and existing_dir.strip():
                        _authorize_directory(existing_dir)
                    task_id = existing.get("taskID")
                    alive = (
                        await _recorded_session_alive(client, task_id, existing.get("directory"))
                        if isinstance(task_id, str) and task_id
                        else False
                    )
                    if alive:
                        _obs_result = {
                            "taskID": task_id,
                            "sessionID": task_id,
                            "state": "running",
                            "providerID": existing.get("providerID", resolved_provider),
                            "modelID": existing.get("modelID", resolved_model),
                            "directory": existing.get("directory", ""),
                            "title": existing.get("title"),
                            "agent": existing.get("agent"),
                            "requestID": normalized_request,
                            "deduplicated": True,
                            "timed_out": False,
                            "retryable": True,
                            "next_action": "worker_wait",
                            "error_code": None,
                            "evidence": {
                                "status": "running",
                                "messageID": None,
                                "output_chars": 0,
                                "total_chars": 0,
                            },
                            "approval_state": None,
                            "approval_token": None,
                            "risky_action": None,
                            "expires_at": None,
                        }
                        observability.emit(
                            event=observability.EVENT_WORKER,
                            tool="worker_run",
                            outcome=observability.OUTCOME_SUCCEEDED,
                            duration_ms=observability.duration_ms_since(_obs_start),
                            request_id=_obs_request,
                            task_id=observability.safe_task_id(task_id),
                        )
                        return _obs_result
                    if isinstance(task_id, str):
                        stored_tasks.pop(task_id, None)
            session = await client.create_session(title, authorized_dir)
            session_id = session.get("id") if isinstance(session, dict) else None
            if not session_id:
                raise ValueError("opencode session response contained no id")
            effective_dir = session.get("directory") if isinstance(session, dict) else None
            effective_title = session.get("title") if isinstance(session, dict) else None
            if directory is None:
                resolved_raw = effective_dir or authorized_dir
            else:
                resolved_raw = effective_dir or authorized_dir
            resolved_dir = _authorize_directory(resolved_raw)
            record = _build_task_record(
                session_id,
                normalized_request,
                fingerprint,
                resolved_dir,
                effective_title if effective_title is not None else title,
                agent,
                resolved_provider,
                resolved_model,
            )
            stored_tasks[session_id] = record
            try:
                _save_task_state(stored_tasks)
            except Exception:
                with suppress(Exception):
                    await client.delete_session(session_id, authorized_dir)
                raise
            try:
                await client.prompt_async(
                    session_id, message, providerID, modelID, agent, authorized_dir
                )
            except Exception:
                with suppress(Exception):
                    tasks = _load_task_state()
                    if session_id in tasks:
                        del tasks[session_id]
                        _save_task_state(tasks)
                with suppress(Exception):
                    await client.delete_session(session_id, authorized_dir)
                raise
            _obs_result = {
                "taskID": session_id,
                "sessionID": session_id,
                "state": "running",
                "providerID": resolved_provider,
                "modelID": resolved_model,
                "directory": resolved_dir,
                "title": effective_title if effective_title is not None else title,
                "agent": agent,
                "requestID": normalized_request,
                "deduplicated": False,
                "timed_out": False,
                "retryable": True,
                "next_action": "worker_wait",
                "error_code": None,
                "evidence": {
                    "status": "running",
                    "messageID": None,
                    "output_chars": 0,
                    "total_chars": 0,
                },
                "approval_state": None,
                "approval_token": None,
                "risky_action": None,
                "expires_at": None,
            }
            observability.emit(
                event=observability.EVENT_WORKER,
                tool="worker_run",
                outcome=observability.OUTCOME_SUCCEEDED,
                duration_ms=observability.duration_ms_since(_obs_start),
                request_id=_obs_request,
                task_id=observability.safe_task_id(session_id),
            )
            return _obs_result
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_run",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            request_id=_obs_request,
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


@mcp.tool(
    output_schema=WORKER_DECIDE_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_DECIDE_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def worker_decide(
    taskID: str,
    decision: str,
    approval_token: str,
    directory: str | None = None,
) -> dict[str, Any]:
    """Decide a paused approval-gated task without starting any work.

    Approve moves approval_required to approved (still no OpenCode call;
    start it with worker_resume and the same inputs). Reject moves it to
    rejected, terminal. Expired, duplicate, or mismatched decisions fail
    safely with no state change and no side effects. The approval token
    must match the token returned by worker_run for the same taskID.
    An optional directory must canonically match the stored directory
    when supplied; omitted recovers the stored directory.

    Args:
        taskID: Pending approval task ID from worker_run (apr_ prefix).
        decision: Either approve or reject (approved/rejected accepted).
        approval_token: Matching token returned by worker_run.
        directory: Optional directory that must match the stored record.

    Returns:
        Compact dict with taskID, state, approval_state, decision,
        risky_action, directory, expires_at, decided_at, and the stable
        wait-friendly contract. Never exposes the token.

    Raises:
        ValueError: On unknown tasks, mismatched tokens, expired or
            already-decided approvals, or directory mismatch.
    """
    _obs_start = time.perf_counter()
    _obs_task = observability.safe_task_id(taskID)
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_decide",
        outcome=observability.OUTCOME_STARTED,
        task_id=_obs_task,
    )
    try:
        if not taskID or not taskID.strip():
            raise ValueError("taskID must not be empty")
        normalized_decision = _normalize_decision(decision)
        presented = _normalize_approval_token(approval_token)
        async with _locked_task_registry():
            stored_tasks = _load_task_state()
            record = stored_tasks.get(taskID)
            if not isinstance(record, dict) or not _is_approval_record(record):
                raise ValueError("unknown approval taskID")
            stored_dir = record.get("directory")
            if directory is not None:
                authorized = _authorize_directory(directory)
                if not isinstance(stored_dir, str) or not stored_dir.strip():
                    raise ValueError("directory does not match the approval record")
                if _realpath_str(stored_dir) != _realpath_str(authorized):
                    raise ValueError("directory does not match the approval record")
                effective_dir = authorized
            elif isinstance(stored_dir, str) and stored_dir.strip():
                effective_dir = _authorize_directory(stored_dir)
            else:
                effective_dir = _authorize_optional_directory(None)
            if not _match_approval_token(record.get("approval_token"), presented):
                raise ValueError("approval_token does not match this task")
            transitioned = _expire_approval_record(stored_tasks, taskID)
            if transitioned is not None:
                _save_task_state(stored_tasks)
                record = transitioned
            state_now = record.get("approval_state")
            if state_now == APPROVAL_STATE_EXPIRED:
                raise ValueError("approval has expired; re-run worker_run for this task only")
            if state_now != APPROVAL_STATE_REQUIRED:
                raise ValueError("approval was already decided for this task")
            now = time.time()
            if normalized_decision == "approve":
                record["approval_state"] = APPROVAL_STATE_APPROVED
                record["decision"] = "approve"
            else:
                record["approval_state"] = APPROVAL_STATE_REJECTED
                record["decision"] = "reject"
            record["decided_at"] = float(now)
            _save_task_state(stored_tasks)
            next_action = "worker_resume" if normalized_decision == "approve" else "worker_run"
            _obs_result = {
                "taskID": taskID,
                "sessionID": record.get("resume_sessionID"),
                "state": record["approval_state"],
                "approval_state": record["approval_state"],
                "decision": record["decision"],
                "risky_action": record.get("risky_action"),
                "directory": effective_dir,
                "expires_at": record.get("expires_at"),
                "decided_at": record.get("decided_at"),
                "deduplicated": False,
                "timed_out": False,
                "retryable": normalized_decision == "approve",
                "next_action": next_action,
                "error_code": None,
                "evidence": {
                    "status": record["approval_state"],
                    "messageID": None,
                    "output_chars": 0,
                    "total_chars": 0,
                },
            }
            observability.emit(
                event=observability.EVENT_WORKER,
                tool="worker_decide",
                outcome=observability.OUTCOME_SUCCEEDED,
                duration_ms=observability.duration_ms_since(_obs_start),
                task_id=_obs_task,
                action=normalized_decision,
            )
            return _obs_result
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_decide",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


@mcp.tool(
    output_schema=WORKER_RESUME_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_RESUME_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def worker_resume(
    taskID: str,
    approval_token: str,
    message: str,
    directory: str | None = None,
) -> dict[str, Any]:
    """Resume an approved task by starting the deferred worker exactly once.

    Only approval_required tasks that worker_decide approved, with a
    matching approval_token and the same inputs (message fingerprint),
    unexpired, and not already resumed, may start. The deferred session
    is created and prompted once; duplicates, mismatches, rejections,
    expirations, and re-resumes fail safely with no second session.
    The prompt text is hashed for same-task matching and never stored.

    Args:
        taskID: Approved task ID from worker_run (apr_ prefix).
        approval_token: Matching token returned by worker_run.
        message: The same task prompt originally paused (matched by hash).
        directory: Optional directory that must match the stored record.

    Returns:
        Compact dict with taskID (approval ID), sessionID (new opencode
        session), state=resumed, approval_state, directory, model, and
        the stable wait-friendly contract.

    Raises:
        ValueError: On unknown tasks, mismatched tokens or inputs,
            unapproved/rejected/expired/already-resumed approvals, or
            directory mismatch. Registry or prompt failures clean up
            safely without claiming success.
    """
    _obs_start = time.perf_counter()
    _obs_task = observability.safe_task_id(taskID)
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_resume",
        outcome=observability.OUTCOME_STARTED,
        task_id=_obs_task,
    )
    try:
        if not taskID or not taskID.strip():
            raise ValueError("taskID must not be empty")
        if not message or not message.strip():
            raise ValueError("message must not be empty")
        presented = _normalize_approval_token(approval_token)
        async with _locked_task_registry():
            stored_tasks = _load_task_state()
            record = stored_tasks.get(taskID)
            if not isinstance(record, dict) or not _is_approval_record(record):
                raise ValueError("unknown approval taskID")
            stored_dir = record.get("directory")
            if directory is not None:
                authorized = _authorize_directory(directory)
                if not isinstance(stored_dir, str) or not stored_dir.strip():
                    raise ValueError("directory does not match the approval record")
                if _realpath_str(stored_dir) != _realpath_str(authorized):
                    raise ValueError("directory does not match the approval record")
                effective_dir = authorized
            elif isinstance(stored_dir, str) and stored_dir.strip():
                effective_dir = _authorize_directory(stored_dir)
            else:
                effective_dir = _authorize_optional_directory(None)
            if not _match_approval_token(record.get("approval_token"), presented):
                raise ValueError("approval_token does not match this task")
            transitioned = _expire_approval_record(stored_tasks, taskID)
            if transitioned is not None:
                _save_task_state(stored_tasks)
                record = transitioned
            state_now = record.get("approval_state")
            if state_now == APPROVAL_STATE_EXPIRED:
                raise ValueError("approval has expired; re-run worker_run for this task only")
            if state_now == APPROVAL_STATE_REQUIRED:
                raise ValueError("approval is still pending; decide approve first")
            if state_now == APPROVAL_STATE_REJECTED:
                raise ValueError("approval was rejected for this task")
            if state_now == APPROVAL_STATE_RESUMED:
                raise ValueError("approval was already resumed for this task")
            if state_now != APPROVAL_STATE_APPROVED:
                raise ValueError("approval is not resumable for this task")
            provider_id = record.get("providerID")
            model_id = record.get("modelID")
            if not isinstance(provider_id, str) or not provider_id:
                raise ValueError("approval record is missing provider info")
            if not isinstance(model_id, str) or not model_id:
                raise ValueError("approval record is missing model info")
            candidate = _fingerprint_task(
                message,
                directory if directory is not None else None,
                record.get("title"),
                record.get("agent"),
                provider_id,
                model_id,
                True,
                record.get("risky_action"),
            )
            variants = {
                candidate,
                _fingerprint_task(
                    message,
                    None,
                    record.get("title"),
                    record.get("agent"),
                    provider_id,
                    model_id,
                    True,
                    record.get("risky_action"),
                ),
            }
            if isinstance(stored_dir, str):
                variants.add(
                    _fingerprint_task(
                        message,
                        stored_dir,
                        record.get("title"),
                        record.get("agent"),
                        provider_id,
                        model_id,
                        True,
                        record.get("risky_action"),
                    )
                )
            if record.get("fingerprint") not in variants:
                raise ValueError("message does not match the approved task inputs")
            client = get_client()
            stored_title = record.get("title")
            stored_agent = record.get("agent")
            session = await client.create_session(stored_title, effective_dir)
            session_id = session.get("id") if isinstance(session, dict) else None
            if not session_id:
                raise ValueError("opencode session response contained no id")
            try:
                await client.prompt_async(
                    session_id, message, provider_id, model_id, stored_agent, effective_dir
                )
            except Exception:
                with suppress(Exception):
                    await client.delete_session(session_id, effective_dir)
                raise
            record["approval_state"] = APPROVAL_STATE_RESUMED
            record["resume_sessionID"] = session_id
            try:
                _save_task_state(stored_tasks)
            except Exception:
                with suppress(Exception):
                    await client.delete_session(session_id, effective_dir)
                record["approval_state"] = APPROVAL_STATE_APPROVED
                record["resume_sessionID"] = None
                with suppress(Exception):
                    _save_task_state(stored_tasks)
                raise
            stored_request = record.get("requestID")
            _obs_result = {
                "taskID": taskID,
                "sessionID": session_id,
                "state": APPROVAL_STATE_RESUMED,
                "approval_state": APPROVAL_STATE_RESUMED,
                "risky_action": record.get("risky_action"),
                "directory": effective_dir,
                "providerID": provider_id,
                "modelID": model_id,
                "title": stored_title,
                "agent": stored_agent,
                "requestID": stored_request,
                "deduplicated": False,
                "timed_out": False,
                "retryable": True,
                "next_action": "worker_wait",
                "error_code": None,
                "evidence": {
                    "status": APPROVAL_STATE_RESUMED,
                    "messageID": None,
                    "output_chars": 0,
                    "total_chars": 0,
                },
            }
            observability.emit(
                event=observability.EVENT_WORKER,
                tool="worker_resume",
                outcome=observability.OUTCOME_SUCCEEDED,
                duration_ms=observability.duration_ms_since(_obs_start),
                task_id=_obs_task,
                action="resume",
            )
            return _obs_result
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_resume",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
            action="resume",
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


def _resolve_worker_scope(
    taskID: str, directory: str | None
) -> tuple[str, str, dict[str, Any] | None]:
    """Resolve directory scope for worker snapshot/wait tools.

    Explicit directories are authorized strictly; omitted directories are
    recovered from the saved task record, else the server default. With an
    explicit directory the registry is read best-effort for staleness
    metadata only, so a corrupt registry never fails the call.

    Args:
        taskID: Task ID from worker_run.
        directory: Working directory override or None.

    Returns:
        Tuple of (effective_query, effective_dir, stale_record).
    """
    saved: dict[str, Any] | None = None
    if directory is None:
        saved = _load_task_state().get(taskID)
    if directory is not None:
        authorized = _authorize_directory(directory)
        stale_record: dict[str, Any] | None = None
        with suppress(Exception):
            stale_record = _load_task_state().get(taskID)
            if not isinstance(stale_record, dict):
                stale_record = None
        return authorized, authorized, stale_record
    if saved and saved.get("directory"):
        saved_dir = saved.get("directory")
        if not isinstance(saved_dir, str) or not saved_dir.strip():
            effective = _authorize_optional_directory(None)
        else:
            effective = _authorize_directory(saved_dir)
        return effective, effective, saved if isinstance(saved, dict) else None
    effective = _authorize_optional_directory(None)
    return effective, effective, saved if isinstance(saved, dict) else None


def _approval_pending_view(
    taskID: str,
    record: dict[str, Any],
    effective_dir: str,
) -> dict[str, Any] | None:
    """Build a side-effect-free snapshot for a non-resumed approval.

    Expiry is computed on the fly without registry writes; decide and
    resume persist the transition under the registry lock. Resumed
    approvals return None so callers poll the live session instead.

    Args:
        taskID: Approval task ID.
        record: Stored approval record.
        effective_dir: Directory to report.

    Returns:
        Snapshot dict with approval_state, or None when not applicable.
    """
    if not _is_approval_record(record):
        return None
    state_now = record.get("approval_state")
    if state_now == APPROVAL_STATE_RESUMED:
        return None
    if state_now in (APPROVAL_STATE_REQUIRED, APPROVAL_STATE_APPROVED) and _is_approval_expired(
        record
    ):
        state_now = APPROVAL_STATE_EXPIRED
    if state_now not in (
        APPROVAL_STATE_REQUIRED,
        APPROVAL_STATE_APPROVED,
        APPROVAL_STATE_REJECTED,
        APPROVAL_STATE_EXPIRED,
    ):
        return None
    recovery_hint = WORKER_APPROVAL_EXPIRED_HINT if state_now == APPROVAL_STATE_EXPIRED else None
    return {
        "taskID": taskID,
        "sessionID": None,
        "state": state_now,
        "status": state_now,
        "messageID": None,
        "output": "",
        "output_chars": 0,
        "total_chars": 0,
        "truncated_chars": 0,
        "truncated": False,
        "directory": effective_dir,
        "stale": False,
        "stale_reason": None,
        "recovery_hint": recovery_hint,
        "approval_state": state_now,
        "risky_action": record.get("risky_action"),
        "expires_at": record.get("expires_at"),
    }


async def _snapshot_worker(
    taskID: str,
    effective_query: str,
    effective_dir: str,
    stale_record: dict[str, Any] | None,
    include_output: bool,
    cap: int,
) -> dict[str, Any]:
    """Collect one immediate worker snapshot without any LLM call.

    Only read-only OpenCode reads are used (GET /session/status plus the
    latest assistant message). Never creates, prompts, aborts, or deletes.
    A missing session surfaces as state unknown: when the status map has
    no entry and the message fetch reports 404 (or the backend 500 for
    a missing session), the snippet is discarded and an empty assistant
    view is used. Transport, auth, and active-task failures still raise.

    Args:
        taskID: Task ID from worker_run.
        effective_query: Directory-scoped query path.
        effective_dir: Directory to report.
        stale_record: Registry record for stale classification or None.
        include_output: When false, skip fetching messages.
        cap: Bounded output cap.

    Returns:
        Base snapshot dict without the stable wait contract envelope.
    """
    client = get_client()
    statuses = await client.get_session_status(effective_query)
    raw = statuses.get(taskID) if isinstance(statuses, dict) else None
    status = raw.get("type") if isinstance(raw, dict) else raw
    state = _map_worker_state(raw)
    message_id: str | None = None
    output: str | None = None
    output_chars = 0
    total_chars = 0
    truncated_chars = 0
    truncated = False
    if include_output:
        try:
            latest = await client.get_latest_assistant(taskID, effective_query, max_chars=cap + 1)
        except OpencodeError as exc:
            if raw is None and exc.status in (404, 500):
                latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
            else:
                raise
        message_id = latest.get("messageID")
        if latest.get("has_error"):
            state = "error"
        elif raw is None and message_id is not None:
            state = "idle"
        total_chars = int(latest.get("total_chars", 0) or 0)
        text = latest.get("text", "") or ""
        if total_chars > cap:
            output = text[:cap]
            truncated = True
            truncated_chars = total_chars - cap
        else:
            output = text
        output_chars = len(output) if output is not None else 0
    has_output = bool(message_id) or total_chars > 0
    stale, stale_reason = _classify_task_stale(stale_record, state, has_output, include_output)
    if stale:
        state = "stale"
    recovery_hint = WORKER_STALE_RECOVERY_HINT if stale else None
    return {
        "taskID": taskID,
        "sessionID": taskID,
        "state": state,
        "status": status,
        "messageID": message_id,
        "output": output,
        "output_chars": output_chars,
        "total_chars": total_chars,
        "truncated_chars": truncated_chars,
        "truncated": truncated,
        "directory": effective_dir,
        "stale": stale,
        "stale_reason": stale_reason,
        "recovery_hint": recovery_hint,
    }


@mcp.tool(
    output_schema=WORKER_STATUS_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_STATUS_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def worker_status(
    taskID: str,
    directory: str | None = None,
    include_output: bool = True,
    max_output_chars: int = WORKER_OUTPUT_DEFAULT_CHARS,
) -> dict[str, Any]:
    """Poll a background worker for state and its latest assistant text.

    Immediate snapshot fallback for worker_wait: never blocks, never
    invokes an LLM call, never requires client sleep polling. Pass the
    directory returned by worker_run when it differs from the configured
    default: status and messages are directory-scoped. When directory is
    omitted, the saved task record is used to recover it. A worker that
    stays running with empty output past TASK_STALE_AFTER_S is classified
    stale with a bounded reason and a recovery hint; clean it with
    worker_cleanup action=delete for this taskID only.

    Args:
        taskID: Task ID from worker_run (the session ID).
        directory: Working directory override.
        include_output: When false, skip fetching messages.
        max_output_chars: Output cap, clamped to a bounded range.

    Returns:
        Compact dict with taskID, sessionID, state
        (running/idle/error/unknown/stale), raw status, messageID, latest
        output only, output_chars, total_chars, truncated_chars, a truncated
        flag, directory, plus stale, stale_reason, and recovery_hint, plus
        the stable contract (timed_out=False, retryable, next_action,
        error_code, evidence). Only output text is bounded; directory paths
        are returned exactly as requested or saved. stale_reason carries
        elapsed/limit seconds only, never prompts, paths, tokens, or
        backend text. Never dumps full history. GET /session/status
        contains active sessions only, so an absent raw status with a
        non-null assistant messageID and no assistant error infers idle;
        absent status with no assistant stays unknown.

    Raises:
        ValueError: If taskID is empty.
    """
    _obs_start = time.perf_counter()
    _obs_task = observability.safe_task_id(taskID)
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_status",
        outcome=observability.OUTCOME_STARTED,
        task_id=_obs_task,
    )
    try:
        if not taskID or not taskID.strip():
            raise ValueError("taskID must not be empty")
        effective_query, effective_dir, stale_record = _resolve_worker_scope(taskID, directory)
        cap = max(1, min(max_output_chars, WORKER_OUTPUT_MAX_CHARS))
        if isinstance(stale_record, dict) and _is_approval_record(stale_record):
            pending = _approval_pending_view(taskID, stale_record, effective_dir)
            if pending is not None:
                error_code = _error_code_for_snapshot(pending["state"], None, pending["messageID"])
                _obs_result = {
                    **pending,
                    "timed_out": False,
                    "retryable": _retryable_for_state(pending["state"], False, False),
                    "next_action": _next_action_for_state(pending["state"], False),
                    "error_code": error_code,
                    "evidence": _worker_evidence(pending["status"], pending["messageID"], 0, 0),
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_status",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    task_id=_obs_task,
                )
                return _obs_result
            resume_session = stale_record.get("resume_sessionID")
            if (
                stale_record.get("approval_state") == APPROVAL_STATE_RESUMED
                and isinstance(resume_session, str)
                and resume_session
            ):
                live = await _snapshot_worker(
                    resume_session,
                    effective_query,
                    effective_dir,
                    None,
                    include_output,
                    cap,
                )
                _obs_result = {
                    **live,
                    "taskID": taskID,
                    "directory": effective_dir,
                    "approval_state": APPROVAL_STATE_RESUMED,
                    "risky_action": stale_record.get("risky_action"),
                    "expires_at": stale_record.get("expires_at"),
                    "timed_out": False,
                    "retryable": _retryable_for_state(live["state"], False, live["stale"]),
                    "next_action": _next_action_for_state(live["state"], False),
                    "error_code": _error_code_for_snapshot(
                        live["state"], live["status"], live["messageID"]
                    ),
                    "evidence": _worker_evidence(
                        live["status"],
                        live["messageID"],
                        live["output_chars"],
                        live["total_chars"],
                    ),
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_status",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    task_id=_obs_task,
                )
                return _obs_result
        base = await _snapshot_worker(
            taskID, effective_query, effective_dir, stale_record, include_output, cap
        )
        error_code = _error_code_for_snapshot(base["state"], base["status"], base["messageID"])
        _obs_result = {
            **base,
            "timed_out": False,
            "retryable": _retryable_for_state(base["state"], False, base["stale"]),
            "next_action": _next_action_for_state(base["state"], False),
            "error_code": error_code,
            "evidence": _worker_evidence(
                base["status"], base["messageID"], base["output_chars"], base["total_chars"]
            ),
            "approval_state": None,
            "risky_action": None,
            "expires_at": None,
        }
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_status",
            outcome=observability.OUTCOME_SUCCEEDED,
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
        )
        return _obs_result
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_status",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


@mcp.tool(
    output_schema=WORKER_WAIT_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_WAIT_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def worker_wait(
    taskID: str,
    directory: str | None = None,
    timeout_s: float | None = WORKER_WAIT_DEFAULT_TIMEOUT_S,
    include_output: bool = True,
    max_output_chars: int = WORKER_OUTPUT_DEFAULT_CHARS,
) -> dict[str, Any]:
    """Wait for a background worker to change state, bounded server-side.

    Coordinator-friendly alternative to client sleep polling: the server
    polls OpenCode on the caller's behalf and returns as soon as the task
    state (or assistant output progress) changes, or when the bounded
    timeout expires. Never waits indefinitely: timeout_s is clamped to a
    finite [1, 120]s range and the call always returns by the deadline.
    Never invokes an extra LLM call: only read-only GET /session/status
    and the latest assistant message are used, never prompt_async. Keep
    worker_run asynchronous and use worker_status for an immediate
    snapshot fallback.

    Args:
        taskID: Task ID from worker_run (the session ID).
        directory: Working directory override (same scoping as
            worker_status; omitted recovers the saved record).
        timeout_s: Max seconds to wait, clamped to a bounded range.
        include_output: When false, skip fetching messages.
        max_output_chars: Output cap, clamped to a bounded range.

    Returns:
        worker_status snapshot plus the stable contract: timed_out
        (True only when the deadline expired without a change), changed
        (True when state/output progressed before the deadline),
        elapsed_s, timeout_s, retryable, next_action, error_code
        ("task_not_found" when the session is absent), and concise
        evidence. Missing tasks return immediately with state unknown.
        Pending approvals (required/approved/rejected/expired) also
        return immediately without polling; resumed approvals long-poll
        the live resumed session exactly like a running task.

    Raises:
        ValueError: If taskID is empty or timeout_s is not finite.
    """
    _obs_start = time.perf_counter()
    _obs_task = observability.safe_task_id(taskID)
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_wait",
        outcome=observability.OUTCOME_STARTED,
        task_id=_obs_task,
    )
    try:
        if not taskID or not taskID.strip():
            raise ValueError("taskID must not be empty")
        bounded_timeout = _clamp_worker_wait_timeout(timeout_s)
        cap = max(1, min(max_output_chars, WORKER_OUTPUT_MAX_CHARS))
        effective_query, effective_dir, stale_record = _resolve_worker_scope(taskID, directory)
        start = time.monotonic()
        if isinstance(stale_record, dict) and _is_approval_record(stale_record):
            pending = _approval_pending_view(taskID, stale_record, effective_dir)
            if pending is not None:
                first_error = _error_code_for_snapshot(pending["state"], None, pending["messageID"])
                return {
                    **pending,
                    "timed_out": False,
                    "changed": False,
                    "elapsed_s": round(time.monotonic() - start, 3),
                    "timeout_s": bounded_timeout,
                    "retryable": _retryable_for_state(pending["state"], False, False),
                    "next_action": _next_action_for_state(pending["state"], False),
                    "error_code": first_error,
                    "evidence": _worker_evidence(pending["status"], None, 0, 0),
                }
            resume_session = stale_record.get("resume_sessionID")
            if (
                stale_record.get("approval_state") == APPROVAL_STATE_RESUMED
                and isinstance(resume_session, str)
                and resume_session
            ):
                live_first = await _snapshot_worker(
                    resume_session,
                    effective_query,
                    effective_dir,
                    None,
                    include_output,
                    cap,
                )
                live_state = live_first["state"]
                live_message = live_first["messageID"]
                live_total = live_first["total_chars"]
                live_error = _error_code_for_snapshot(
                    live_state, live_first["status"], live_message
                )
                if live_state != "running" or live_error is not None or live_first.get("stale"):
                    merged: dict[str, Any] = {
                        **live_first,
                        "taskID": taskID,
                        "directory": effective_dir,
                        "approval_state": APPROVAL_STATE_RESUMED,
                        "risky_action": stale_record.get("risky_action"),
                        "expires_at": stale_record.get("expires_at"),
                    }
                    return {
                        **merged,
                        "timed_out": False,
                        "changed": False,
                        "elapsed_s": round(time.monotonic() - start, 3),
                        "timeout_s": bounded_timeout,
                        "retryable": _retryable_for_state(live_state, False, live_first["stale"]),
                        "next_action": _next_action_for_state(live_state, False),
                        "error_code": live_error,
                        "evidence": _worker_evidence(
                            live_first["status"],
                            live_message,
                            live_first["output_chars"],
                            live_total,
                        ),
                    }
                deadline = start + bounded_timeout
                latest = live_first
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(WORKER_WAIT_POLL_S, remaining))
                    current = await _snapshot_worker(
                        resume_session,
                        effective_query,
                        effective_dir,
                        None,
                        include_output,
                        cap,
                    )
                    if (
                        current["state"] != live_state
                        or current["messageID"] != live_message
                        or current["total_chars"] != live_total
                    ):
                        merged = {
                            **current,
                            "taskID": taskID,
                            "directory": effective_dir,
                            "approval_state": APPROVAL_STATE_RESUMED,
                            "risky_action": stale_record.get("risky_action"),
                            "expires_at": stale_record.get("expires_at"),
                        }
                        return {
                            **merged,
                            "timed_out": False,
                            "changed": True,
                            "elapsed_s": round(time.monotonic() - start, 3),
                            "timeout_s": bounded_timeout,
                            "retryable": _retryable_for_state(
                                current["state"], False, current["stale"]
                            ),
                            "next_action": _next_action_for_state(current["state"], False),
                            "error_code": _error_code_for_snapshot(
                                current["state"], current["status"], current["messageID"]
                            ),
                            "evidence": _worker_evidence(
                                current["status"],
                                current["messageID"],
                                current["output_chars"],
                                current["total_chars"],
                            ),
                        }
                    latest = current
                merged = {
                    **latest,
                    "taskID": taskID,
                    "directory": effective_dir,
                    "approval_state": APPROVAL_STATE_RESUMED,
                    "risky_action": stale_record.get("risky_action"),
                    "expires_at": stale_record.get("expires_at"),
                }
                return {
                    **merged,
                    "timed_out": True,
                    "changed": False,
                    "elapsed_s": round(time.monotonic() - start, 3),
                    "timeout_s": bounded_timeout,
                    "retryable": _retryable_for_state(latest["state"], True, latest["stale"]),
                    "next_action": _next_action_for_state(latest["state"], True),
                    "error_code": _error_code_for_snapshot(
                        latest["state"], latest["status"], latest["messageID"]
                    ),
                    "evidence": _worker_evidence(
                        latest["status"],
                        latest["messageID"],
                        latest["output_chars"],
                        latest["total_chars"],
                    ),
                }
        first = await _snapshot_worker(
            taskID, effective_query, effective_dir, stale_record, include_output, cap
        )
        first_state = first["state"]
        first_message = first["messageID"]
        first_total = first["total_chars"]
        first_error = _error_code_for_snapshot(first_state, first["status"], first_message)
        if first_state != "running" or first_error is not None or first.get("stale"):
            return {
                **first,
                "timed_out": False,
                "changed": False,
                "elapsed_s": round(time.monotonic() - start, 3),
                "timeout_s": bounded_timeout,
                "retryable": _retryable_for_state(first_state, False, first["stale"]),
                "next_action": _next_action_for_state(first_state, False),
                "error_code": first_error,
                "evidence": _worker_evidence(
                    first["status"], first_message, first["output_chars"], first_total
                ),
                "approval_state": None,
                "risky_action": None,
                "expires_at": None,
            }
        deadline = start + bounded_timeout
        latest = first
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(WORKER_WAIT_POLL_S, remaining))
            current = await _snapshot_worker(
                taskID, effective_query, effective_dir, stale_record, include_output, cap
            )
            if (
                current["state"] != first_state
                or current["messageID"] != first_message
                or current["total_chars"] != first_total
            ):
                return {
                    **current,
                    "timed_out": False,
                    "changed": True,
                    "elapsed_s": round(time.monotonic() - start, 3),
                    "timeout_s": bounded_timeout,
                    "retryable": _retryable_for_state(current["state"], False, current["stale"]),
                    "next_action": _next_action_for_state(current["state"], False),
                    "error_code": _error_code_for_snapshot(
                        current["state"], current["status"], current["messageID"]
                    ),
                    "evidence": _worker_evidence(
                        current["status"],
                        current["messageID"],
                        current["output_chars"],
                        current["total_chars"],
                    ),
                    "approval_state": None,
                    "risky_action": None,
                    "expires_at": None,
                }
            latest = current
        return {
            **latest,
            "timed_out": True,
            "changed": False,
            "elapsed_s": round(time.monotonic() - start, 3),
            "timeout_s": bounded_timeout,
            "retryable": _retryable_for_state(latest["state"], True, latest["stale"]),
            "next_action": _next_action_for_state(latest["state"], True),
            "error_code": _error_code_for_snapshot(
                latest["state"], latest["status"], latest["messageID"]
            ),
            "evidence": _worker_evidence(
                latest["status"],
                latest["messageID"],
                latest["output_chars"],
                latest["total_chars"],
            ),
            "approval_state": None,
            "risky_action": None,
            "expires_at": None,
        }
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_wait",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


def _find_catalog_model_name(data: dict[str, Any], provider_id: str, model_id: str) -> str | None:
    """Return the catalog display name for a provider/model pair, if listed.

    Args:
        data: Raw providers payload with an "all" list.
        provider_id: Provider to match.
        model_id: Model to match.

    Returns:
        Catalog name when the pair is listed, else None.
    """
    for provider in data.get("all", []) or []:
        if not isinstance(provider, dict):
            continue
        if provider.get("id") != provider_id:
            continue
        entries = provider.get("models", {}) or {}
        if not isinstance(entries, dict):
            continue
        for model_key, spec in entries.items():
            detail = spec if isinstance(spec, dict) else {}
            candidate = detail.get("id") or model_key
            if candidate == model_id:
                name = detail.get("name")
                return name if isinstance(name, str) else None
    return None


def _is_model_listed(data: dict[str, Any], provider_id: str, model_id: str) -> bool:
    """Check whether a provider/model pair is listed in the catalog payload.

    Args:
        data: Raw providers payload with an "all" list.
        provider_id: Provider to match.
        model_id: Model to match.

    Returns:
        True when the pair is listed, regardless of its display name.
    """
    for provider in data.get("all", []) or []:
        if not isinstance(provider, dict):
            continue
        if provider.get("id") != provider_id:
            continue
        entries = provider.get("models", {}) or {}
        if not isinstance(entries, dict):
            continue
        for model_key, spec in entries.items():
            detail = spec if isinstance(spec, dict) else {}
            if (detail.get("id") or model_key) == model_id:
                return True
    return False


def _build_recommendations(
    data: dict[str, Any],
    connected: set[str],
    default_provider_id: str,
    default_model_id: str,
) -> list[dict[str, Any]]:
    """Build the ordered model recommendations (free first, paid second).

    The list is filter-independent so clients can discover the paid
    fallback even when `free_only`, `connected_only`, `query`, or `limit`
    hides it from `models`. It never changes the default: the bridge
    still auto-selects only the configured free default.

    Args:
        data: Raw providers payload with "connected" and "all".
        connected: Connected provider IDs.
        default_provider_id: Configured default provider.
        default_model_id: Configured default model.

    Returns:
        Two entries: rank 1 is the configured default (try first),
        rank 2 is the paid OpenCode Go fallback (explicit request only).
    """
    default_name = _find_catalog_model_name(data, default_provider_id, default_model_id)
    fallback_found = _find_catalog_model_name(
        data, FALLBACK_PAID_PROVIDER_ID, FALLBACK_PAID_MODEL_ID
    )
    return [
        {
            "rank": 1,
            "providerID": default_provider_id,
            "modelID": default_model_id,
            "name": default_name,
            "free": True,
            "connected": default_provider_id in connected,
            "available": _is_model_listed(data, default_provider_id, default_model_id),
            "requires_explicit_request": False,
            "reason": "default-free-first",
        },
        {
            "rank": 2,
            "providerID": FALLBACK_PAID_PROVIDER_ID,
            "modelID": FALLBACK_PAID_MODEL_ID,
            "name": fallback_found or FALLBACK_PAID_NAME,
            "free": False,
            "connected": FALLBACK_PAID_PROVIDER_ID in connected,
            "available": _is_model_listed(data, FALLBACK_PAID_PROVIDER_ID, FALLBACK_PAID_MODEL_ID),
            "requires_explicit_request": True,
            "reason": "paid-fallback-use-only-when-explicitly-requested",
        },
    ]


@mcp.tool(
    output_schema=WORKER_CATALOG_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_CATALOG_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def worker_catalog(
    query: str | None = None,
    free_only: bool = True,
    connected_only: bool = True,
    limit: int = WORKER_CATALOG_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List worker models with free/connected filters.

    Free means the model ID or name contains an explicit "free" marker
    (case-insensitive). Zero token-cost metadata alone never counts as
    free; cost metadata is preserved in entries but does not infer
    billing entitlement. The configured default provider/model sorts
    first when it survives filters, then provider/model order.
    `recommendations` is filter-independent and always lists rank 1
    (configured free default, try first) then rank 2 (paid OpenCode Go
    `opencode-go/muse-spark-1.3-contributor`, explicit request only).

    Args:
        query: Case-insensitive substring filter over provider and model
            IDs/names. Omit for no text filtering.
        free_only: Keep only free models.
        connected_only: Keep only connected providers.
        limit: Max entries (1-100).

    Returns:
        Compact dict with model entries, bridge defaults, total count,
        and ordered recommendations (free first, paid fallback second),
        plus the stable contract (timed_out=False, retryable, next_action,
        error_code, evidence).
    """
    _obs_start = time.perf_counter()
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_catalog",
        outcome=observability.OUTCOME_STARTED,
    )
    try:
        client = get_client()
        count = max(1, min(limit, WORKER_CATALOG_MAX_LIMIT))
        data = await client.get_providers_raw()
        connected = set(data.get("connected", []) or [])
        needle = query.lower() if query else None
        models: list[dict[str, Any]] = []
        for provider in data.get("all", []) or []:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            provider_name = provider.get("name")
            is_connected = provider_id in connected
            if connected_only and not is_connected:
                continue
            entries = provider.get("models", {}) or {}
            if not isinstance(entries, dict):
                continue
            for model_key, spec in entries.items():
                detail = spec if isinstance(spec, dict) else {}
                model_id = detail.get("id") or model_key
                name = detail.get("name")
                cost = detail.get("cost")
                free = _is_free_model(model_id, name, cost)
                if free_only and not free:
                    continue
                if (
                    needle
                    and needle
                    not in f"{provider_id or ''} {provider_name or ''} "
                    f"{model_id or ''} {name or ''}".lower()
                ):
                    continue
                entry: dict[str, Any] = {
                    "providerID": provider_id,
                    "modelID": model_id,
                    "name": name,
                    "connected": is_connected,
                    "free": free,
                }
                if (
                    isinstance(cost, dict)
                    and cost.get("input") is not None
                    and cost.get("output") is not None
                ):
                    entry["cost"] = {"input": cost["input"], "output": cost["output"]}
                models.append(entry)
        models.sort(
            key=lambda item: (
                not (
                    item["providerID"] == client.default_provider_id
                    and item["modelID"] == client.default_model_id
                ),
                item["providerID"] or "",
                item["modelID"] or "",
            )
        )
        _obs_result = {
            "models": models[:count],
            "default": {
                "providerID": client.default_provider_id,
                "modelID": client.default_model_id,
            },
            "total": len(models),
            "recommendations": _build_recommendations(
                data,
                connected,
                client.default_provider_id,
                client.default_model_id,
            ),
            "timed_out": False,
            "retryable": False,
            "next_action": "worker_run",
            "error_code": None,
            "evidence": {
                "status": None,
                "messageID": None,
                "output_chars": 0,
                "total_chars": len(models),
            },
        }
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_catalog",
            outcome=observability.OUTCOME_SUCCEEDED,
            duration_ms=observability.duration_ms_since(_obs_start),
        )
        return _obs_result
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_catalog",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def exec_run(
    command: str, workdir: str | None = None, timeout_s: int | None = None
) -> dict[str, Any]:
    """Run a raw shell command on the server. Full access, no sandbox.

    Opt-in only: disabled unless ENABLE_EXEC_RUN=true. The tool stays
    listed on /mcp for backward compatibility, but calls fail closed
    when disabled. Prefer opencode sessions for code changes (they track
    diffs). Use this for system ops: docker, systemctl, logs, networking,
    disk. /worker-mcp never exposes this tool; it is the recommended
    endpoint.

    Args:
        command: Shell command to run.
        workdir: Working directory. Defaults to the server default.
        timeout_s: Timeout in seconds. Defaults to server setting.

    Returns:
        Dict with exit_code, stdout, stderr (truncated), and workdir.

    Raises:
        RuntimeError: If ENABLE_EXEC_RUN is not explicitly enabled.
    """
    settings = get_settings()
    if not settings.enable_exec_run:
        raise RuntimeError(
            "exec_run is disabled: set ENABLE_EXEC_RUN=true to opt in. "
            "Prefer /worker-mcp session tools for code edits."
        )
    cwd = _authorize_optional_directory(workdir, what="workdir")
    timeout = timeout_s or settings.exec_timeout_s
    timeout = max(1, min(timeout, 600))
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": f"timed out after {timeout}s (process killed)",
                "workdir": cwd,
            }
        cap = settings.exec_max_output_chars
        return {
            "exit_code": process.returncode,
            "stdout": _truncate(stdout.decode(errors="replace"), cap),
            "stderr": _truncate(stderr.decode(errors="replace"), cap),
            "workdir": cwd,
        }
    except FileNotFoundError:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"workdir not found: {cwd}",
            "workdir": cwd,
        }
    except NotADirectoryError:
        return {"exit_code": 127, "stdout": "", "stderr": f"not a directory: {cwd}", "workdir": cwd}


@mcp.tool(
    output_schema=WORKER_VERIFY_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_VERIFY_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def worker_verify(
    taskID: str,
    directory: str | None = None,
    max_output_chars: int = WORKER_VERIFY_DEFAULT_CHARS,
) -> dict[str, Any]:
    """Verify a worker: bounded latest output plus a read-only git bundle.

    Never runs caller-provided commands. Git inspection uses only fixed
    arguments via asyncio.create_subprocess_exec, never a shell, each with
    a narrowly scoped ``-c safe.directory`` for the inspected directory.

    Args:
        taskID: Task ID from worker_run (the session ID).
        directory: Repository directory to verify. Defaults to server default.
        max_output_chars: Output cap, clamped to a bounded range.

    Returns:
        Compact dict with taskID, sessionID, state, status, bounded output
        counts, directory, and a verification bundle (git status --short,
        diff --stat, diff --check exit/output, changed files, latest commit
        evidence), plus the stable contract inherited from worker_status
        (timed_out, retryable, next_action, error_code, evidence).
        latest_commit is the directory HEAD for information only
        and is never attributed to the task. Handles missing directories
        and non-git paths cleanly.

    Raises:
        ValueError: If taskID is empty.
    """
    _obs_start = time.perf_counter()
    _obs_task = observability.safe_task_id(taskID)
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_verify",
        outcome=observability.OUTCOME_STARTED,
        task_id=_obs_task,
    )
    try:
        if not taskID or not taskID.strip():
            raise ValueError("taskID must not be empty")
        status_result = await worker_status(taskID, directory, True, max_output_chars)
        recovered_dir = status_result.get("directory")
        if directory is not None:
            effective_dir = _authorize_directory(directory)
        elif isinstance(recovered_dir, str) and recovered_dir.strip():
            effective_dir = _authorize_directory(recovered_dir)
        else:
            effective_dir = _authorize_optional_directory(None)
        verification = await _collect_verification(effective_dir)
        _obs_result = {
            **status_result,
            "directory": verification["directory"],
            "verification": verification,
        }
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_verify",
            outcome=observability.OUTCOME_SUCCEEDED,
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
        )
        return _obs_result
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_verify",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


@mcp.tool(
    output_schema=WORKER_CLEANUP_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@worker_mcp.tool(
    output_schema=WORKER_CLEANUP_OUTPUT_SCHEMA,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def worker_cleanup(
    taskID: str,
    directory: str | None = None,
    action: str = "delete",
) -> dict[str, Any]:
    """Clean up a worker with an explicit abort or delete action.

    All arguments are validated before any side effect runs. Delete is
    idempotent: when the session is already gone from OpenCode (HTTP 404
    on abort or delete), the task record is still removed and delete
    reports success with a generic warning, so stale workers clean up
    safely. Only the given taskID is ever touched; unrelated sessions
    are never listed or killed.

    Args:
        taskID: Task ID from worker_run (the session ID).
        directory: Working directory override.
        action: Either "abort" (stop the worker, keep the session) or
            "delete" (best-effort abort, then delete the session).

    Returns:
        Stable compact dict with taskID, sessionID, action, aborted,
        deleted, directory, and cleanup_warning, plus the stable contract
        (state, timed_out=False, retryable=False, next_action,
        error_code, evidence). aborted is True only when
        the pre-action abort actually succeeded; when the best-effort abort
        before delete fails, aborted is False and cleanup_warning carries a
        short generic note (no internal error details).

    Raises:
        ValueError: If taskID is empty or action is not abort/delete.
    """
    _obs_start = time.perf_counter()
    _obs_task = observability.safe_task_id(taskID)
    _obs_action_raw = (action or "").strip().lower()
    _obs_action = _obs_action_raw if _obs_action_raw in ("abort", "delete") else "invalid"
    observability.emit(
        event=observability.EVENT_WORKER,
        tool="worker_cleanup",
        outcome=observability.OUTCOME_STARTED,
        task_id=_obs_task,
        action=_obs_action,
    )
    try:
        if not taskID or not taskID.strip():
            raise ValueError("taskID must not be empty")
        normalized = (action or "").strip().lower()
        if normalized not in ("abort", "delete"):
            raise ValueError("action must be either 'abort' or 'delete'")
        client = get_client()
        saved_dir: str | None = None
        if directory is None:
            saved_dir = (_load_task_state().get(taskID) or {}).get("directory")
        if directory is not None:
            effective_dir = _authorize_directory(directory)
            query_dir: str | None = effective_dir
        elif isinstance(saved_dir, str) and saved_dir.strip():
            effective_dir = _authorize_directory(saved_dir)
            query_dir = effective_dir
        else:
            effective_dir = _authorize_optional_directory(None)
            query_dir = effective_dir
        stored_approval: dict[str, Any] | None = None
        candidate_record = _load_task_state().get(taskID)
        if isinstance(candidate_record, dict) and _is_approval_record(candidate_record):
            stored_approval = candidate_record
            stored_dir_raw = stored_approval.get("directory")
            if directory is not None:
                if not isinstance(stored_dir_raw, str) or not stored_dir_raw.strip():
                    raise ValueError("directory does not match the approval record")
                if _realpath_str(stored_dir_raw) != _realpath_str(effective_dir):
                    raise ValueError("directory does not match the approval record")
            approval_state_now = stored_approval.get("approval_state")
            if approval_state_now in (
                APPROVAL_STATE_REQUIRED,
                APPROVAL_STATE_APPROVED,
            ) and _is_approval_expired(stored_approval):
                approval_state_now = APPROVAL_STATE_EXPIRED
            resume_session = stored_approval.get("resume_sessionID")
            if not (isinstance(resume_session, str) and resume_session):
                if normalized == "abort":
                    _obs_result = {
                        "taskID": taskID,
                        "sessionID": None,
                        "action": "abort",
                        "aborted": False,
                        "deleted": False,
                        "directory": effective_dir,
                        "cleanup_warning": _bound_text(
                            "approval has not started; nothing to abort",
                            WORKER_CLEANUP_WARNING_MAX_CHARS,
                        ),
                        "state": approval_state_now,
                        "timed_out": False,
                        "retryable": False,
                        "next_action": "worker_status",
                        "error_code": None,
                        "evidence": {
                            "status": approval_state_now,
                            "messageID": None,
                            "output_chars": 0,
                            "total_chars": 0,
                        },
                    }
                    observability.emit(
                        event=observability.EVENT_WORKER,
                        tool="worker_cleanup",
                        outcome=observability.OUTCOME_SUCCEEDED,
                        duration_ms=observability.duration_ms_since(_obs_start),
                        task_id=_obs_task,
                        action="abort",
                    )
                    return _obs_result
                async with _locked_task_registry():
                    tasks = _load_task_state()
                    current = tasks.get(taskID)
                    if (
                        not isinstance(current, dict)
                        or not _is_approval_record(current)
                        or (
                            isinstance(current.get("resume_sessionID"), str)
                            and current.get("resume_sessionID")
                        )
                    ):
                        raise ValueError("approval already started; retry cleanup")
                    expired = _expire_approval_record(tasks, taskID)
                    if expired is not None:
                        _save_task_state(tasks)
                        current = expired
                    _remove_task_record(taskID)
                    _obs_result = {
                        "taskID": taskID,
                        "sessionID": None,
                        "action": "delete",
                        "aborted": False,
                        "deleted": True,
                        "directory": effective_dir,
                        "cleanup_warning": _bound_text(
                            "approval removed before start",
                            WORKER_CLEANUP_WARNING_MAX_CHARS,
                        ),
                        "state": current.get("approval_state"),
                        "timed_out": False,
                        "retryable": False,
                        "next_action": "worker_run",
                        "error_code": None,
                        "evidence": {
                            "status": current.get("approval_state"),
                            "messageID": None,
                            "output_chars": 0,
                            "total_chars": 0,
                        },
                    }
                    observability.emit(
                        event=observability.EVENT_WORKER,
                        tool="worker_cleanup",
                        outcome=observability.OUTCOME_SUCCEEDED,
                        duration_ms=observability.duration_ms_since(_obs_start),
                        task_id=_obs_task,
                        action="delete",
                    )
                    return _obs_result
            if normalized == "abort":
                await client.abort_session(resume_session, query_dir)
                _obs_result = {
                    "taskID": taskID,
                    "sessionID": resume_session,
                    "action": "abort",
                    "aborted": True,
                    "deleted": False,
                    "directory": effective_dir,
                    "cleanup_warning": None,
                    "state": "idle",
                    "timed_out": False,
                    "retryable": False,
                    "next_action": "worker_status",
                    "error_code": None,
                    "evidence": {
                        "status": "aborted",
                        "messageID": None,
                        "output_chars": 0,
                        "total_chars": 0,
                    },
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_cleanup",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    task_id=_obs_task,
                    action="abort",
                )
                return _obs_result
            async with _locked_task_registry():
                aborted_flag = True
                try:
                    await client.abort_session(resume_session, query_dir)
                except OpencodeError as exc:
                    if exc.status != 404:
                        aborted_flag = False
                    else:
                        aborted_flag = False
                except Exception:  # noqa: BLE001 - best-effort abort for resumed approvals
                    aborted_flag = False
                try:
                    await client.delete_session(resume_session, query_dir)
                except OpencodeError as exc:
                    if exc.status != 404:
                        raise
                _remove_task_record(taskID)
                _obs_result = {
                    "taskID": taskID,
                    "sessionID": resume_session,
                    "action": "delete",
                    "aborted": aborted_flag,
                    "deleted": True,
                    "directory": effective_dir,
                    "cleanup_warning": None
                    if aborted_flag
                    else _bound_text(
                        "pre-delete abort failed; session deleted",
                        WORKER_CLEANUP_WARNING_MAX_CHARS,
                    ),
                    "state": "idle",
                    "timed_out": False,
                    "retryable": False,
                    "next_action": "worker_status",
                    "error_code": None,
                    "evidence": {
                        "status": "deleted",
                        "messageID": None,
                        "output_chars": 0,
                        "total_chars": 0,
                    },
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_cleanup",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    task_id=_obs_task,
                    action="delete",
                )
                return _obs_result
        if normalized == "abort":
            await client.abort_session(taskID, query_dir)
            _obs_result = {
                "taskID": taskID,
                "sessionID": taskID,
                "action": "abort",
                "aborted": True,
                "deleted": False,
                "directory": effective_dir,
                "cleanup_warning": None,
                "state": "idle",
                "timed_out": False,
                "retryable": False,
                "next_action": "worker_status",
                "error_code": None,
                "evidence": {
                    "status": "aborted",
                    "messageID": None,
                    "output_chars": 0,
                    "total_chars": 0,
                },
            }
            observability.emit(
                event=observability.EVENT_WORKER,
                tool="worker_cleanup",
                outcome=observability.OUTCOME_SUCCEEDED,
                duration_ms=observability.duration_ms_since(_obs_start),
                task_id=_obs_task,
                action="abort",
            )
            return _obs_result
        async with _locked_task_registry():
            aborted_flag = True
            try:
                await client.abort_session(taskID, query_dir)
            except OpencodeError as exc:
                if exc.status == 404:
                    # Session already gone: still idempotent, keep going
                    # to delete (also 404-tolerant) and drop the record.
                    aborted_flag = False
                else:
                    try:
                        await client.delete_session(taskID, query_dir)
                    except OpencodeError as del_exc:
                        if del_exc.status != 404:
                            raise
                    _remove_task_record(taskID)
                    _obs_result = {
                        "taskID": taskID,
                        "sessionID": taskID,
                        "action": "delete",
                        "aborted": False,
                        "deleted": True,
                        "directory": effective_dir,
                        "cleanup_warning": _bound_text(
                            "pre-delete abort failed; session deleted",
                            WORKER_CLEANUP_WARNING_MAX_CHARS,
                        ),
                        "state": "unknown",
                        "timed_out": False,
                        "retryable": False,
                        "next_action": "worker_status",
                        "error_code": None,
                        "evidence": {
                            "status": "deleted",
                            "messageID": None,
                            "output_chars": 0,
                            "total_chars": 0,
                        },
                    }
                    observability.emit(
                        event=observability.EVENT_WORKER,
                        tool="worker_cleanup",
                        outcome=observability.OUTCOME_SUCCEEDED,
                        duration_ms=observability.duration_ms_since(_obs_start),
                        task_id=_obs_task,
                        action="delete",
                    )
                    return _obs_result
            except Exception:  # noqa: BLE001 - best-effort abort; outcome via aborted flag
                try:
                    await client.delete_session(taskID, query_dir)
                except OpencodeError as del_exc:
                    if del_exc.status != 404:
                        raise
                _remove_task_record(taskID)
                _obs_result = {
                    "taskID": taskID,
                    "sessionID": taskID,
                    "action": "delete",
                    "aborted": False,
                    "deleted": True,
                    "directory": effective_dir,
                    "cleanup_warning": _bound_text(
                        "pre-delete abort failed; session deleted",
                        WORKER_CLEANUP_WARNING_MAX_CHARS,
                    ),
                    "state": "unknown",
                    "timed_out": False,
                    "retryable": False,
                    "next_action": "worker_status",
                    "error_code": None,
                    "evidence": {
                        "status": "deleted",
                        "messageID": None,
                        "output_chars": 0,
                        "total_chars": 0,
                    },
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_cleanup",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    task_id=_obs_task,
                    action="delete",
                )
                return _obs_result
            if not aborted_flag:
                try:
                    await client.delete_session(taskID, query_dir)
                except OpencodeError as exc:
                    if exc.status != 404:
                        raise
                _remove_task_record(taskID)
                _obs_result = {
                    "taskID": taskID,
                    "sessionID": taskID,
                    "action": "delete",
                    "aborted": False,
                    "deleted": True,
                    "directory": effective_dir,
                    "cleanup_warning": _bound_text(
                        "session already gone; record removed",
                        WORKER_CLEANUP_WARNING_MAX_CHARS,
                    ),
                    "state": "unknown",
                    "timed_out": False,
                    "retryable": False,
                    "next_action": "worker_status",
                    "error_code": None,
                    "evidence": {
                        "status": "deleted",
                        "messageID": None,
                        "output_chars": 0,
                        "total_chars": 0,
                    },
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_cleanup",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    task_id=_obs_task,
                    action="delete",
                )
                return _obs_result
            try:
                await client.delete_session(taskID, query_dir)
            except OpencodeError as exc:
                if exc.status != 404:
                    raise
                _remove_task_record(taskID)
                _obs_result = {
                    "taskID": taskID,
                    "sessionID": taskID,
                    "action": "delete",
                    "aborted": True,
                    "deleted": True,
                    "directory": effective_dir,
                    "cleanup_warning": _bound_text(
                        "session already gone; record removed",
                        WORKER_CLEANUP_WARNING_MAX_CHARS,
                    ),
                    "state": "unknown",
                    "timed_out": False,
                    "retryable": False,
                    "next_action": "worker_status",
                    "error_code": None,
                    "evidence": {
                        "status": "deleted",
                        "messageID": None,
                        "output_chars": 0,
                        "total_chars": 0,
                    },
                }
                observability.emit(
                    event=observability.EVENT_WORKER,
                    tool="worker_cleanup",
                    outcome=observability.OUTCOME_SUCCEEDED,
                    duration_ms=observability.duration_ms_since(_obs_start),
                    task_id=_obs_task,
                    action="delete",
                )
                return _obs_result
            _remove_task_record(taskID)
            _obs_result = {
                "taskID": taskID,
                "sessionID": taskID,
                "action": "delete",
                "aborted": True,
                "deleted": True,
                "directory": effective_dir,
                "cleanup_warning": None,
                "state": "unknown",
                "timed_out": False,
                "retryable": False,
                "next_action": "worker_status",
                "error_code": None,
                "evidence": {
                    "status": "deleted",
                    "messageID": None,
                    "output_chars": 0,
                    "total_chars": 0,
                },
            }
            observability.emit(
                event=observability.EVENT_WORKER,
                tool="worker_cleanup",
                outcome=observability.OUTCOME_SUCCEEDED,
                duration_ms=observability.duration_ms_since(_obs_start),
                task_id=_obs_task,
                action="delete",
            )
            return _obs_result
    except Exception as _obs_exc:
        _obs_class, _obs_status = observability.classify_error(_obs_exc)
        observability.emit(
            event=observability.EVENT_WORKER,
            tool="worker_cleanup",
            outcome=observability.outcome_for(_obs_exc),
            duration_ms=observability.duration_ms_since(_obs_start),
            task_id=_obs_task,
            action=_obs_action,
            error_class=_obs_class,
            status_code=_obs_status,
        )
        raise


def _truncate(text: str, cap: int) -> str:
    """Truncate text with a marker.

    Args:
        text: Text to cap.
        cap: Max chars.

    Returns:
        Capped text.
    """
    if len(text) > cap:
        return text[:cap] + f"\n...[truncated {len(text) - cap} chars]"
    return text


class RequestBodyLimitMiddleware:
    """ASGI middleware rejecting oversized MCP request bodies early.

    Enforces MCP_MAX_BODY_BYTES on declared Content-Length and on the
    actual streamed body for /mcp and /worker-mcp before FastMCP/tool
    handling. Declared oversized lengths get an immediate generic 413;
    absent, malformed, negative, or under-limit declarations fall through
    to bounded streaming enforcement where http.request chunks are
    counted and bodies over the limit get the same generic 413. Rejected
    requests never reach downstream tools. All other paths (including
    /health) and non-HTTP scopes pass through untouched. Buffered memory
    stays bounded to the limit (plus one small control message): body
    bytes are coalesced into a single bytearray, so an unbounded number
    of empty or fragmented http.request messages cannot grow metadata
    without bound. Downstream sees one coalesced http.request message
    with identical bytes (chunk boundaries are not preserved). The
    413 body is generic and never echoes tokens, sizes, headers, or
    body bytes.
    """

    def __init__(self, app: Any, max_body_bytes: int) -> None:
        """Create the middleware.

        Args:
            app: Downstream ASGI app.
            max_body_bytes: Max request body in bytes (declared or streamed).

        Raises:
            RuntimeError: If the limit is not a positive integer.
        """
        if not isinstance(max_body_bytes, int) or max_body_bytes <= 0:
            raise RuntimeError("Body limit misconfigured: must be a positive integer")
        self.app = app
        self._max_body_bytes = max_body_bytes

    async def _reject(self, scope: Any, receive: Any, send: Any) -> None:
        """Send a generic 413 without echoing request or config values.

        Args:
            scope: ASGI scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
        """
        observability.emit(
            event=observability.EVENT_AUTH,
            tool=observability.TOOL_AUTH,
            outcome=observability.OUTCOME_REJECTED,
            error_class="PayloadTooLarge",
            status_code=413,
        )
        response = JSONResponse({"error": "payload too large"}, status_code=413)
        await response(scope, receive, send)

    @staticmethod
    def _chunk_bytes(message: Any) -> bytes:
        """Return the body bytes of an http.request chunk safely.

        Args:
            message: ASGI message.

        Returns:
            Body bytes, or b"" for missing/non-bytes bodies.
        """
        if not isinstance(message, dict):
            return b""
        body = message.get("body", b"")
        if isinstance(body, bytes):
            return body
        if isinstance(body, (bytearray, memoryview)):
            return bytes(body)
        return b""

    @staticmethod
    def _chunk_len(message: Any) -> int:
        """Return the byte length of an http.request chunk safely.

        Args:
            message: ASGI message.

        Returns:
            Length of the body bytes, or 0 for missing/non-bytes bodies.
        """
        return len(RequestBodyLimitMiddleware._chunk_bytes(message))

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Reject oversized declared and streamed bodies before downstream.

        Body bytes are coalesced into one bounded bytearray; downstream
        replays as a single http.request message with identical bytes.

        Args:
            scope: ASGI scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        normalized = (scope.get("path", "") or "").rstrip("/") or "/"
        if normalized not in ("/mcp", "/worker-mcp"):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw = headers.get(b"content-length")
        if raw is not None:
            try:
                declared = int(raw.decode().strip())
            except (ValueError, UnicodeDecodeError, AttributeError):
                declared = -1
            if declared > self._max_body_bytes:
                await self._reject(scope, receive, send)
                return
        buffered = bytearray()
        pending: dict | None = None
        complete = False
        while True:
            message = await receive()
            if not isinstance(message, dict) or message.get("type") != "http.request":
                pending = message if isinstance(message, dict) else {}
                break
            chunk = self._chunk_bytes(message)
            if chunk:
                if len(buffered) + len(chunk) > self._max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                buffered.extend(chunk)
            if not message.get("more_body", False):
                complete = True
                break
        body_snapshot = bytes(buffered)
        replayed = False
        pending_sent = pending is None

        async def _replay() -> dict:
            """Replay coalesced body, then pending control, then channel."""
            nonlocal replayed, pending_sent
            if not replayed:
                replayed = True
                if pending is not None and len(body_snapshot) > 0:
                    return {
                        "type": "http.request",
                        "body": body_snapshot,
                        "more_body": True,
                    }
                if complete or len(body_snapshot) > 0 or pending is None:
                    return {
                        "type": "http.request",
                        "body": body_snapshot,
                        "more_body": False,
                    }
                pending_sent = True
                return pending
            if not pending_sent and pending is not None:
                pending_sent = True
                return pending
            return await receive()

        await self.app(scope, _replay, send)


def _normalize_incoming_origin(value: str) -> str:
    """Normalize an incoming Origin value for exact comparison.

    Strips surrounding whitespace and a single trailing "/" (the same
    rule used for MCP_ALLOWED_ORIGINS entries). No other normalization
    is applied; matching stays exact and case-sensitive.

    Args:
        value: Raw Origin header value.

    Returns:
        Normalized origin string.
    """
    cleaned = value.strip()
    if cleaned.endswith("/") and len(cleaned) > 1:
        return cleaned[:-1]
    return cleaned


def _origin_from_referer(value: str) -> str | None:
    """Derive an origin from a Referer header value.

    Parses the Referer as a URL and returns "scheme://host[:port]"
    preserving the Referer hostport exactly. Returns None when the
    value is malformed (missing http/https scheme, missing host,
    embedded userinfo, bad port, or whitespace).

    Args:
        value: Raw Referer header value.

    Returns:
        Derived origin string, or None when malformed.
    """
    cleaned = value.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return None
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc or "@" in parsed.netloc:
        return None
    if not parsed.hostname:
        return None
    try:
        _ = parsed.port
    except ValueError:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


RESOURCE_DOCUMENTATION_URL = "https://github.com/ManuOtel/opencode-mcp-bridge"
WELL_KNOWN_PREFIX = "/.well-known/oauth-protected-resource"
MCP_RESOURCE_SUFFIXES = {"": "", "/mcp": "/mcp", "/worker-mcp": "/worker-mcp"}
SERVER_CARD_PATH = "/.well-known/mcp/server-card.json"
SERVER_CARD_FALLBACK_VERSION = "0.5.1"


def _public_base_url(scope: Any, headers: dict[bytes, bytes]) -> str:
    """Derive the public base URL from proxy headers or the request scope.

    Prefers X-Forwarded-Proto/Host (first value) behind a reverse proxy,
    then Host, then the ASGI server entry. No secret or token is echoed.

    Args:
        scope: ASGI scope.
        headers: Lowercased request headers.

    Returns:
        Base URL as scheme://host without a trailing slash.
    """
    forwarded_proto = headers.get(b"x-forwarded-proto", b"").decode("latin-1")
    forwarded_host = headers.get(b"x-forwarded-host", b"").decode("latin-1")
    scheme = (forwarded_proto.split(",")[0].strip() if forwarded_proto else "") or scope.get(
        "scheme", "https"
    )
    if scheme not in ("http", "https"):
        scheme = "https"
    raw_host = (forwarded_host.split(",")[0].strip() if forwarded_host else "") or (
        headers.get(b"host", b"").decode("latin-1").split(",")[0].strip()
    )
    host = raw_host or "localhost"
    return f"{scheme}://{host}"


def _metadata_url_for_scope(scope: Any, headers: dict[bytes, bytes], suffix: str) -> str:
    """Return the metadata URL for an MCP resource suffix.

    Args:
        scope: ASGI scope.
        headers: Lowercased request headers.
        suffix: Either "" (generic), "/mcp", or "/worker-mcp".

    Returns:
        Absolute metadata URL under the well-known prefix.
    """
    base = _public_base_url(scope, headers)
    return f"{base}{WELL_KNOWN_PREFIX}{suffix}"


def _protected_resource_payload(resource: str) -> dict[str, Any]:
    """Build a truthful RFC 9728 protected-resource metadata payload.

    The bridge uses a static Bearer token and operates no OAuth
    authorization server, so authorization_servers is intentionally
    omitted (optional per RFC 9728) rather than invented.

    Args:
        resource: Absolute protected-resource identifier.

    Returns:
        Minimal metadata dict with resource, bearer method, and docs.
    """
    return {
        "resource": resource,
        "bearer_methods_supported": ["header"],
        "resource_documentation": RESOURCE_DOCUMENTATION_URL,
    }


async def _protected_resource_handler(request: Request) -> Response:
    """Serve unauthenticated RFC 9728 metadata for the generic prefix.

    Args:
        request: Starlette request.

    Returns:
        JSON metadata describing the server root resource.
    """
    raw_headers = {k.lower(): v for k, v in request.scope.get("headers", [])}
    base = _public_base_url(request.scope, raw_headers)
    return JSONResponse(_protected_resource_payload(base + "/"))


async def _protected_resource_mcp_handler(request: Request) -> Response:
    """Serve unauthenticated RFC 9728 metadata for the /mcp resource.

    Args:
        request: Starlette request.

    Returns:
        JSON metadata with the absolute /mcp resource identifier.
    """
    raw_headers = {k.lower(): v for k, v in request.scope.get("headers", [])}
    base = _public_base_url(request.scope, raw_headers)
    return JSONResponse(_protected_resource_payload(base + "/mcp"))


async def _protected_resource_worker_handler(request: Request) -> Response:
    """Serve unauthenticated RFC 9728 metadata for the /worker-mcp resource.

    Args:
        request: Starlette request.

    Returns:
        JSON metadata with the absolute /worker-mcp resource identifier.
    """
    raw_headers = {k.lower(): v for k, v in request.scope.get("headers", [])}
    base = _public_base_url(request.scope, raw_headers)
    return JSONResponse(_protected_resource_payload(base + "/worker-mcp"))


def _is_protected_resource_path(normalized: str) -> bool:
    """Check whether a normalized path is an RFC 9728 metadata endpoint.

    Args:
        normalized: Path with trailing slash stripped (root stays "/").

    Returns:
        True for the prefix itself and any path under it.
    """
    return normalized == WELL_KNOWN_PREFIX or normalized.startswith(WELL_KNOWN_PREFIX + "/")


def protected_resource_routes() -> list[Route]:
    """Return unauthenticated GET routes for RFC 9728 discovery.

    Covers the generic prefix plus the path-inserted variants for /mcp
    and /worker-mcp, each with and without a trailing slash so scanners
    get JSON either way. Only GET/HEAD are served; auth still guards
    every other method via the middleware bypass rule.

    Returns:
        List of Starlette routes.
    """
    return [
        Route(WELL_KNOWN_PREFIX, _protected_resource_handler, methods=["GET", "HEAD"]),
        Route(WELL_KNOWN_PREFIX + "/", _protected_resource_handler, methods=["GET", "HEAD"]),
        Route(WELL_KNOWN_PREFIX + "/mcp", _protected_resource_mcp_handler, methods=["GET", "HEAD"]),
        Route(
            WELL_KNOWN_PREFIX + "/mcp/",
            _protected_resource_mcp_handler,
            methods=["GET", "HEAD"],
        ),
        Route(
            WELL_KNOWN_PREFIX + "/worker-mcp",
            _protected_resource_worker_handler,
            methods=["GET", "HEAD"],
        ),
        Route(
            WELL_KNOWN_PREFIX + "/worker-mcp/",
            _protected_resource_worker_handler,
            methods=["GET", "HEAD"],
        ),
    ]


def _server_card_version() -> str:
    """Return the bridge version for the static server card.

    Prefers the installed distribution version so the card tracks
    pyproject; falls back to the release constant when packaging
    metadata is unavailable (e.g. uninstalled checkout).

    Returns:
        Version string, never empty.
    """
    try:
        from importlib.metadata import version

        return version("opencode-mcp-bridge")
    except Exception:  # noqa: BLE001 - fallback keeps discovery working
        return SERVER_CARD_FALLBACK_VERSION


async def _server_card_payload() -> dict[str, Any]:
    """Build the static Smithery server-card payload from worker tools.

    Lists exactly the eight worker_* tools served on /worker-mcp with
    their live descriptions and JSON input schemas, so the card cannot
    drift from the real catalog. Never includes exec_run, tokens,
    credentials, or OAuth claims: auth is a static Bearer token and
    the bridge operates no authorization server.

    Returns:
        Server-card dict per Smithery static fallback (SEP-1649 shape).
    """
    tools = await worker_mcp.list_tools()
    entries = []
    for tool in sorted(tools, key=lambda item: item.name):
        if tool.name not in WORKER_TOOL_NAMES:
            continue
        schema = tool.parameters
        if not isinstance(schema, dict):
            schema = {"type": "object"}
        entries.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": schema,
            }
        )
    return {
        "serverInfo": {"name": "opencode-bridge-worker", "version": _server_card_version()},
        "authentication": {"required": True, "schemes": ["bearer"]},
        "tools": entries,
        "resources": [],
        "prompts": [],
    }


async def _server_card_handler(request: Request) -> Response:
    """Serve the unauthenticated static server card for blocked scans.

    Args:
        request: Starlette request (unused, no secrets read).

    Returns:
        JSON server-card describing the worker endpoint and its tools.
    """
    return JSONResponse(await _server_card_payload())


def _is_server_card_path(normalized: str) -> bool:
    """Check whether a normalized path is the static server-card endpoint.

    Args:
        normalized: Path with trailing slash stripped (root stays "/").

    Returns:
        True for the card path with or without a trailing slash.
    """
    return normalized == SERVER_CARD_PATH


def server_card_routes() -> list[Route]:
    """Return unauthenticated GET routes for the static server card.

    Covers the exact Smithery path plus a trailing-slash variant so
    scanners get JSON either way. Only GET/HEAD are served; auth still
    guards every other method via the middleware bypass rule.

    Returns:
        List of Starlette routes.
    """
    return [
        Route(SERVER_CARD_PATH, _server_card_handler, methods=["GET", "HEAD"]),
        Route(SERVER_CARD_PATH + "/", _server_card_handler, methods=["GET", "HEAD"]),
    ]


class BearerAuthMiddleware:
    """ASGI middleware requiring a static Bearer token, except health.

    Covers /mcp (full catalog), /worker-mcp (worker-only catalog),
    /ready (readiness), and /metrics (counters). Only GET/HEAD on
    normalized /health (/health/) bypass auth; every other method on
    health and every MCP/readiness/metrics route requires the token.
    GET/HEAD on /.well-known/oauth-protected-resource and its /mcp and
    /worker-mcp children also bypass auth (RFC 9728 discovery, no
    secrets). GET/HEAD on /.well-known/mcp/server-card.json also
    bypasses auth (static Smithery fallback, no secrets). Accepts one primary token plus an optional secondary
    rotation token; every candidate is compared with hmac.compare_digest
    (no early exit) and validation fails closed. Token values are never
    logged. Rejections on /mcp and /worker-mcp carry a Bearer
    WWW-Authenticate challenge with a resource_metadata pointer so
    OAuth-aware scanners get valid discovery metadata; the bridge
    operates no authorization server, so the metadata intentionally
    omits authorization_servers.

    When allowed_origins is non-empty, an additional browser-origin
    policy applies to /mcp and /worker-mcp only, after authentication:
    a present Origin must exactly match the allowlist; when Origin is
    absent, a present Referer must derive to an allowed origin, and
    malformed Referer values are rejected. Absent Origin and Referer
    stays allowed for CLI/SDK compatibility. /health and other paths
    never check origins.
    """

    def __init__(
        self,
        app: Any,
        token: str | list[str] | tuple[str, ...],
        extra_tokens: list[str] | tuple[str, ...] | None = None,
        allowed_origins: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Create the middleware.

        Args:
            app: Downstream ASGI app.
            token: Expected Bearer token, or the full accepted list for
                rotation (primary first). Kept as a single positional
                arg for backward compatibility.
            extra_tokens: Optional extra accepted tokens (e.g. secondary
                rotation token). Ignored when token is already a list.
            allowed_origins: Optional exact-origin allowlist for
                browser-facing MCP requests. Empty/None disables the
                origin policy entirely.
        """
        if isinstance(token, (list, tuple)):
            accepted = [t for t in token if isinstance(t, str) and t.strip()]
        else:
            accepted = [token] if isinstance(token, str) and token.strip() else []
            for candidate in extra_tokens or ():
                if isinstance(candidate, str) and candidate.strip():
                    accepted.append(candidate.strip())
        if not accepted:
            raise RuntimeError("Bearer auth misconfigured: no tokens available")
        self.app = app
        self._expected_tokens: tuple[bytes, ...] = tuple(t.encode() for t in accepted)
        self._allowed_origins: tuple[str, ...] = tuple(allowed_origins or ())
        self._allowed_origin_set: frozenset[str] = frozenset(self._allowed_origins)

    def _is_authorized(self, presented: bytes) -> bool:
        """Compare a presented token against all accepted tokens.

        Every candidate runs through hmac.compare_digest with no early
        exit, so accept/reject timing does not reveal which slot matched.

        Args:
            presented: Raw token bytes from the Authorization header.

        Returns:
            True when any accepted token matches.
        """
        if not presented:
            return False
        matched = False
        for expected in self._expected_tokens:
            if hmac.compare_digest(presented, expected):
                matched = True
        return matched

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Check auth for HTTP requests, pass through lifespan/websocket.

        Args:
            scope: ASGI scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        normalized = path.rstrip("/") or "/"
        if normalized == "/health" and scope.get("method") in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if _is_protected_resource_path(normalized) and scope.get("method") in (
            "GET",
            "HEAD",
        ):
            await self.app(scope, receive, send)
            return
        if _is_server_card_path(normalized) and scope.get("method") in (
            "GET",
            "HEAD",
        ):
            await self.app(scope, receive, send)
            return
        auth = headers.get(b"authorization", b"")
        scheme, _, presented = auth.partition(b" ")
        if scheme.lower() != b"bearer" or not self._is_authorized(presented):
            observability.emit(
                event=observability.EVENT_AUTH,
                tool=observability.TOOL_AUTH,
                outcome=observability.OUTCOME_REJECTED,
                error_class="Unauthorized",
            )
            challenge = 'Bearer error="unauthorized"'
            if normalized in ("/mcp", "/worker-mcp"):
                metadata_url = _metadata_url_for_scope(scope, headers, normalized)
                challenge = f'Bearer error="unauthorized", resource_metadata="{metadata_url}"'
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": challenge},
            )
            await response(scope, receive, send)
            return
        if normalized in ("/mcp", "/worker-mcp") and self._allowed_origin_set:
            origin_raw = headers.get(b"origin", b"").decode("latin-1").strip()
            if origin_raw:
                if _normalize_incoming_origin(origin_raw) not in self._allowed_origin_set:
                    observability.emit(
                        event=observability.EVENT_AUTH,
                        tool=observability.TOOL_AUTH,
                        outcome=observability.OUTCOME_REJECTED,
                        error_class="Forbidden",
                    )
                    response = JSONResponse({"error": "forbidden"}, status_code=403)
                    await response(scope, receive, send)
                    return
            else:
                referer_raw = headers.get(b"referer", b"").decode("latin-1").strip()
                if referer_raw:
                    derived = _origin_from_referer(referer_raw)
                    if derived is None or derived not in self._allowed_origin_set:
                        observability.emit(
                            event=observability.EVENT_AUTH,
                            tool=observability.TOOL_AUTH,
                            outcome=observability.OUTCOME_REJECTED,
                            error_class="Forbidden",
                        )
                        response = JSONResponse({"error": "forbidden"}, status_code=403)
                        await response(scope, receive, send)
                        return
        await self.app(scope, receive, send)


def create_app() -> Any:
    """Build the Starlette app: /mcp (full) + /worker-mcp (worker-only).

    Both MCP endpoints share the same Bearer token; GET /health stays open.
    GET /ready and GET /metrics need the same Bearer token. RFC 9728 protected-resource metadata under
    /.well-known/oauth-protected-resource also stays open (no secrets).
    The static Smithery server card under /.well-known/mcp/server-card.json
    also stays open (no secrets). Tool functions are registered once on two FastMCP servers, so there is
    no duplicated business logic. Lifespan enters both FastMCP session
    managers via the public Starlette lifespan protocol.

    Returns:
        ASGI app ready for uvicorn.
    """
    settings = get_settings()
    full_app = mcp.http_app(path="/mcp", stateless_http=True)
    worker_app = worker_mcp.http_app(path="/worker-mcp", stateless_http=True)

    # Merge routes without a generic (path, methods) dedupe: that would
    # silently drop same-path routes with different endpoints. Only the
    # intentionally shared /health, /ready, and /metrics routes (same
    # handler fn on both servers) are deduped; all other routes are
    # keyed by endpoint identity so collisions survive.
    seen: set[tuple[Any, ...]] = set()
    merged_routes: list[Any] = []
    for route in [*full_app.routes, *worker_app.routes]:
        path = getattr(route, "path", None)
        methods = tuple(sorted(getattr(route, "methods", None) or []))
        endpoint = getattr(route, "endpoint", None)
        if path in ("/health", "/health/", "/ready", "/ready/", "/metrics", "/metrics/"):
            key = ("shared-route", path.rstrip("/") or "/", methods)
        else:
            key = (path, methods, id(endpoint))
        if key in seen:
            continue
        seen.add(key)
        merged_routes.append(route)
    merged_routes.extend(protected_resource_routes())
    merged_routes.extend(server_card_routes())

    # Dedupe middleware by full identity (class + args + kwargs); class-only
    # dedupe would silently drop same-class middleware with different config.
    def _middleware_key(item: Any) -> tuple[Any, ...]:
        return (item.cls, repr(getattr(item, "args", ())), repr(getattr(item, "kwargs", {})))

    merged_middleware: list[Any] = list(full_app.user_middleware)
    known = {_middleware_key(m) for m in merged_middleware}
    for item in worker_app.user_middleware:
        key = _middleware_key(item)
        if key not in known:
            merged_middleware.append(item)
            known.add(key)

    @asynccontextmanager
    async def combined_lifespan(app: Starlette):  # type: ignore[no-untyped-def]
        """Enter both FastMCP lifespans so both session managers run."""
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(full_app.router.lifespan_context(full_app))
            await stack.enter_async_context(worker_app.router.lifespan_context(worker_app))
            yield

    outer = Starlette(
        routes=merged_routes,
        middleware=merged_middleware,
        lifespan=combined_lifespan,
    )
    limited = RequestBodyLimitMiddleware(outer, settings.mcp_max_body_bytes)
    return BearerAuthMiddleware(
        limited,
        accepted_bearer_tokens(settings),
        allowed_origins=settings.allowed_origins,
    )


def main() -> None:
    """Run the bridge with uvicorn."""
    settings = get_settings()
    uvicorn.run(create_app(), host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
