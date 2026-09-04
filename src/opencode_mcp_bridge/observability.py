"""Production-safe structured observability for worker requests.

Single-line JSON logs for systemd/docker. Never emits bearer tokens,
Authorization headers, prompts/messages, environment values, exception
text, or directory paths. Only safe fields: event, tool, outcome,
duration, redacted request/task identifiers, action enum, error class,
and numeric status codes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

SERVICE_NAME = "opencode-mcp-bridge"
EVENT_WORKER = "worker.request"
EVENT_AUTH = "mcp.auth"
TOOL_AUTH = "mcp_auth"

OUTCOME_STARTED = "started"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_REJECTED = "rejected"

TASK_ID_MAX_CHARS = 128

logger = logging.getLogger("opencode_mcp_bridge.observability")
logger.setLevel(logging.INFO)


def redact_request_id(value: Any) -> str | None:
    """Redact an idempotency key to a correlation-safe hash.

    Never returns the raw value. Returns None for missing/blank input
    so callers can log start events before validation without leaking.

    Args:
        value: Raw requestID input (any type, never trusted).

    Returns:
        Stable redacted handle like sha256:<12hex>:len=<n>, or None.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    cleaned = text.strip()
    if not cleaned:
        return None
    digest = hashlib.sha256(cleaned.encode()).hexdigest()[:16]
    return f"sha256:{digest}:len={len(cleaned)}"


def safe_task_id(value: Any) -> str | None:
    """Bound a task/session identifier for logging.

    Task IDs are operator correlation IDs (ses_*), not secrets. Bound
    length only; never include paths, prompts, or exception text here.

    Args:
        value: Raw taskID input.

    Returns:
        Bounded string or None when missing/blank.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    cleaned = text.strip()
    if not cleaned:
        return None
    return cleaned[:TASK_ID_MAX_CHARS]


def classify_error(exc: BaseException) -> tuple[str, int | None]:
    """Map an exception to a stable class plus optional numeric status.

    Uses only the exception type name and numeric status attributes.
    Never inspects or returns exception messages, snippets, paths,
    URLs, or credentials.

    Args:
        exc: Raised exception.

    Returns:
        Tuple of (error_class, status_code or None).
    """
    status: int | None = getattr(exc, "status", None)
    if not isinstance(status, int):
        status = None
    return (type(exc).__name__, status)


def outcome_for(exc: BaseException) -> str:
    """Map an exception to a stable terminal outcome.

    Validation failures are rejections (caller can fix input); all
    other errors are failures (backend/registry/internal).

    Args:
        exc: Raised exception.

    Returns:
        Either rejected or failed.
    """
    if isinstance(exc, ValueError):
        return OUTCOME_REJECTED
    return OUTCOME_FAILED


def duration_ms_since(start: float) -> float:
    """Return elapsed milliseconds since a perf_counter timestamp.

    Args:
        start: Start value from time.perf_counter().

    Returns:
        Elapsed milliseconds rounded to 3 decimals, never negative.
    """
    return round(max(0.0, time.perf_counter() - start) * 1000.0, 3)


def emit(
    *,
    event: str,
    tool: str,
    outcome: str,
    duration_ms: float | None = None,
    request_id: str | None = None,
    task_id: str | None = None,
    action: str | None = None,
    error_class: str | None = None,
    status_code: int | None = None,
) -> None:
    """Emit one JSON line with only allowlisted safe fields.

    Args:
        event: Stable event namespace (worker.request, mcp.auth).
        tool: Tool or subsystem name.
        outcome: started, succeeded, failed, or rejected.
        duration_ms: Elapsed ms for terminal events, None for start.
        request_id: Already-redacted request handle or None.
        task_id: Bounded task/session ID or None.
        action: Fixed action enum (abort/delete) or None.
        error_class: Exception type name or None.
        status_code: Numeric backend status or None.
    """
    payload: dict[str, Any] = {
        "service": SERVICE_NAME,
        "event": event,
        "tool": tool,
        "outcome": outcome,
        "duration_ms": duration_ms,
        "request_id": request_id,
        "task_id": task_id,
        "action": action,
        "error_class": error_class,
        "status_code": status_code,
    }
    try:
        logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception:  # noqa: BLE001, S110 - observability must never break tools
        pass
