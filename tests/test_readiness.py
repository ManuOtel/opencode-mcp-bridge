"""Readiness, metrics, and endpoint-boundary tests. No network to opencode."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import observability, server

TOKEN = "readiness-token-123"

SENSITIVE_MARKERS = (
    "1.2.3-secret-version",
    "http://127.0.0.1:4096",
    "/home/tester/secret-path",
    "super-secret-password",
    "Traceback",
    "SECRET-PROMPT-UNIQUE",
    "SECRET-REQUEST-UNIQUE",
    "SECRET-BEARER-UNIQUE",
)


def _make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build a lifespan-managed test client with isolated registry."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("TASK_STATE_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    observability.reset_metrics()
    return TestClient(server.create_app())


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_ready_requires_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GET /ready without (or with a wrong) token is 401, never 200/503."""
    with _make_client(monkeypatch, tmp_path) as client:
        assert client.get("/ready").status_code == 401
        assert client.get("/ready", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/ready", headers=_auth()).status_code in (401, 404, 405)


def test_ready_healthy_minimal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Healthy OpenCode plus registry returns exactly {"ok": True}."""

    class FakeClient:
        async def health(self) -> dict:
            return {"healthy": True, "version": "1.2.3-secret-version"}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch, tmp_path) as client:
        response = client.get("/ready", headers=_auth())
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    for marker in SENSITIVE_MARKERS:
        assert marker not in response.text


def test_ready_opencode_failure_is_generic_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OpenCode down returns a generic 503 with no backend details."""

    class FakeClient:
        async def health(self) -> dict:
            raise RuntimeError(
                "opencode GET http://127.0.0.1:4096/global/health failed: "
                "/home/tester/secret-path super-secret-password Traceback"
            )

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch, tmp_path) as client:
        response = client.get("/ready", headers=_auth())
    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "unavailable"}
    for marker in SENSITIVE_MARKERS:
        assert marker not in response.text


def test_ready_registry_failure_is_generic_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Corrupt registry returns a generic 503 with no path contents."""

    class FakeClient:
        async def health(self) -> dict:
            return {"healthy": True}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    monkeypatch.setattr(
        server, "_load_task_state", lambda: (_ for _ in ()).throw(RuntimeError("corrupt"))
    )
    with _make_client(monkeypatch, tmp_path) as client:
        response = client.get("/ready", headers=_auth())
    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "unavailable"}
    for marker in SENSITIVE_MARKERS:
        assert marker not in response.text


def test_ready_missing_parent_stays_missing_and_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read-only probe never mkdirs: missing parent stays missing, 503."""

    class FakeClient:
        async def health(self) -> dict:
            return {"healthy": True}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch, tmp_path) as client:
        missing = tmp_path / "no-such-parent" / "tasks.json"
        assert not missing.parent.exists()
        monkeypatch.setenv("TASK_STATE_PATH", str(missing))
        response = client.get("/ready", headers=_auth())
        assert response.status_code == 503
        assert response.json() == {"ok": False, "error": "unavailable"}
        assert not missing.parent.exists()
        assert not missing.exists()


def test_ready_head_auth_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HEAD /ready needs the token; authed HEAD never leaks details."""

    class FakeClient:
        async def health(self) -> dict:
            return {"healthy": True}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch, tmp_path) as client:
        assert client.request("HEAD", "/ready").status_code == 401
        response = client.request("HEAD", "/ready", headers=_auth())
        assert response.status_code == 200
        for marker in SENSITIVE_MARKERS:
            assert marker not in response.text


def test_ready_trailing_slash_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GET /ready/ needs the token and matches /ready when healthy."""

    class FakeClient:
        async def health(self) -> dict:
            return {"healthy": True}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    with _make_client(monkeypatch, tmp_path) as client:
        assert client.get("/ready/").status_code == 401
        response = client.get("/ready/", headers=_auth())
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        for marker in SENSITIVE_MARKERS:
            assert marker not in response.text


def test_metrics_requires_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GET /metrics without (or with a wrong) token is 401."""
    with _make_client(monkeypatch, tmp_path) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metrics_post_auth_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """POST /metrics is never open and never returns counters."""
    with _make_client(monkeypatch, tmp_path) as client:
        unauth = client.post("/metrics")
        assert unauth.status_code == 401
        assert "metrics" not in unauth.text
        authed = client.post("/metrics", headers=_auth())
        assert authed.status_code in (401, 404, 405)
        assert authed.status_code != 200
        assert "metrics" not in authed.text


def test_metrics_allowlist_covers_full_catalog() -> None:
    """Static allowlist covers every real tool plus infra subsystems."""
    assert "worker_decide" in observability.METRIC_TOOLS
    assert "worker_resume" in observability.METRIC_TOOLS
    assert set(server.WORKER_TOOL_NAMES) <= set(observability.METRIC_TOOLS)
    assert set(server.ALL_TOOL_NAMES) <= set(observability.METRIC_TOOLS)
    for infra in (
        observability.TOOL_AUTH,
        observability.TOOL_READINESS,
        observability.TOOL_LIVENESS,
        observability.TOOL_METRICS,
    ):
        assert infra in observability.METRIC_TOOLS


def test_metrics_bounded_and_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Counters use only allowlisted triples and never echo secrets."""
    secret_prompt = "SECRET-PROMPT-UNIQUE-ABC-123"
    secret_request = "SECRET-REQUEST-UNIQUE-XYZ-789"
    secret_token = "SECRET-BEARER-UNIQUE-777"
    with _make_client(monkeypatch, tmp_path) as client:
        observability.record(
            event=observability.EVENT_WORKER, tool="worker_run", outcome="succeeded"
        )
        observability.record(event="evil.event", tool=secret_prompt, outcome="succeeded")
        observability.record(
            event=observability.EVENT_WORKER, tool="worker_run", outcome=secret_token
        )
        response = client.get("/metrics", headers=_auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    assert all(isinstance(count, int) for count in metrics.values())
    assert "worker.request|worker_run|succeeded" in metrics
    combined = json.dumps(payload, sort_keys=True)
    for marker in (secret_prompt, secret_request, secret_token, *SENSITIVE_MARKERS):
        assert marker not in combined
    allowed_tools = set(observability.METRIC_TOOLS)
    for key in metrics:
        event, tool, outcome = key.split("|")
        assert event in observability.METRIC_EVENTS
        assert tool in allowed_tools
        assert outcome in observability.METRIC_OUTCOMES


def test_worker_mcp_never_serves_exec_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HTTP tools/list boundary: exec_run only on /mcp, never on /worker-mcp."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **_auth(),
    }

    def _tool_names(path: str, client) -> list[str]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200, response.text[:500]
        for line in response.text.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
                tools = payload.get("result", {}).get("tools", [])
                if tools:
                    return sorted(t["name"] for t in tools)
        payload = response.json()
        tools = payload.get("result", {}).get("tools", [])
        assert tools, response.text[:500]
        return sorted(t["name"] for t in tools)

    with _make_client(monkeypatch, tmp_path) as client:
        full = _tool_names("/mcp", client)
        worker = _tool_names("/worker-mcp", client)
    assert "exec_run" in full
    assert "exec_run" not in worker
    assert set(worker) == set(server.WORKER_TOOL_NAMES)
