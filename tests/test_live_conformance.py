"""Opt-in live endpoint conformance harness. Skips unless configured.

No network unless explicit live opt-in env vars are present, so normal
CI stays hermetic. Live tests require an unmistakable opt-in on every
run; generic MCP_URL/MCP_BEARER_TOKEN values never enable them.
Configure a target endpoint per run (local first, then deployed):

  OPENCODE_MCP_LIVE_ENABLE=1 \\
  OPENCODE_MCP_LIVE_WORKER_URL=http://127.0.0.1:8087/worker-mcp \\
  OPENCODE_MCP_LIVE_BEARER_TOKEN=<token> \\
    uv run pytest tests/test_live_conformance.py -v

Deployed:

  OPENCODE_MCP_LIVE_ENABLE=1 \\
  OPENCODE_MCP_LIVE_WORKER_URL=https://<your-domain>/worker-mcp \\
  OPENCODE_MCP_LIVE_BEARER_TOKEN=<token> \\
    uv run pytest tests/test_live_conformance.py -v

Optional env:

  OPENCODE_MCP_LIVE_FULL_URL   full catalog endpoint (default: sibling
                               /mcp derived from the worker URL).
  OPENCODE_MCP_LIVE_HEALTH_URL explicit health URL for the gate
                               (default: sibling /health derived from
                               the worker URL root).
  OPENCODE_MCP_LIVE_DIRECTORY  server-side directory fallback for the
                               one disposable worker run (default: omit
                               and let the bridge use its default;
                               cleanup prefers the server-returned
                               canonical task directory).
  OPENCODE_MCP_LIVE_WAIT_S     worker_wait timeout for the live run
                               (default 10, clamped 1-30).

Coverage per run: initialize handshake, tools/list on the worker
endpoint (exactly eight tools, no exec_run) and separately on the full
endpoint (separation check), worker_catalog model discovery
(free-first), one disposable free-worker run with duplicate requestID
behavior, worker_status snapshot, bounded worker_wait, worker_verify
evidence, worker_cleanup, plus error paths (401 without token, unknown
tool, missing task, conflicting requestID reuse). Secrets are never
printed: tokens stay in headers only and never enter assert messages,
fixture reprs, or captured output.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import pytest

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a runtime dependency
    httpx = None  # type: ignore[assignment]

EXPECTED_WORKER_TOOLS = [
    "worker_catalog",
    "worker_cleanup",
    "worker_decide",
    "worker_resume",
    "worker_run",
    "worker_status",
    "worker_verify",
    "worker_wait",
]

ALLOWED_STATES = {"running", "idle", "error", "unknown", "stale"}

DEFAULT_TIMEOUT_S = 30.0


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class LiveConfig(dict):  # type: ignore[type-arg]
    """Live config dict with a redacted repr so pytest never prints tokens."""

    def __repr__(self) -> str:
        redacted = {k: ("[redacted]" if k == "token" else v) for k, v in self.items()}
        return f"LiveConfig({redacted!r})"

    __str__ = __repr__


def _live_enabled() -> bool:
    """Unmistakable opt-in: live tests run only with ENABLE=1."""
    return _env("OPENCODE_MCP_LIVE_ENABLE") == "1"


def _live_worker_url() -> str:
    """Only the explicit live worker URL; generic MCP_URL never enables live."""
    return _env("OPENCODE_MCP_LIVE_WORKER_URL")


def _live_full_url(worker_url: str) -> str:
    explicit = _env("OPENCODE_MCP_LIVE_FULL_URL")
    if explicit:
        return explicit
    if worker_url.rstrip("/").endswith("/worker-mcp"):
        return worker_url.rstrip("/")[: -len("/worker-mcp")] + "/mcp"
    return ""


def _live_health_url(worker_url: str) -> str:
    """Explicit health URL, else sibling /health derived from worker root."""
    explicit = _env("OPENCODE_MCP_LIVE_HEALTH_URL")
    if explicit:
        return explicit
    trimmed = worker_url.rstrip("/")
    if "/" in trimmed:
        return trimmed.rsplit("/", 1)[0] + "/health"
    return trimmed + "/health"


def _live_token() -> str:
    """Only the explicit live bearer token; generic tokens never enable live."""
    return _env("OPENCODE_MCP_LIVE_BEARER_TOKEN")


def _live_config() -> dict[str, Any] | None:
    """Return live config or None when explicit opt-in env is absent."""
    if not _live_enabled():
        return None
    worker_url = _live_worker_url()
    token = _live_token()
    if not worker_url or not token:
        return None
    wait_raw = _env("OPENCODE_MCP_LIVE_WAIT_S")
    try:
        wait_s = int(wait_raw) if wait_raw else 10
    except ValueError:
        wait_s = 10
    wait_s = max(1, min(30, wait_s))
    return LiveConfig(
        {
            "worker_url": worker_url,
            "full_url": _live_full_url(worker_url),
            "health_url": _live_health_url(worker_url),
            "token": token,
            "directory": _env("OPENCODE_MCP_LIVE_DIRECTORY"),
            "wait_s": wait_s,
        }
    )


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    """Skip cleanly when explicit live opt-in configuration is absent."""
    if httpx is None:
        pytest.skip("httpx is not installed")
    config = _live_config()
    if config is None:
        pytest.skip(
            "live conformance skipped: set OPENCODE_MCP_LIVE_ENABLE=1, "
            "OPENCODE_MCP_LIVE_WORKER_URL, and OPENCODE_MCP_LIVE_BEARER_TOKEN"
        )
    return config


def _headers(token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }


def _payload_from_text(text: str) -> dict[str, Any]:
    """Parse a Streamable HTTP body as JSON or SSE data payload."""
    stripped = text.strip()
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        assert isinstance(parsed, dict)
        return parsed
    for line in text.splitlines():
        if line.startswith("data: "):
            parsed = json.loads(line[len("data: ") :])
            assert isinstance(parsed, dict)
            return parsed
    raise AssertionError("no JSON or SSE data payload in response")


def _rpc(
    url: str,
    token: str,
    method: str,
    params: dict[str, Any],
    rpc_id: int = 1,
) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
        response = client.post(url, json=body, headers=_headers(token))
    assert response.status_code == 200, f"{method} HTTP {response.status_code}"
    payload = _payload_from_text(response.text)
    assert payload.get("error") is None, "unexpected JSON-RPC error"
    return payload


def _call_tool(
    url: str, token: str, name: str, arguments: dict[str, Any], rpc_id: int = 1
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """Call one tool; return (is_error, structured_result, raw_payload)."""
    payload = _rpc(
        url,
        token,
        "tools/call",
        {"name": name, "arguments": arguments},
        rpc_id=rpc_id,
    )
    result = payload.get("result") or {}
    is_error = result.get("isError") is True
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return is_error, structured, payload
    content = result.get("content") or []
    if content and isinstance(content[0], dict):
        text = content[0].get("text") or "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            return is_error, parsed, payload
    return is_error, {}, payload


def _assert_no_secret(payload: dict[str, Any], token: str) -> None:
    """Fail without ever including the raw token value in the message."""
    serialized = json.dumps(payload)
    if token and token in serialized:
        raise AssertionError("response unexpectedly echoes credentials")


def test_live_initialize_handshake(live: dict[str, Any]) -> None:
    """The live worker endpoint completes the initialize handshake."""
    payload = _rpc(
        live["worker_url"],
        live["token"],
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "live-conformance", "version": "0"},
        },
    )
    result = payload["result"]
    assert isinstance(result.get("protocolVersion"), str)
    assert result["protocolVersion"]
    assert "tools" in result.get("capabilities", {})
    assert result.get("serverInfo", {}).get("name") == "opencode-bridge-worker"
    _assert_no_secret(payload, live["token"])


def test_live_worker_tools_list_exact(live: dict[str, Any]) -> None:
    """Worker endpoint lists exactly the eight worker tools, never exec_run."""
    payload = _rpc(live["worker_url"], live["token"], "tools/list", {})
    tools = payload["result"]["tools"]
    names = sorted(tool["name"] for tool in tools)
    assert names == EXPECTED_WORKER_TOOLS
    assert "exec_run" not in names
    _assert_no_secret(payload, live["token"])


def test_live_full_endpoint_stays_separate(live: dict[str, Any]) -> None:
    """Full endpoint keeps the wider catalog; worker endpoint stays narrow."""
    if not live["full_url"]:
        pytest.skip("OPENCODE_MCP_LIVE_FULL_URL is not configured or derivable")
    payload = _rpc(live["full_url"], live["token"], "tools/list", {})
    names = sorted(tool["name"] for tool in payload["result"]["tools"])
    assert set(EXPECTED_WORKER_TOOLS) <= set(names)
    assert "exec_run" in names
    assert len(names) > len(EXPECTED_WORKER_TOOLS)
    _assert_no_secret(payload, live["token"])


def test_live_model_discovery_free_first(live: dict[str, Any]) -> None:
    """worker_catalog reports a free-first default plus paid fallback rank."""
    is_error, result, payload = _call_tool(live["worker_url"], live["token"], "worker_catalog", {})
    assert is_error is False
    assert isinstance(result.get("models"), list)
    default = result.get("default") or {}
    assert default.get("providerID")
    assert default.get("modelID")
    recs = result.get("recommendations") or []
    assert len(recs) >= 1
    assert recs[0].get("rank") == 1
    assert recs[0].get("requires_explicit_request") is False
    _assert_no_secret(payload, live["token"])


def test_live_unauthenticated_is_rejected(live: dict[str, Any]) -> None:
    """Missing bearer token fails closed with 401 and no tool data."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
        response = client.post(live["worker_url"], json=body, headers=headers)
    assert response.status_code == 401


def test_live_tool_errors_fail_closed(live: dict[str, Any]) -> None:
    """Unknown tools and missing args fail as errors without leaking."""
    is_error, _, _ = _call_tool(live["worker_url"], live["token"], "no_such_tool", {}, rpc_id=11)
    assert is_error is True
    is_error, _, _ = _call_tool(live["worker_url"], live["token"], "worker_status", {}, rpc_id=12)
    assert is_error is True
    is_error, result, _ = _call_tool(
        live["worker_url"],
        live["token"],
        "worker_status",
        {"taskID": "live-missing-task-000"},
        rpc_id=13,
    )
    assert is_error is False
    assert result.get("state") == "unknown"


def test_live_disposable_worker_lifecycle(live: dict[str, Any]) -> None:
    """One disposable free-worker run: run, dedup, status, wait, verify."""
    is_error, catalog, _ = _call_tool(
        live["worker_url"], live["token"], "worker_catalog", {}, rpc_id=21
    )
    assert is_error is False
    default = catalog.get("default") or {}
    provider = default.get("providerID")
    model = default.get("modelID")
    assert provider and model

    nonce = uuid.uuid4().hex[:12]
    request_id = f"live-{nonce}"
    message = f"Live conformance probe {nonce}: reply with OK and nothing else."
    args: dict[str, Any] = {
        "message": message,
        "title": f"live-conformance-{nonce}",
        "providerID": provider,
        "modelID": model,
        "requestID": request_id,
    }
    if live["directory"]:
        args["directory"] = live["directory"]

    task_id = ""
    task_dir = ""
    try:
        started_at = time.monotonic()
        is_error, first, _ = _call_tool(
            live["worker_url"], live["token"], "worker_run", args, rpc_id=22
        )
        assert is_error is False, "worker_run must start the disposable task"
        task_id = str(first.get("taskID") or "")
        assert task_id
        assert first.get("deduplicated") is False
        task_dir = first.get("directory") or ""
        assert isinstance(task_dir, str)

        # Duplicate request: same ID plus same inputs returns the same task.
        is_error, dup, _ = _call_tool(
            live["worker_url"], live["token"], "worker_run", args, rpc_id=23
        )
        assert is_error is False
        assert dup.get("taskID") == task_id
        assert dup.get("deduplicated") is True

        # Conflicting reuse: same ID with different inputs fails before effects.
        conflict = dict(args)
        conflict["message"] = f"Conflicting probe {nonce}: must be rejected."
        is_error, _, _ = _call_tool(
            live["worker_url"], live["token"], "worker_run", conflict, rpc_id=24
        )
        assert is_error is True

        status_args: dict[str, Any] = {"taskID": task_id}
        if task_dir:
            status_args["directory"] = task_dir
        is_error, status, _ = _call_tool(
            live["worker_url"], live["token"], "worker_status", status_args, rpc_id=25
        )
        assert is_error is False
        assert status.get("taskID") == task_id
        assert status.get("state") in ALLOWED_STATES

        wait_args = dict(status_args)
        wait_args["timeout_s"] = live["wait_s"]
        wait_args["include_output"] = False
        is_error, waited, _ = _call_tool(
            live["worker_url"], live["token"], "worker_wait", wait_args, rpc_id=26
        )
        assert is_error is False
        assert waited.get("taskID") == task_id
        assert waited.get("state") in ALLOWED_STATES
        assert isinstance(waited.get("timed_out"), bool)
        assert time.monotonic() - started_at < 120

        is_error, verified, _ = _call_tool(
            live["worker_url"], live["token"], "worker_verify", status_args, rpc_id=27
        )
        assert is_error is False
        assert "verification" in verified
    finally:
        if task_id:
            cleanup_args: dict[str, Any] = {"taskID": task_id, "action": "delete"}
            # Prefer the server-returned canonical task directory; fall back
            # to the configured directory only when the server omitted one.
            cleanup_dir = task_dir or live["directory"]
            if cleanup_dir:
                cleanup_args["directory"] = cleanup_dir
            is_error, cleaned, _ = _call_tool(
                live["worker_url"],
                live["token"],
                "worker_cleanup",
                cleanup_args,
                rpc_id=28,
            )
            assert is_error is False
            assert cleaned.get("taskID") == task_id
