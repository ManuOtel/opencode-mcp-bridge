"""Request-body size protection tests. No network to opencode.

Covers MCP_MAX_BODY_BYTES default/override, enforcement on /mcp and
/worker-mcp via declared Content-Length and streamed/chunked bodies,
under-limit success, oversized 413 without tool invocation,
absent/malformed Content-Length streaming enforcement, fragmented
bodies, health/auth preservation, clients omitting Origin, bounded
buffering, and no secret leakage.
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
    """Exact limit passes; limit+1 rejects; empty missing/malformed passes."""

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


def _run_streamed(
    max_bytes: int,
    chunks: list[bytes],
    *,
    path: str = "/mcp",
    content_length: bytes | None = None,
) -> tuple[bool, int | None, dict | None, bytes, list[dict]]:
    """Drive middleware with fragmented http.request chunks.

    Returns (downstream_called, status, parsed_413_body, seen_body, seen_msgs).
    """
    calls: list[bool] = []
    statuses: list[int] = []
    bodies: list[dict | None] = []
    seen: list[bytes] = []
    seen_msgs: list[dict] = []
    queue: list[dict] = []
    for index, chunk in enumerate(chunks):
        queue.append(
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
        )
    if not queue:
        queue.append({"type": "http.request", "body": b"", "more_body": False})
    receive_calls = [0]

    async def downstream(scope: dict, receive: object, send: object) -> None:
        calls.append(True)
        while True:
            message = await receive()  # type: ignore[misc]
            seen_msgs.append(dict(message))
            seen.append(bytes(message.get("body", b"") or b""))
            if not message.get("more_body", False):
                break

    async def _receive() -> dict:
        receive_calls[0] += 1
        if queue:
            return queue.pop(0)
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

    async def _noop(scope: dict, receive: object, send: object) -> None:
        return None

    headers = []
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope = {"type": "http", "path": path, "method": "POST", "headers": headers}
    middleware = RequestBodyLimitMiddleware(_noop, max_bytes)
    middleware.app = downstream  # type: ignore[method-assign]
    asyncio.run(middleware(scope, _receive, _send))
    joined = b"".join(seen)
    result_body = bodies[0] if bodies else None
    result_status = statuses[0] if statuses else None
    # Attach receive count for bounded checks via function attr hack.
    _run_streamed.last_receive_calls = receive_calls[0]  # type: ignore[attr-defined]
    return (bool(calls), result_status, result_body, joined, seen_msgs)


@pytest.mark.parametrize("path", ["/mcp", "/worker-mcp"])
def test_fragmented_under_limit_replays_exactly(path: str) -> None:
    """Fragmented under-limit bodies reach downstream intact on both paths."""
    chunks = [b"a" * 30, b"b" * 30, b"c" * 40]
    called, status, body, joined, seen_msgs = _run_streamed(
        100, chunks, path=path, content_length=None
    )
    assert called and status is None and body is None
    assert joined == b"".join(chunks)
    assert [m.get("more_body") for m in seen_msgs] == [True, True, False]
    assert [bytes(m.get("body", b"")) for m in seen_msgs] == chunks


@pytest.mark.parametrize("path", ["/mcp", "/worker-mcp"])
def test_fragmented_over_limit_rejects_before_downstream(path: str) -> None:
    """Fragmented over-limit bodies get generic 413 with no downstream call."""
    chunks = [b"x" * 40, b"y" * 40, b"z" * 40]
    called, status, body, joined, _ = _run_streamed(100, chunks, path=path, content_length=None)
    assert not called
    assert status == 413
    assert body == {"error": "payload too large"}
    assert joined == b""


@pytest.mark.parametrize("path", ["/mcp", "/worker-mcp"])
@pytest.mark.parametrize("header", [None, b"abc", b"", b"12x", b"-7"])
def test_absent_malformed_streamed_over_limit_rejects(path: str, header: object) -> None:
    """Absent/malformed lengths no longer bypass: streamed bytes are counted."""
    chunks = [b"q" * 60, b"r" * 60]
    called, status, body, _, _ = _run_streamed(
        100,
        chunks,
        path=path,
        content_length=header,  # type: ignore[arg-type]
    )
    assert not called
    assert status == 413
    assert body == {"error": "payload too large"}


@pytest.mark.parametrize("path", ["/mcp", "/worker-mcp"])
@pytest.mark.parametrize("header", [None, b"abc", b"", b"-7"])
def test_absent_malformed_streamed_under_limit_passes(path: str, header: object) -> None:
    """Absent/malformed lengths with small actual bodies still pass through."""
    chunks = [b"ok", b"!"]
    called, status, body, joined, _ = _run_streamed(
        100,
        chunks,
        path=path,
        content_length=header,  # type: ignore[arg-type]
    )
    assert called and status is None and body is None
    assert joined == b"ok!"


def test_lying_content_length_streamed_enforced() -> None:
    """A small declared length cannot hide a large streamed body."""
    chunks = [b"a" * 50, b"b" * 60]
    called, status, body, _, _ = _run_streamed(100, chunks, path="/mcp", content_length=b"10")
    assert not called
    assert status == 413
    assert body == {"error": "payload too large"}


def test_streamed_413_is_generic_and_bounded() -> None:
    """Over-limit 413 echoes nothing; buffering stops at the first bad chunk."""
    secret = b"secret-body-canary-xyz" + b"b" * 60
    chunks = [b"a" * 60, secret, b"c" * 200]
    called, status, body, _, _ = _run_streamed(100, chunks, path="/mcp")
    assert not called
    assert status == 413
    assert body == {"error": "payload too large"}
    assert "secret-body-canary-xyz" not in str(body)
    # Limit 100: 60 ok, next chunk pushes over -> reject on 2nd receive.
    assert _run_streamed.last_receive_calls == 2  # type: ignore[attr-defined]


def test_streamed_health_exempt_and_non_mcp_passthrough() -> None:
    """Health and non-MCP paths never 413, even with huge streamed bodies."""
    big = [b"x" * 500, b"y" * 500]
    for path in ("/health", "/health/", "/other"):
        called, status, body, joined, _ = _run_streamed(10, big, path=path, content_length=b"9999")
        assert called and status is None and body is None, path
        assert joined == b"".join(big), path


def test_streamed_non_http_passthrough() -> None:
    """WebSocket scopes pass through without reading body chunks."""

    async def downstream(scope: dict, receive: object, send: object) -> None:
        scope["called"] = True  # type: ignore[index]

    async def _receive() -> dict:
        raise AssertionError("receive must not be called for non-HTTP")

    async def _send(message: dict) -> None:
        return None

    scope: dict = {"type": "websocket", "path": "/mcp", "headers": []}
    asyncio.run(RequestBodyLimitMiddleware(downstream, 10)(scope, _receive, _send))
    assert scope.get("called") is True


def _call_app(
    app: object, *, path: str, token: str | None, chunks: list[bytes]
) -> tuple[int | None, str]:
    """Call the full ASGI stack with fragmented chunks, capturing status/text."""
    queue: list[dict] = []
    for index, chunk in enumerate(chunks):
        queue.append({"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1})
    if not queue:
        queue.append({"type": "http.request", "body": b"", "more_body": False})
    statuses: list[int] = []
    texts: list[bytes] = []

    async def _receive() -> dict:
        if queue:
            return queue.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict) -> None:
        if message.get("type") == "http.response.start":
            statuses.append(message.get("status"))
        elif message.get("type") == "http.response.body":
            texts.append(bytes(message.get("body", b"") or b""))

    headers = [(b"accept", b"application/json, text/event-stream")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    asyncio.run(app(scope, _receive, _send))  # type: ignore[operator]
    status = statuses[0] if statuses else None
    text = b"".join(texts).decode(errors="replace")
    return (status, text)


@pytest.mark.parametrize("path", ["/mcp", "/worker-mcp"])
def test_app_streamed_auth_ordering_and_413(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """Full stack: unauth streamed oversized stays 401; authed gets 413."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    monkeypatch.setenv("MCP_MAX_BODY_BYTES", "64")
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    app = server.create_app()
    big = [b"a" * 40, b"b" * 40]
    status, text = _call_app(app, path=path, token=None, chunks=big)
    assert status == 401
    status, text = _call_app(app, path=path, token=WRONG, chunks=big)
    assert status == 401
    status, text = _call_app(app, path=path, token=PRIMARY, chunks=big)
    assert status == 413
    assert '"payload too large"' in text
    assert PRIMARY not in text
    assert WRONG not in text
    assert "aaa" not in text
