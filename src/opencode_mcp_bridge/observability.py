"""Production-safe structured observability for worker requests.

Single-line JSON logs for systemd/docker. Never emits bearer tokens,
Authorization headers, prompts/messages, environment values, exception
text, or directory paths. Only safe fields: event, tool, outcome,
duration, redacted request/task identifiers, action enum, error class,
and numeric status codes. Every string field passes through the central
redactor before emission.

In-memory counters mirror the same bounded (event, tool, outcome)
triples for authenticated GET /metrics. The tool allowlist covers the
full existing tool catalog (all worker_* tools including approval
operations worker_decide/worker_resume, plus legacy compatibility
tools and exec_run) and infra subsystems (mcp_auth, readiness,
liveness, metrics). Events are worker.request, mcp.auth,
bridge.readiness, bridge.liveness, and bridge.metrics; approval
operations are tools under worker.request, not a separate event.
No external telemetry, no network calls, no raw identifiers, paths,
prompts, tokens, or exception details.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

SERVICE_NAME = "opencode-mcp-bridge"
EVENT_WORKER = "worker.request"
EVENT_AUTH = "mcp.auth"
EVENT_READINESS = "bridge.readiness"
EVENT_LIVENESS = "bridge.liveness"
EVENT_METRICS = "bridge.metrics"
TOOL_AUTH = "mcp_auth"
TOOL_READINESS = "readiness"
TOOL_LIVENESS = "liveness"
TOOL_METRICS = "metrics"

OUTCOME_STARTED = "started"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_REJECTED = "rejected"

TASK_ID_MAX_CHARS = 128

REDACTED = "[REDACTED]"
REDACT_MAX_STRING_CHARS = 2000
REDACT_MAX_ITEMS = 50
REDACT_MAX_DEPTH = 4

_RE_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;}\]]+")
_RE_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(MCP_BEARER_TOKEN(?:_SECONDARY)?|OPENCODE_SERVER_PASSWORD"
    r"|PASSWORD|PASSWD|PWD|SECRET|TOKEN|API[_-]?KEY|API[_-]?SECRET"
    r"|AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|BEARER|AUTHORIZATION|X-API-KEY"
    r"|CLIENT[_-]?SECRET)\b\s*([:=]|=>)\s*"
    r"(\"[^\"]*\"|'[^']*'|[^\s\",;}\]]+)"
)


def redact_text(value: str) -> str:
    """Scrub one string of bearer/password/API-key/secret assignments.

    Conservative and deterministic: only the two patterns above are
    replaced with [REDACTED]; everything else passes through unchanged
    (then truncated to a fixed bound). Safe allowlist values such as
    event, tool, outcome, and hashed request handles never match.

    Args:
        value: Raw string that may contain a secret pattern.

    Returns:
        Scrubbed string with secret values replaced by [REDACTED].
    """
    scrubbed = _RE_BEARER.sub("Bearer " + REDACTED, value)
    scrubbed = _RE_SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", scrubbed)
    return scrubbed[:REDACT_MAX_STRING_CHARS]


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Scrub strings and bounded nested mappings/sequences.

    Numbers, booleans, and None pass through untouched so safe numeric
    fields (duration, status) are preserved exactly. Strings go through
    redact_text. Mappings and sequences recurse with fixed depth, item,
    and string bounds so no generic unbounded serializer is created and
    no caller-controlled value can become a metric label (metrics never
    take identifiers or free-form values).

    Args:
        value: String, number, mapping, sequence, or None.
        _depth: Current recursion depth (internal, bounded).

    Returns:
        Scrubbed value with the same scalar/container shape.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if _depth >= REDACT_MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= REDACT_MAX_ITEMS:
                break
            safe_key = redact_text(key) if isinstance(key, str) else key
            if isinstance(key, str) and _RE_SECRET_ASSIGNMENT.match(f"{key}=x"):
                cleaned[safe_key] = REDACTED
            else:
                cleaned[safe_key] = redact_value(item, _depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        items = [redact_value(item, _depth + 1) for item in value[:REDACT_MAX_ITEMS]]
        return items if isinstance(value, list) else tuple(items)
    return redact_text(str(value)[:REDACT_MAX_STRING_CHARS])


METRIC_EVENTS = frozenset(
    {EVENT_WORKER, EVENT_AUTH, EVENT_READINESS, EVENT_LIVENESS, EVENT_METRICS}
)
METRIC_OUTCOMES = frozenset({OUTCOME_STARTED, OUTCOME_SUCCEEDED, OUTCOME_FAILED, OUTCOME_REJECTED})
METRIC_TOOLS = frozenset(
    {
        "worker_run",
        "worker_status",
        "worker_wait",
        "worker_verify",
        "worker_cleanup",
        "worker_catalog",
        "worker_decide",
        "worker_resume",
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
        "exec_run",
        TOOL_AUTH,
        TOOL_READINESS,
        TOOL_LIVENESS,
        TOOL_METRICS,
    }
)

_metrics_lock = threading.Lock()
_metrics_counters: dict[str, int] = {}

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
    The central redactor still scrubs secret patterns as defense in
    depth, so a mistakenly passed token never lands in logs verbatim.

    Args:
        value: Raw taskID input.

    Returns:
        Bounded scrubbed string or None when missing/blank.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    cleaned = text.strip()
    if not cleaned:
        return None
    return redact_text(cleaned[:TASK_ID_MAX_CHARS])


def classify_error(exc: BaseException) -> tuple[str, int | None]:
    """Map an exception to a stable class plus optional numeric status.

    Uses only the exception type name and numeric status attributes.
    Never inspects or returns exception messages, snippets, paths,
    URLs, or credentials. The class name passes through the central
    redactor as defense in depth (type names never match secret
    patterns, so they are preserved exactly).

    Args:
        exc: Raised exception.

    Returns:
        Tuple of (error_class, status_code or None).
    """
    status: int | None = getattr(exc, "status", None)
    if not isinstance(status, int):
        status = None
    return (redact_text(type(exc).__name__), status)


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


def _metric_key(event: str, tool: str, outcome: str) -> str | None:
    """Return the bounded counter key, or None for out-of-allowlist input.

    Only exact allowlist members count, so cardinality stays bounded and
    no caller-controlled string ever becomes a metric label.

    Args:
        event: Stable event namespace.
        tool: Tool or subsystem name.
        outcome: Stable outcome.

    Returns:
        Key like event|tool|outcome, or None when not allowlisted.
    """
    if event not in METRIC_EVENTS:
        return None
    if tool not in METRIC_TOOLS:
        return None
    if outcome not in METRIC_OUTCOMES:
        return None
    return f"{event}|{tool}|{outcome}"


def record(*, event: str, tool: str, outcome: str) -> None:
    """Increment one bounded in-memory counter.

    Unknown (event, tool, outcome) triples are ignored so cardinality
    stays fixed. Never takes identifiers, paths, prompts, tokens, or
    exception details, so nothing sensitive can enter the counters.

    Args:
        event: Stable event namespace (allowlisted only).
        tool: Tool or subsystem name (allowlisted only).
        outcome: Stable outcome (allowlisted only).
    """
    key = _metric_key(event, tool, outcome)
    if key is None:
        return
    with _metrics_lock:
        _metrics_counters[key] = _metrics_counters.get(key, 0) + 1


def snapshot() -> dict[str, int]:
    """Return a copy of the bounded in-memory counters.

    Returns:
        Map of event|tool|outcome keys to counts. Keys are always
        allowlisted; values are plain ints. Never contains raw
        identifiers, paths, prompts, tokens, or exception details.
    """
    with _metrics_lock:
        return dict(_metrics_counters)


def reset_metrics() -> None:
    """Clear all in-memory counters (tests only)."""
    with _metrics_lock:
        _metrics_counters.clear()


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

    Every string field passes through the central redactor first, so
    bearer tokens, Authorization values, passwords, API-key-like
    values, and known secret env assignments can never appear in the
    structured log or error fields verbatim. Numeric fields pass
    through untouched; metrics use the pre-redaction allowlist triple
    so no caller-controlled value becomes a label.

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
    record(event=event, tool=tool, outcome=outcome)
    payload: dict[str, Any] = {
        "service": SERVICE_NAME,
        "event": redact_value(event),
        "tool": redact_value(tool),
        "outcome": redact_value(outcome),
        "duration_ms": duration_ms,
        "request_id": redact_value(request_id) if request_id is not None else None,
        "task_id": redact_value(task_id) if task_id is not None else None,
        "action": redact_value(action) if action is not None else None,
        "error_class": redact_value(error_class) if error_class is not None else None,
        "status_code": status_code,
    }
    try:
        logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception:  # noqa: BLE001, S110 - observability must never break tools
        pass
