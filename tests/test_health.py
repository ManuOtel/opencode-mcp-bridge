"""Health liveness tests: /health stays minimal, open, and dependency-free. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import observability, server

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


def test_health_liveness_minimal_without_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 {"ok": True} without touching OpenCode, even if it is down."""

    def _boom() -> object:
        raise AssertionError("liveness must not touch OpenCode")

    monkeypatch.setattr(server, "get_client", _boom)
    with _make_client(monkeypatch) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"ok": True}
    for marker in SENSITIVE_MARKERS:
        assert marker not in response.text


def test_health_stays_open_and_minimal_when_backend_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend failure never changes liveness: still open 200, no disclosure."""

    class FakeClient:
        async def health(self) -> dict:
            raise RuntimeError(
                "opencode GET http://127.0.0.1:4096/global/health failed: "
                "/home/tester/secret-path super-secret-password Traceback"
            )

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    for marker in SENSITIVE_MARKERS:
        assert marker not in response.text


def test_health_never_touches_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public probes record nothing, so scraping cannot corrupt counters."""
    with _make_client(monkeypatch) as client:
        observability.reset_metrics()
        before = observability.snapshot()
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.request("HEAD", "/health").status_code == 200
        assert observability.snapshot() == before
