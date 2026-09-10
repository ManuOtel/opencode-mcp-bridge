"""Origin/Referer allowlist tests for browser-facing MCP requests. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import config, server

TOKEN = "origin-policy-canary-token-001"
ALLOWED = "https://allowed.example"
OTHER = "https://other.example"
EVIL = "https://evil.example"


def _make_client(monkeypatch: pytest.MonkeyPatch, *, origins: str | None):
    """Build a lifespan-managed test client with an optional origin allowlist."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    if origins is None:
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", origins)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def _rpc(client, path: str, token: str | None, *, origin=None, referer=None, host=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    if referer is not None:
        headers["Referer"] = referer
    if host is not None:
        headers["Host"] = host
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
    return client.post(path, json=body, headers=headers)


def test_disabled_policy_allows_any_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset allowlist preserves current behavior even with browser headers."""
    with _make_client(monkeypatch, origins=None) as client:
        for path in ("/mcp", "/worker-mcp"):
            assert _rpc(client, path, TOKEN, origin=EVIL).status_code == 200
            assert _rpc(client, path, TOKEN, referer="https://evil.example/x").status_code == 200
            assert _rpc(client, path, TOKEN, referer="not-a-url").status_code == 200
            assert _rpc(client, path, TOKEN).status_code == 200


def test_disabled_policy_blank_means_no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank or comma-blank values disable the policy like unset does."""
    for blank in ("", "   ", " , , "):
        with _make_client(monkeypatch, origins=blank) as client:
            assert _rpc(client, "/mcp", TOKEN, origin=EVIL).status_code == 200
            assert _rpc(client, "/worker-mcp", TOKEN, origin=EVIL).status_code == 200


def test_allowed_origin_on_both_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact allowlisted Origin passes on /mcp and /worker-mcp."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            response = _rpc(client, path, TOKEN, origin=ALLOWED)
            assert response.status_code == 200, response.text[:500]


def test_disallowed_origin_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-allowlisted Origin gets a generic 403 without echoing values."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            response = _rpc(client, path, TOKEN, origin=EVIL)
            assert response.status_code == 403
            assert response.json() == {"error": "forbidden"}
            assert TOKEN not in response.text
            assert EVIL not in response.text
            assert ALLOWED not in response.text


def test_absent_origin_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Origin and no Referer stays allowed for CLI/SDK clients."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            assert _rpc(client, path, TOKEN).status_code == 200


def test_referer_only_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Origin absent plus Referer deriving to an allowed origin passes."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            response = _rpc(client, path, TOKEN, referer=f"{ALLOWED}/some/page?q=1")
            assert response.status_code == 200, response.text[:500]


def test_referer_only_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Origin absent plus Referer deriving elsewhere is rejected."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            response = _rpc(client, path, TOKEN, referer="https://evil.example/page")
            assert response.status_code == 403
            assert response.json() == {"error": "forbidden"}
            assert "evil.example" not in response.text
            assert TOKEN not in response.text


def test_malformed_referer_rejected_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed Referer values fail closed only when the policy is enabled."""
    bad_values = [
        "not-a-url",
        "/relative/path",
        "ftp://allowed.example/x",
        "https://",
        "https://user@allowed.example/",
        "http://[bad]/",
    ]
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            for bad in bad_values:
                response = _rpc(client, path, TOKEN, referer=bad)
                assert response.status_code == 403, f"{path} {bad!r}"
                assert response.json() == {"error": "forbidden"}


def test_both_headers_origin_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Referer never broadens access when Origin is present."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            allowed_origin_evil_referer = _rpc(
                client, path, TOKEN, origin=ALLOWED, referer="https://evil.example/x"
            )
            assert allowed_origin_evil_referer.status_code == 200
            evil_origin_allowed_referer = _rpc(
                client, path, TOKEN, origin=EVIL, referer=f"{ALLOWED}/x"
            )
            assert evil_origin_allowed_referer.status_code == 403
            assert evil_origin_allowed_referer.json() == {"error": "forbidden"}


def test_auth_runs_before_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing or wrong tokens remain 401 even with a disallowed Origin."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for path in ("/mcp", "/worker-mcp"):
            assert _rpc(client, path, None, origin=EVIL).status_code == 401
            assert _rpc(client, path, "wrong-token", origin=EVIL).status_code == 401
            assert _rpc(client, path, None, origin=ALLOWED).status_code == 401
            missing = _rpc(client, path, None, origin=EVIL)
            assert missing.json() == {"error": "unauthorized"}


def test_health_exempt_from_origin_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /health never enforces origins and never requires auth."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        response = client.get("/health", headers={"Origin": EVIL})
        assert response.status_code in (200, 503)
        assert response.status_code not in (401, 403)
        referer = client.get("/health", headers={"Referer": "not-a-url"})
        assert referer.status_code in (200, 503)
        assert referer.status_code not in (401, 403)


def test_non_mcp_paths_skip_origin_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /health keeps current auth behavior and never returns 403."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        authed = client.post("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert authed.status_code != 403
        assert authed.status_code != 401
        unauthed = client.post("/health", headers={"Origin": EVIL})
        assert unauthed.status_code == 401


def test_host_never_infers_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host headers neither grant nor override the Origin decision."""
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        allowed_host_evil_origin = _rpc(client, "/mcp", TOKEN, origin=EVIL, host="allowed.example")
        assert allowed_host_evil_origin.status_code == 403
        evil_host_no_origin = _rpc(client, "/mcp", TOKEN, host="evil.example")
        assert evil_host_no_origin.status_code == 200


def test_trailing_slash_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single trailing slash in config or request is stripped and tested."""
    with _make_client(monkeypatch, origins=f"{ALLOWED}/") as client:
        assert _rpc(client, "/mcp", TOKEN, origin=ALLOWED).status_code == 200
        assert _rpc(client, "/worker-mcp", TOKEN, origin=ALLOWED).status_code == 200
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        assert _rpc(client, "/mcp", TOKEN, origin=f"{ALLOWED}/").status_code == 200
        assert _rpc(client, "/worker-mcp", TOKEN, origin=f"{ALLOWED}/").status_code == 200


def test_referer_port_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Referer derivation preserves explicit ports for exact matching."""
    with _make_client(monkeypatch, origins="https://allowed.example:8443") as client:
        ok = _rpc(client, "/mcp", TOKEN, referer="https://allowed.example:8443/page")
        assert ok.status_code == 200
        mismatch = _rpc(client, "/mcp", TOKEN, referer="https://allowed.example/page")
        assert mismatch.status_code == 403


def test_rejection_never_leaks_headers_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 bodies are generic and never reflect tokens, origins, or referers."""
    secret_referer = "https://evil.example/secret-path?tok=abc"
    with _make_client(monkeypatch, origins=ALLOWED) as client:
        for response in (
            _rpc(client, "/mcp", TOKEN, origin=EVIL),
            _rpc(client, "/worker-mcp", TOKEN, origin=EVIL),
            _rpc(client, "/mcp", TOKEN, referer=secret_referer),
            _rpc(client, "/mcp", TOKEN, origin=EVIL, referer=secret_referer),
        ):
            assert response.status_code == 403
            assert response.json() == {"error": "forbidden"}
            assert TOKEN not in response.text
            assert EVIL not in response.text
            assert secret_referer not in response.text
            assert "secret-path" not in response.text


def test_config_rejects_malformed_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed allowlist entries fail closed without echoing values."""
    bad_lists = [
        "not-an-origin",
        "example.com",
        "null",
        "*",
        "https://allowed.example/app",
        "https://allowed.example?x=1",
        "https://allowed.example#frag",
        "ftp://allowed.example",
        "https://user@allowed.example",
        "https://allowed.example:badport",
        "https://allowed.example//",
        "https://",
    ]
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    for bad in bad_lists:
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", bad)
        with pytest.raises(RuntimeError, match="MCP_ALLOWED_ORIGINS"):
            config.load_settings()


def test_config_parsing_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset/blank disables; entries dedupe in first-seen order."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    assert config.load_settings().allowed_origins == ()
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "   ")
    assert config.load_settings().allowed_origins == ()
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", f"{ALLOWED}, {OTHER}, {ALLOWED} ")
    assert config.load_settings().allowed_origins == (ALLOWED, OTHER)


def test_middleware_origin_disabled_by_default() -> None:
    """Direct middleware construction without origins preserves old behavior."""

    async def _noop_app(scope: dict, receive: object, send: object) -> None:
        return None

    middleware = server.BearerAuthMiddleware(_noop_app, [TOKEN])
    assert middleware._allowed_origin_set == frozenset()
