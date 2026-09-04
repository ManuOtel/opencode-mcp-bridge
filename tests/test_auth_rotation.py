"""Bearer-token rotation tests: primary/secondary overlap, fail-closed config.

No network. Token values are canaries used only for accept/reject
assertions; error bodies and config error messages must never echo them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import config, server
from opencode_mcp_bridge.server import BearerAuthMiddleware

PRIMARY = "primary-rotation-canary-001"
SECONDARY = "secondary-rotation-canary-002"
WRONG = "wrong-rotation-canary-999"


def _make_client(monkeypatch: pytest.MonkeyPatch, *, secondary: str | None = None):
    """Build a lifespan-managed test client with rotation tokens configured."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    if secondary is None:
        monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    else:
        monkeypatch.setenv("MCP_BEARER_TOKEN_SECONDARY", secondary)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def _rpc(client, token: str | None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    return client.post("/mcp", json=body, headers=headers)


def test_legacy_single_token_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset secondary keeps single-token behavior: primary works, others fail."""
    with _make_client(monkeypatch) as client:
        assert _rpc(client, PRIMARY).status_code == 200
        assert _rpc(client, None).status_code == 401
        assert _rpc(client, WRONG).status_code == 401


def test_rotation_overlap_accepts_both_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """During overlap both primary and secondary authenticate on both paths."""
    with _make_client(monkeypatch, secondary=SECONDARY) as client:
        assert _rpc(client, PRIMARY).status_code == 200
        assert _rpc(client, SECONDARY).status_code == 200
        for path in ("/mcp", "/worker-mcp"):
            for token in (PRIMARY, SECONDARY):
                response = client.get(path, headers={"Authorization": f"Bearer {token}"})
                assert response.status_code != 401
        assert _rpc(client, WRONG).status_code == 401
        assert _rpc(client, None).status_code == 401


def test_removed_old_token_rejected_after_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsetting the secondary ends overlap: only the primary remains valid."""
    with _make_client(monkeypatch, secondary=SECONDARY) as client:
        assert _rpc(client, SECONDARY).status_code == 200
    with _make_client(monkeypatch) as client:
        assert _rpc(client, PRIMARY).status_code == 200
        assert _rpc(client, SECONDARY).status_code == 401


def test_blank_secondary_fails_closed_without_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set-but-blank secondary raises; the message names the var, not the value."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.setenv("MCP_BEARER_TOKEN_SECONDARY", "   ")
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN_SECONDARY"):
        config.load_settings()


def test_duplicate_secondary_fails_closed_without_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secondary equal to the primary is a misconfiguration, never accepted."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.setenv("MCP_BEARER_TOKEN_SECONDARY", PRIMARY)
    with pytest.raises(RuntimeError, match="must differ"):
        config.load_settings()


def test_config_errors_never_echo_token_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed messages carry variable names only, never token canaries."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.setenv("MCP_BEARER_TOKEN_SECONDARY", PRIMARY)
    try:
        config.load_settings()
    except RuntimeError as exc:
        assert PRIMARY not in str(exc)
        assert SECONDARY not in str(exc)
    else:  # pragma: no cover - load_settings must raise here
        raise AssertionError("duplicate secondary must fail closed")


def test_rejection_body_discloses_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 bodies are generic and never reflect either token value."""
    with _make_client(monkeypatch, secondary=SECONDARY) as client:
        for token in (None, WRONG, ""):
            response = _rpc(client, token)
            assert response.status_code == 401
            assert response.json() == {"error": "unauthorized"}
            assert PRIMARY not in response.text
            assert SECONDARY not in response.text
            assert WRONG not in response.text


def test_middleware_accepts_token_list_and_legacy_string() -> None:
    """List construction accepts either slot; legacy single-string still works."""

    async def _noop_app(scope: dict, receive: object, send: object) -> None:
        return None

    async def _probe(middleware: BearerAuthMiddleware, token: str | None) -> bool:
        calls: list[bool] = []

        async def downstream(scope: dict, receive: object, send: object) -> None:
            calls.append(True)

        async def _send(message: dict) -> None:
            return None

        async def _receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        headers = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": headers}
        middleware.app = downstream
        await middleware(scope, _receive, _send)
        return bool(calls)

    import asyncio

    async def run() -> tuple[bool, bool, bool, bool, bool]:
        multi = BearerAuthMiddleware(_noop_app, [PRIMARY, SECONDARY])
        legacy = BearerAuthMiddleware(_noop_app, PRIMARY)
        return (
            await _probe(multi, PRIMARY),
            await _probe(multi, SECONDARY),
            await _probe(multi, WRONG),
            await _probe(multi, None),
            await _probe(legacy, PRIMARY),
        )

    primary_ok, secondary_ok, wrong_ok, missing_ok, legacy_ok = asyncio.run(run())
    assert primary_ok and secondary_ok and legacy_ok
    assert not wrong_ok and not missing_ok


def test_middleware_requires_tokens_fail_closed() -> None:
    """Empty token configuration raises instead of running open."""
    with pytest.raises(RuntimeError, match="no tokens"):
        BearerAuthMiddleware(object(), "")
    with pytest.raises(RuntimeError, match="no tokens"):
        BearerAuthMiddleware(object(), [])


def test_accepted_bearer_tokens_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper returns primary-only by default and both during overlap."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", PRIMARY)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    assert config.accepted_bearer_tokens(config.load_settings()) == (PRIMARY,)
    monkeypatch.setenv("MCP_BEARER_TOKEN_SECONDARY", SECONDARY)
    assert config.accepted_bearer_tokens(config.load_settings()) == (PRIMARY, SECONDARY)
