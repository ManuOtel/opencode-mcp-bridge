"""Streamable HTTP transport + dual-endpoint auth regression coverage.

No network to opencode. Locks the protocol/auth contract without
changing behavior: authenticated initialize shape per endpoint,
secondary-token catalog access on both paths, generic 401 bodies for
malformed credentials on every method, and tools/call proof that
/worker-mcp never exposes exec_run.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server

PRIMARY = "transport-primary-canary-001"
SECONDARY = "transport-secondary-canary-002"
WRONG = "transport-wrong-canary-999"

SERVER_NAMES = {"/mcp": "opencode-bridge", "/worker-mcp": "opencode-bridge-worker"}


def _make_client(monkeypatch: pytest.MonkeyPatch, *, secondary: str | None = None):
    """Build a lifespan-managed test client with rotation tokens configured."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    if secondary is None:
        monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    else:
        monkeypatch.setenv("MCP_BEARER_TOKEN_SECONDARY", secondary)
    monkeypatch.delenv("ENABLE_EXEC_RUN", raising=False)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def _headers(token: str | None = None, scheme: str = "Bearer") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"{scheme} {token}" if token else scheme
    return headers


def _init_body() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }


def _sse_result(response) -> dict:
    """Return the first SSE data payload as JSON."""
    assert response.status_code == 200, response.text[:500]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no SSE data payload in: {response.text[:500]}")


def test_authenticated_initialize_reports_transport_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialize echoes the protocol version and names each endpoint server."""
    with _make_client(monkeypatch) as client:
        for path, expected_name in SERVER_NAMES.items():
            response = client.post(path, json=_init_body(), headers=_headers(PRIMARY))
            payload = _sse_result(response)
            result = payload["result"]
            assert result["protocolVersion"] == "2025-06-18"
            assert "tools" in result["capabilities"]
            assert result["serverInfo"]["name"] == expected_name
            assert PRIMARY not in response.text
            assert SECONDARY not in response.text


def test_secondary_token_covers_catalog_on_both_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation overlap applies to tools/list on /mcp and /worker-mcp."""
    with _make_client(monkeypatch, secondary=SECONDARY) as client:
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        for path in ("/mcp", "/worker-mcp"):
            for token in (PRIMARY, SECONDARY):
                response = client.post(path, json=body, headers=_headers(token))
                assert response.status_code == 200, response.text[:500]
            denied = client.post(path, json=body, headers=_headers(WRONG))
            assert denied.status_code == 401
            assert denied.json() == {"error": "unauthorized"}


def test_unauthorized_matrix_returns_generic_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad credentials get a generic 401 body on every method and path."""
    bad_headers = [
        _headers(None),
        _headers(WRONG),
        _headers("cHJvYmU=", scheme="Basic"),
        _headers("", scheme="Bearer"),
        {"Content-Type": "application/json", "Authorization": "Bearer"},
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer  {PRIMARY}",
        },
    ]
    with _make_client(monkeypatch, secondary=SECONDARY) as client:
        bodies = [
            _init_body(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        for path in ("/mcp", "/worker-mcp"):
            for headers in bad_headers:
                for body in bodies:
                    response = client.post(path, json=body, headers=headers)
                    assert response.status_code == 401, (path, headers, body)
                    assert response.json() == {"error": "unauthorized"}
                for method in ("GET", "DELETE"):
                    response = client.request(method, path, headers=headers)
                    assert response.status_code == 401, (path, method, headers)
                    assert response.json() == {"error": "unauthorized"}
            for token in (PRIMARY, SECONDARY):
                assert client.get(path, headers=_headers(token)).status_code != 401
        for headers in bad_headers:
            response = client.post("/health", headers=headers)
            assert response.status_code == 401
            assert response.json() == {"error": "unauthorized"}
        for token in (PRIMARY, SECONDARY, WRONG):
            assert token not in response.text


def test_bearer_scheme_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lowercase 'bearer' authenticates; the scheme check is not case-gated."""
    with _make_client(monkeypatch) as client:
        response = client.post(
            "/mcp", json=_init_body(), headers=_headers(PRIMARY, scheme="bearer")
        )
        assert response.status_code == 200, response.text[:500]


def test_authenticated_methods_pass_auth_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid tokens never get 401, even for methods the transport rejects."""
    with _make_client(monkeypatch) as client:
        for path in ("/mcp", "/worker-mcp"):
            for method in ("GET", "PUT", "DELETE"):
                response = client.request(method, path, headers=_headers(PRIMARY))
                assert response.status_code != 401, (path, method)


def test_worker_tool_callable_on_both_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tools/call worker_catalog succeeds (isError false) on both endpoints."""

    class _FakeClient:
        default_provider_id = "opencode"
        default_model_id = "m"

        async def get_providers_raw(self) -> dict:
            return {"connected": [], "all": []}

    monkeypatch.setattr(server, "get_client", lambda: _FakeClient())
    with _make_client(monkeypatch) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "worker_catalog", "arguments": {}},
        }
        for path in ("/mcp", "/worker-mcp"):
            payload = _sse_result(client.post(path, json=body, headers=_headers(PRIMARY)))
            assert payload["result"]["isError"] is False


def test_exec_run_never_exposed_on_worker_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even opted-in, /worker-mcp tools/call exec_run is unknown, never a shell."""

    async def _no_spawn(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must not spawn for worker-mcp exec_run")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _no_spawn)
    monkeypatch.setenv("ENABLE_EXEC_RUN", "true")
    with _make_client(monkeypatch) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "exec_run", "arguments": {"command": "echo hi"}},
        }
        payload = _sse_result(client.post("/worker-mcp", json=body, headers=_headers(PRIMARY)))
        assert payload["result"]["isError"] is True
        text = payload["result"]["content"][0]["text"]
        assert "Unknown tool" in text
        assert "echo hi" not in text


def test_mcp_exec_run_disabled_at_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/mcp tools/call exec_run fails closed naming the opt-in flag."""
    monkeypatch.setenv("ENABLE_EXEC_RUN", "false")
    with _make_client(monkeypatch) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "exec_run", "arguments": {"command": "echo hi"}},
        }
        payload = _sse_result(client.post("/mcp", json=body, headers=_headers(PRIMARY)))
        assert payload["result"]["isError"] is True
        text = payload["result"]["content"][0]["text"]
        assert "ENABLE_EXEC_RUN=true" in text
