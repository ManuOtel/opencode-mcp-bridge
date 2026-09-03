"""HTTP-level tests for dual MCP endpoints. No network to opencode."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server

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

TOKEN = "test-token-123"


def _make_client(monkeypatch: pytest.MonkeyPatch):
    """Build a lifespan-managed test client with a fixed bearer token."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def _rpc(client, path: str, method: str, token: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
    if method == "initialize":
        body["params"] = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }
    response = client.post(path, json=body, headers=headers)
    return response  # type: ignore[return-value]


def _tool_names(response) -> list[str]:
    assert response.status_code == 200, response.text[:500]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            tools = payload.get("result", {}).get("tools", [])
            if tools:
                return sorted(t["name"] for t in tools)
    # Fallback for JSON-response transport (no SSE framing).
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        tools = payload.get("result", {}).get("tools", [])
        if tools:
            return sorted(t["name"] for t in tools)
    raise AssertionError(f"no tools payload in: {response.text[:500]}")


def test_health_open_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /health never requires the bearer token (503 without opencode)."""
    with _make_client(monkeypatch) as client:
        response = client.get("/health")
        assert response.status_code in (200, 503)
        assert response.status_code != 401
        assert client.get("/health/").status_code != 401
        assert client.post("/health").status_code == 401


def test_auth_required_on_both_mcp_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing or wrong tokens get 401 on /mcp and /worker-mcp."""
    with _make_client(monkeypatch) as client:
        for path in ("/mcp", "/worker-mcp"):
            assert _rpc(client, path, "initialize").status_code == 401
            assert _rpc(client, path, "initialize", "wrong").status_code == 401
            assert _rpc(client, path, "tools/list").status_code == 401
            assert _rpc(client, path, "tools/list", "wrong").status_code == 401
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code != 401
            assert client.delete(path).status_code == 401
            authed = _rpc(client, path, "initialize", TOKEN)
            assert authed.status_code == 200, authed.text[:500]


def test_tool_catalogs_per_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/mcp serves the full 16; /worker-mcp serves exactly the five workers."""
    with _make_client(monkeypatch) as client:
        full = _tool_names(_rpc(client, "/mcp", "tools/list", TOKEN))
        assert full == sorted(server.ALL_TOOL_NAMES)
        assert len(full) == 16
        worker = _tool_names(_rpc(client, "/worker-mcp", "tools/list", TOKEN))
        assert worker == sorted(server.WORKER_TOOL_NAMES)
        assert len(worker) == 5


def test_mcp_legacy_names_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    """/mcp keeps every legacy session/diff/exec tool for existing clients."""
    with _make_client(monkeypatch) as client:
        full = set(_tool_names(_rpc(client, "/mcp", "tools/list", TOKEN)))
        assert LEGACY_NAMES <= full
        worker = set(_tool_names(_rpc(client, "/worker-mcp", "tools/list", TOKEN)))
        assert LEGACY_NAMES.isdisjoint(worker)


def test_shared_tool_annotations_match() -> None:
    """All five worker tools carry equal annotations on both servers."""
    import asyncio

    def _key(tool) -> tuple:
        ann = tool.annotations
        return (
            getattr(ann, "read_only_hint", None),
            getattr(ann, "destructive_hint", None),
            getattr(ann, "idempotent_hint", None),
            getattr(ann, "open_world_hint", None),
        )

    full = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    worker = {t.name: t for t in asyncio.run(server.worker_mcp.list_tools())}
    assert set(worker) == set(server.WORKER_TOOL_NAMES)
    for name in server.WORKER_TOOL_NAMES:
        assert name in full
        assert _key(full[name]) == _key(worker[name])
