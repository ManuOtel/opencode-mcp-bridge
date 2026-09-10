"""Static Smithery server-card fallback. No network.

The bridge uses a static Bearer token with no OAuth server. Smithery
documents a static fallback at /.well-known/mcp/server-card.json when
its automatic scan cannot complete behind auth. The card must stay
truthful: worker endpoint tools only, bearer auth only, no OAuth
claims, no tokens, no exec_run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server

PRIMARY = "server-card-primary-001"
WRONG = "server-card-wrong-999"


def _make_client(monkeypatch: pytest.MonkeyPatch):
    """Build a lifespan-managed test client with a fixed bearer token."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def test_server_card_open_with_truthful_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated GET returns the worker card, bearer only, exact tools."""
    with _make_client(monkeypatch) as client:
        for path in (
            "/.well-known/mcp/server-card.json",
            "/.well-known/mcp/server-card.json/",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text[:300])
            assert response.headers["content-type"].startswith("application/json")
            payload = response.json()
            assert payload["serverInfo"]["name"] == "opencode-bridge-worker"
            assert payload["serverInfo"]["version"]
            assert payload["authentication"] == {"required": True, "schemes": ["bearer"]}
            assert "oauth" not in response.text.lower()
            names = [tool["name"] for tool in payload["tools"]]
            assert names == sorted(server.WORKER_TOOL_NAMES)
            assert len(names) == 5
            assert "exec_run" not in names
            for tool in payload["tools"]:
                assert tool["description"]
                assert isinstance(tool["inputSchema"], dict)
                assert tool["inputSchema"].get("type") == "object"
            assert payload["resources"] == []
            assert payload["prompts"] == []
            assert PRIMARY not in response.text
            assert WRONG not in response.text


def test_server_card_head_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEAD on the card path bypasses auth like other discovery routes."""
    with _make_client(monkeypatch) as client:
        response = client.head("/.well-known/mcp/server-card.json")
        assert response.status_code == 200


def test_server_card_post_still_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only GET/HEAD bypass auth; POST to the card stays 401."""
    with _make_client(monkeypatch) as client:
        response = client.post("/.well-known/mcp/server-card.json")
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        assert PRIMARY not in response.text


def test_server_card_matches_live_worker_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Card tool names match the live authenticated worker tools/list."""
    import json

    with _make_client(monkeypatch) as client:
        card = client.get("/.well-known/mcp/server-card.json").json()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {PRIMARY}",
        }
        live = client.post("/worker-mcp", json=body, headers=headers)
        assert live.status_code == 200, live.text[:300]
        live_names: list[str] = []
        for line in live.text.splitlines():
            if line.startswith("data: "):
                tools = json.loads(line[len("data: ") :]).get("result", {}).get("tools", [])
                if tools:
                    live_names = sorted(t["name"] for t in tools)
                    break
        assert live_names == sorted(server.WORKER_TOOL_NAMES)
        card_names = sorted(t["name"] for t in card["tools"])
        assert card_names == live_names
