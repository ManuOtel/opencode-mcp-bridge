"""Health disclosure tests: /health stays minimal and unauthenticated. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server

TOKEN = "test-token-123"

SENSITIVE_MARKERS = (
    "1.2.3-secret-version",
    "http://127.0.0.1:4096",
    "/home/tester/secret-path",
    "super-secret-password",
    "Traceback",
)


def _make_client(monkeypatch: pytest.MonkeyPatch):
    """Build a lifespan-managed test client with a fixed bearer token."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def test_health_success_minimal_no_disclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 returns only {"ok": True} even when backend reports a version."""

    class FakeClient:
        async def health(self) -> dict:
            return {"healthy": True, "version": "1.2.3-secret-version"}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"ok": True}
    for marker in SENSITIVE_MARKERS:
        assert marker not in response.text


def test_health_failure_generic_no_disclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    """503 returns a generic error without exception text, URLs, or paths."""

    class FakeClient:
        async def health(self) -> dict:
            raise RuntimeError(
                "opencode GET http://127.0.0.1:4096/global/health failed: "
                "/home/tester/secret-path super-secret-password Traceback"
            )

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["ok"] is False
    assert payload.get("error") == "unavailable"
    for marker in SENSITIVE_MARKERS:
        assert marker not in response.text
