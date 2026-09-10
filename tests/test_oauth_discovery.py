"""RFC 9728 discovery for static-Bearer Smithery scans. No network.

The bridge runs no OAuth authorization server, so metadata must stay
truthful: absolute resource identifiers, bearer header support, docs
link, and no invented authorization_servers. Auth on /mcp and
/worker-mcp must not weaken: missing/wrong tokens still get generic
401, now with a Bearer challenge pointing at the metadata URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server

PRIMARY = "oauth-discovery-primary-001"
WRONG = "oauth-discovery-wrong-999"


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


def test_metadata_endpoints_open_with_truthful_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unauthenticated GET returns valid resource metadata, no invented AS."""
    with _make_client(monkeypatch) as client:
        cases = {
            "/.well-known/oauth-protected-resource": "http://testserver/",
            "/.well-known/oauth-protected-resource/mcp": "http://testserver/mcp",
            "/.well-known/oauth-protected-resource/worker-mcp": ("http://testserver/worker-mcp"),
        }
        for path, expected_resource in cases.items():
            response = client.get(path)
            assert response.status_code == 200, (path, response.text[:300])
            assert response.headers["content-type"].startswith("application/json")
            payload = response.json()
            assert payload["resource"] == expected_resource
            assert payload["bearer_methods_supported"] == ["header"]
            assert (
                payload["resource_documentation"]
                == "https://github.com/ManuOtel/opencode-mcp-bridge"
            )
            assert "authorization_servers" not in payload
            assert PRIMARY not in response.text
            assert WRONG not in response.text


def test_401_points_at_metadata_without_weakening_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 body stays generic; challenge names the matching metadata URL."""
    with _make_client(monkeypatch) as client:
        for path, suffix in (("/mcp", "/mcp"), ("/worker-mcp", "/worker-mcp")):
            for token in (None, WRONG):
                headers = {"Content-Type": "application/json"}
                if token is not None:
                    headers["Authorization"] = f"Bearer {token}"
                response = client.post(path, json={"jsonrpc": "2.0"}, headers=headers)
                assert response.status_code == 401, path
                assert response.json() == {"error": "unauthorized"}
                challenge = response.headers.get("www-authenticate", "")
                assert challenge.startswith("Bearer"), challenge
                expected = "http://testserver/.well-known/oauth-protected-resource" + suffix
                assert f'resource_metadata="{expected}"' in challenge
                assert PRIMARY not in response.text
                assert WRONG not in response.text


def test_metadata_post_still_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only GET/HEAD bypass auth; POST to metadata stays 401."""
    with _make_client(monkeypatch) as client:
        response = client.post("/.well-known/oauth-protected-resource/mcp")
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
