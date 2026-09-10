"""Transport contract tests for Streamable HTTP endpoints. No network."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server

PRIMARY = "transport-contract-primary-001"
WRONG = "transport-contract-wrong-999"

LEGACY_NAMES = {
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
}


def _make_client(monkeypatch: pytest.MonkeyPatch, *, max_body: str | None = None):
    """Build a lifespan-managed test client with a fixed bearer token."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    if max_body is None:
        monkeypatch.delenv("MCP_MAX_BODY_BYTES", raising=False)
    else:
        monkeypatch.setenv("MCP_MAX_BODY_BYTES", max_body)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


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


def _tools_list_body() -> dict:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _tool_names(response) -> list[str]:
    assert response.status_code == 200, response.text[:500]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            tools = payload.get("result", {}).get("tools", [])
            if tools:
                return sorted(t["name"] for t in tools)
    payload = response.json()
    tools = payload.get("result", {}).get("tools", [])
    if tools:
        return sorted(t["name"] for t in tools)
    raise AssertionError(f"no tools payload in: {response.text[:500]}")


def _no_downstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches tool/client execution."""

    def _explode() -> object:
        raise AssertionError("downstream must not execute on rejected requests")

    monkeypatch.setattr(server, "get_client", _explode)


def test_missing_and_wrong_bearer_rejected_before_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing/wrong Bearer gets generic 401 on both paths, never downstream."""
    _no_downstream(monkeypatch)
    with _make_client(monkeypatch) as client:
        for path in ("/mcp", "/worker-mcp"):
            for token in (None, WRONG):
                for body in (_init_body(), _tools_list_body()):
                    response = client.post(path, json=body, headers=_headers(token))
                    assert response.status_code == 401, (path, token)
                    assert response.json() == {"error": "unauthorized"}
                    assert PRIMARY not in response.text
                    assert WRONG not in response.text
            assert client.get(path).status_code == 401
            assert client.delete(path).status_code == 401


def test_oversized_without_auth_stays_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth runs first: oversized declared bodies without a token stay 401."""
    _no_downstream(monkeypatch)
    with _make_client(monkeypatch, max_body="512") as client:
        big = _init_body(pad="x" * 2000)
        for path in ("/mcp", "/worker-mcp"):
            assert client.post(path, json=big, headers=_headers(None)).status_code == 401
            denied = client.post(path, json=big, headers=_headers(WRONG))
            assert denied.status_code == 401
            assert denied.json() == {"error": "unauthorized"}


def test_declared_oversized_returns_generic_413(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared bodies over the limit get generic 413 on both endpoints."""
    _no_downstream(monkeypatch)
    with _make_client(monkeypatch, max_body="512") as client:
        big = _init_body(pad="x" * 2000)
        for path in ("/mcp", "/worker-mcp"):
            response = client.post(path, json=big, headers=_headers(PRIMARY))
            assert response.status_code == 413, response.text[:500]
            assert response.json() == {"error": "payload too large"}
            assert PRIMARY not in response.text
            assert WRONG not in response.text
            assert "x" * 16 not in response.text


def _call_app(app, *, path: str, token: str | None, chunks: list[bytes]):
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
    asyncio.run(app(scope, _receive, _send))
    return (statuses[0] if statuses else None, b"".join(texts).decode(errors="replace"))


@pytest.mark.parametrize("path", ["/mcp", "/worker-mcp"])
def test_streamed_oversized_returns_generic_413(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """Fragmented over-limit bodies get 413 authed, 401 without auth."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("MCP_MAX_BODY_BYTES", "64")
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    app = server.create_app()
    big = [b"a" * 40, b"b" * 40]
    status, _ = _call_app(app, path=path, token=None, chunks=big)
    assert status == 401
    status, _ = _call_app(app, path=path, token=WRONG, chunks=big)
    assert status == 401
    status, text = _call_app(app, path=path, token=PRIMARY, chunks=big)
    assert status == 413
    assert '"payload too large"' in text
    assert PRIMARY not in text
    assert WRONG not in text


def test_health_minimal_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /health is open and returns exactly {"ok": True} when reachable."""

    class FakeClient:
        async def health(self) -> dict:
            return {"healthy": True, "version": "9.9.9-hidden"}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"ok": True}
    assert "9.9.9-hidden" not in response.text
    assert PRIMARY not in response.text


def test_worker_tools_list_exact_five_without_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker tools/list exposes exactly the five worker tools, never exec_run."""
    with _make_client(monkeypatch) as client:
        names = _tool_names(
            client.post("/worker-mcp", json=_tools_list_body(), headers=_headers(PRIMARY))
        )
    assert names == sorted(server.WORKER_TOOL_NAMES)
    assert len(names) == 5
    assert "exec_run" not in names


def test_full_tools_list_backward_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full /mcp tools/list keeps all 16 tools including legacy and exec_run."""
    with _make_client(monkeypatch) as client:
        names = _tool_names(client.post("/mcp", json=_tools_list_body(), headers=_headers(PRIMARY)))
    assert names == sorted(server.ALL_TOOL_NAMES)
    assert len(names) == 16
    assert LEGACY_NAMES <= set(names)
    assert set(server.WORKER_TOOL_NAMES) <= set(names)
    assert "exec_run" in names
