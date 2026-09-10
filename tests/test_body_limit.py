"""Request-body size protection tests. No network to opencode.

Covers MCP_MAX_BODY_BYTES default/override, enforcement on /mcp and
/worker-mcp via declared Content-Length, under-limit success, oversized
413 without tool invocation, malformed/absent Content-Length passthrough,
health/auth preservation, clients omitting Origin, and no secret leakage.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import config, server
from opencode_mcp_bridge.server import RequestBodyLimitMiddleware

PRIMARY = "body-limit-primary-canary-001"
SECONDARY = "body-limit-secondary-canary-002"
WRONG = "body-limit-wrong-canary-999"

DEFAULT_LIMIT = 1048576


def _make_client(monkeypatch: pytest.MonkeyPatch, *, max_body: str | None = None):
    """Build a lifespan-managed test client with body-limit env configured."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    if max_body is None:
        monkeypatch.delenv("MCP_MAX_BODY_BYTES", raising=False)
    else:
        monkeypatch.setenv("MCP_MAX_BODY_BYTES", max_body)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def _init_body(pad: str = "") -> dict:
    body: dict = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    if pad:
        body["params"]["_pad"] = pad
    return body


def _post_init(client, path: str, token: str | None, body: dict):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    # No Origin header on purpose: existing clients omit it and must keep working.
    return client.post(path, json=body, headers=headers)


def _run_middleware(
    middleware: RequestBodyLimitMiddleware,
    *,
    path: str = "/mcp",
    method: str = "POST",
    content_length: bytes | None = b"10",
) -> tuple[bool, int | None, dict | None]:
    """Drive the middleware with a fake downstream, capturing status/body."""
    calls: list[bool] = []
    statuses: list[int] = []
    bodies: list[dict | None] = []

    async def downstream(scope: dict, receive: object, send: object) -> None:
        calls.append(True)

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict) -> None:
        if message.get("type") == "http.response.start":
            statuses.append(message.get("status"))
        elif message.get("type") == "http.response.body":
            raw = message.get("body", b"")
            try:
                import json as _json

                bodies.append(_json.loads(raw.decode() or "{}"))
            except (ValueError, UnicodeDecodeError):
                bodies.append(None)

    headers = []
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope = {"type": "http", "path": path, "method": method, "headers": headers}
    middleware.app = downstream
    asyncio.run(middleware(scope, _receive, _send))
    return (bool(calls), statuses[0] if statuses else None, bodies[0] if bodies else None)


def test_default_max_body_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset MCP_MAX_BODY_BYTES defaults to ~1 MiB (1048576)."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.delenv("MCP_MAX_BODY_BYTES", raising=False)
    assert config.load_settings().mcp_max_body_bytes == DEFAULT_LIMIT


def test_override_max_body_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit MCP_MAX_BODY_BYTES overrides the default."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.setenv("MCP_MAX_BODY_BYTES", "2048")
    assert config.load_settings().mcp_max_body_bytes == 2048


@pytest.mark.parametrize("raw", ["0", "-5", "abc", ""])
def test_invalid_max_body_bytes_fails_closed(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Zero, negative, non-numeric, or empty limits fail closed without leaks."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.setenv("MCP_MAX_BODY_BYTES", raw)
    with pytest.raises(RuntimeError, match="MCP_MAX_BODY_BYTES|numeric"):
        config.load_settings()


def test_under_limit_success_both_paths_no_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small bodies pass on both endpoints when clients omit Origin."""
    with _make_client(monkeypatch, max_body="8192") as client:
        for path in ("/mcp", "/worker-mcp"):
            response = _post_init(client, path, PRIMARY, _init_body())
            assert response.status_code == 200, response.text[:500]
            assert PRIMARY not in response.text
            assert SECONDARY not in response.text


def test_oversized_rejected_both_paths_generic_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared bodies over the limit get generic 413 on both endpoints."""
    with _make_client(monkeypatch, max_body="512") as client:
        big = _init_body(pad="x" * 2000)
        for path in ("/mcp", "/worker-mcp"):
            response = _post_init(client, path, PRIMARY, big)
            assert response.status_code == 413, response.text[:500]
            assert response.json() == {"error": "payload too large"}
            assert PRIMARY not in response.text
            assert SECONDARY not in response.text
            assert WRONG not in response.text


def test_oversized_without_auth_stays_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth still runs first: oversized bodies without a token stay 401."""
    with _make_client(monkeypatch, max_body="512") as client:
        big = _init_body(pad="x" * 2000)
        for path in ("/mcp", "/worker-mcp"):
            assert _post_init(client, path, None, big).status_code == 401
            assert _post_init(client, path, WRONG, big).status_code == 401


def test_health_stays_open_with_tiny_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /health never requires auth and is never 413, even with tiny limit."""
    with _make_client(monkeypatch, max_body="16") as client:
        response = client.get("/health")
        assert response.status_code in (200, 503)
        assert response.status_code != 401
        assert response.status_code != 413


def test_middleware_boundary_and_passthrough() -> None:
    """Exact limit passes; limit+1 rejects; missing/malformed pass through."""

    async def _noop(scope: dict, receive: object, send: object) -> None:
        return None

    called, status, body = _run_middleware(
        RequestBodyLimitMiddleware(_noop, 100), content_length=b"100"
    )
    assert called and status is None and body is None

    called, status, body = _run_middleware(
        RequestBodyLimitMiddleware(_noop, 100), content_length=b"101"
    )
    assert not called and status == 413 and body == {"error": "payload too large"}

    called, status, _ = _run_middleware(RequestBodyLimitMiddleware(_noop, 100), content_length=None)
    assert called and status is None

    for malformed in (b"abc", b"", b"12x", b"-7"):
        called, status, _ = _run_middleware(
            RequestBodyLimitMiddleware(_noop, 100), content_length=malformed
        )
        assert called, malformed
        assert status is None, malformed


def test_middleware_skips_non_mcp_paths_and_non_http() -> None:
    """Health and non-HTTP scopes never trigger the 413 path."""

    async def _noop(scope: dict, receive: object, send: object) -> None:
        return None

    for path in ("/health", "/health/", "/other"):
        called, status, _ = _run_middleware(
            RequestBodyLimitMiddleware(_noop, 1), path=path, content_length=b"9999"
        )
        assert called and status is None, path

    async def _probe_non_http() -> bool:
        calls: list[bool] = []

        async def downstream(scope: dict, receive: object, send: object) -> None:
            calls.append(True)

        async def _receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(message: dict) -> None:
            return None

        middleware = RequestBodyLimitMiddleware(downstream, 1)
        await middleware({"type": "websocket"}, _receive, _send)
        return bool(calls)

    assert asyncio.run(_probe_non_http())


def test_middleware_rejects_invalid_limit() -> None:
    """Middleware construction itself fails closed on non-positive limits."""

    async def _noop(scope: dict, receive: object, send: object) -> None:
        return None

    for bad in (0, -1, "100"):  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="positive integer"):
            RequestBodyLimitMiddleware(_noop, bad)


def test_oversized_never_invokes_downstream() -> None:
    """Oversized declared length returns 413 without calling the app/tools."""

    async def _noop(scope: dict, receive: object, send: object) -> None:
        return None

    called, status, body = _run_middleware(
        RequestBodyLimitMiddleware(_noop, 64),
        path="/worker-mcp",
        content_length=b"1000000",
    )
    assert not called
    assert status == 413
    assert body == {"error": "payload too large"}
