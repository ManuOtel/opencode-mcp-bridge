"""Deterministic tests for OpenCode route negotiation. No network."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server
from opencode_mcp_bridge.opencode_client import (
    OpencodeClient,
    OpencodeError,
    _legacy_to_v2_path,
    _unwrap_envelope,
)


def _mock_client(handler: Any) -> OpencodeClient:
    """Build a client backed by a mock transport."""
    client = OpencodeClient("http://opencode", "u", "p")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://opencode"
    )
    return client


def test_legacy_preferred_when_both_families_exist() -> None:
    """Legacy wins when /global/health works, even if /api/health exists."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(
                200, json={"healthy": True, "version": "1.18.30"}, request=request
            )
        if request.url.path == "/api/health":
            return httpx.Response(
                200, json={"data": {"healthy": True, "version": "2.0.0"}}, request=request
            )
        if request.url.path.endswith("/prompt_async"):
            return httpx.Response(204, request=request)
        raise AssertionError(f"unexpected path {request.url.path}")

    async def run() -> tuple[dict[str, Any], bool, int]:
        client = _mock_client(handler)
        try:
            cap = await client.probe_capability()
            before = len(seen)
            cached = await client.probe_capability()
            prompt_ok = await client.prompt_async("ses_x", "hello", directory="/tmp/w")
            return cap, cached is cap, before, prompt_ok
        finally:
            await client.close()

    cap, is_cached, count_before_cache, prompt_ok = asyncio.run(run())
    assert cap["family"] == "legacy"
    assert cap["version"] == "1.18.30"
    assert cap["legacy_available"] is True
    assert "/api/health" not in seen, "legacy must win without probing V2"
    assert is_cached is True
    assert count_before_cache == len([p for p in seen if p == "/global/health"]) == 1
    assert prompt_ok is True
    assert seen[-1] == "/session/ses_x/prompt_async"


def test_v2_selected_when_legacy_health_absent() -> None:
    """V2 fallback engages only when legacy health is absent.

    Health reporting works via the verified /api/health envelope, and
    session creation routes to the verified POST /api/session contract.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(404, json={"error": "not found"}, request=request)
        if request.url.path == "/api/health":
            return httpx.Response(
                200, json={"data": {"healthy": True, "version": "2.0.0"}}, request=request
            )
        if request.url.path == "/api/session":
            return httpx.Response(
                200,
                json={"data": {"id": "ses_1", "location": {"directory": "/tmp/w"}}},
                request=request,
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    async def run() -> tuple[dict[str, Any], dict[str, Any], str | None, dict[str, Any]]:
        client = _mock_client(handler)
        try:
            cap = await client.probe_capability()
            health = await client.health()
            session = await client.create_session("t", "/tmp/w")
            return cap, health, client.server_version, session
        finally:
            await client.close()

    cap, health, version, session = asyncio.run(run())
    assert cap["family"] == "v2"
    assert cap["legacy_available"] is False
    assert cap["v2_available"] is True
    assert version == "2.0.0"
    assert health == {"healthy": True, "version": "2.0.0"}
    assert session["id"] == "ses_1"
    assert seen.count("/api/health") == 1
    assert "/session" not in seen
    assert not any("prompt_async" in path for path in seen)


def test_v2_lifecycle_uses_verified_routes() -> None:
    """V2 worker lifecycle uses verified /api routes, never guessed paths."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if path == "/api/health":
            return httpx.Response(200, json={"data": {"healthy": True}}, request=request)
        if path == "/api/provider":
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": []},
                request=request,
            )
        if path == "/api/model":
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": []},
                request=request,
            )
        if path == "/api/model/default":
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": None},
                request=request,
            )
        if path == "/api/session/active":
            return httpx.Response(200, json={"data": {}}, request=request)
        if path == "/api/session/ses_1/message":
            return httpx.Response(200, json={"data": [], "cursor": {}}, request=request)
        if path == "/api/session/ses_1/interrupt":
            return httpx.Response(200, json={"data": {"interrupted": True}}, request=request)
        if path == "/api/session/ses_1" and request.method == "DELETE":
            return httpx.Response(204, request=request)
        raise AssertionError(f"unexpected {request.method} {path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.probe_capability()
            assert client.api_family == "v2"
            health = await client.health()
            assert health == {"healthy": True}
            providers = await client.list_providers()
            assert providers == {"providers": [], "default": {}}
            assert await client.get_session_status("/tmp/w") == {}
            assert await client.list_messages("ses_1", "/tmp/w") == []
            assert await client.abort_session("ses_1", "/tmp/w") is True
            assert await client.delete_session("ses_1", "/tmp/w") is True
            # Sync send_message has no verified V2 mapping and fails closed.
            with pytest.raises(OpencodeError, match="no verified sync-reply mapping"):
                await client.send_message("ses_1", "hi", directory="/tmp/w")
        finally:
            await client.close()

    asyncio.run(run())
    paths = [r.url.path for r in requests]
    assert paths[:2] == ["/global/health", "/api/health"]
    assert not any("prompt_async" in path for path in paths)
    assert not any(path == "/session/status" for path in paths)
    assert not any(path.startswith("/session/") for path in paths)
    assert not any(path in ("/provider", "/agent") for path in paths)


def test_v2_prompt_uses_verified_prompt_route() -> None:
    """V2 prompt_async uses model/agent switches plus POST .../prompt."""
    seen: list[tuple[str, str]] = []
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        bodies.append(request.content.decode() if request.content else "")
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"data": {"healthy": True}}, request=request)
        if request.url.path == "/api/session/ses_x/model":
            return httpx.Response(204, request=request)
        if request.url.path == "/api/session/ses_x/agent":
            return httpx.Response(204, request=request)
        if request.url.path == "/api/session/ses_x/prompt":
            return httpx.Response(
                200,
                json={"data": {"id": "msg_1", "sessionID": "ses_x"}},
                request=request,
            )
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.probe_capability()
            assert client.api_family == "v2"
            assert (
                await client.prompt_async(
                    "ses_x",
                    "hi",
                    provider_id="acme",
                    model_id="m-1",
                    agent="plan",
                    directory="/tmp/w",
                )
                is True
            )
            # Sync send_message still fails closed before any V2 request.
            with pytest.raises(OpencodeError, match="no verified sync-reply mapping"):
                await client.send_message("ses_x", "hi", directory="/tmp/w")
        finally:
            await client.close()

    asyncio.run(run())
    assert seen[:2] == [("GET", "/global/health"), ("GET", "/api/health")]
    assert ("POST", "/api/session/ses_x/model") in seen
    assert ("POST", "/api/session/ses_x/agent") in seen
    assert ("POST", "/api/session/ses_x/prompt") in seen
    paths = [path for _, path in seen]
    assert not any("prompt_async" in path for path in paths)
    assert not any(path.startswith("/session/") for path in paths)


def test_unsupported_capability_fails_closed() -> None:
    """No usable health path raises clearly and keeps legacy selected."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/global/health", "/api/health"):
            return httpx.Response(404, json={}, request=request)
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> tuple[str, str, int]:
        client = _mock_client(handler)
        try:
            with pytest.raises(OpencodeError, match="no usable legacy or V2"):
                await client.probe_capability()
            return client.api_family, client.server_version or "", len(client.capability or {})
        finally:
            await client.close()

    family, version, cap_len = asyncio.run(run())
    assert family == "legacy"
    assert version == ""
    assert cap_len == 0


def test_incomplete_v2_payload_fails_closed() -> None:
    """A V2 health envelope without a dict payload is rejected, not faked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"data": None}, request=request)
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            with pytest.raises(OpencodeError, match="unsupported V2"):
                await client.probe_capability()
            assert client.api_family == "legacy"
        finally:
            await client.close()

    asyncio.run(run())


def test_v2_session_reads_use_verified_routes() -> None:
    """V2 abort/list_sessions/get_diff use verified /api routes only."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"data": {"healthy": True}}, request=request)
        if request.url.path == "/api/session/ses_1/interrupt":
            return httpx.Response(200, json={"data": {"interrupted": True}}, request=request)
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"data": [], "cursor": {}}, request=request)
        if request.url.path == "/api/session/ses_1/diff":
            return httpx.Response(200, json={"data": []}, request=request)
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.probe_capability()
            assert await client.abort_session("ses_1", "/tmp/w") is True
            assert await client.list_sessions("/tmp/w") == []
            assert await client.get_diff("ses_1", directory="/tmp/w") == []
        finally:
            await client.close()

    asyncio.run(run())
    assert seen[:2] == [("GET", "/global/health"), ("GET", "/api/health")]
    assert ("POST", "/api/session/ses_1/interrupt") in seen
    assert ("GET", "/api/session") in seen
    assert ("GET", "/api/session/ses_1/diff") in seen
    paths = [path for _, path in seen]
    assert not any("prompt_async" in path for path in paths)
    assert not any(path.startswith("/session/") for path in paths)


def test_probe_is_bounded_cached_and_redacted() -> None:
    """Probe makes at most two calls, caches, and never echoes secrets."""
    canary_password = "canary-pw-001"
    canary_prompt = "canary-prompt-002"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404, json={}, request=request)

    async def run() -> str:
        client = OpencodeClient("http://opencode", "u", canary_password)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        try:
            with pytest.raises(OpencodeError) as excinfo:
                await client.probe_capability()
            message = str(excinfo.value)
            assert canary_password not in message
            assert canary_prompt not in message
            count_after_first = len(seen)
            with pytest.raises(OpencodeError):
                await client.probe_capability(force_refresh=True)
            return message + f"|{count_after_first}|{len(seen)}"
        finally:
            await client.close()

    outcome = asyncio.run(run())
    assert outcome.endswith("|2|4")


def test_model_policy_preserved() -> None:
    """Free default and explicit pair rule survive the negotiation change."""
    client = OpencodeClient("http://opencode", "u", "p")
    try:
        assert client.default_provider_id == "opencode"
        assert client.default_model_id == "muse-spark-1.3-contributor-free"
        assert client.resolve_model(None, None) == (
            "opencode",
            "muse-spark-1.3-contributor-free",
        )
        assert client.resolve_model("acme", "m-1") == ("acme", "m-1")
        with pytest.raises(ValueError, match="together"):
            client.resolve_model("only-provider", None)
        with pytest.raises(ValueError, match="together"):
            asyncio.run(client.send_message("ses_x", "hi", model_id="only-model"))
    finally:
        asyncio.run(client.close())


def test_public_tool_catalog_and_boundaries_unchanged() -> None:
    """Negotiation adds no MCP tools and keeps worker-only boundaries."""
    full_names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    worker_names = {t.name for t in asyncio.run(server.worker_mcp.list_tools())}
    assert full_names == set(server.ALL_TOOL_NAMES)
    assert len(full_names) == 19
    assert worker_names == set(server.WORKER_TOOL_NAMES)
    assert len(worker_names) == 8
    assert "exec_run" in full_names
    assert "exec_run" not in worker_names
    assert _unwrap_envelope({"data": {"a": 1}}) == {"a": 1}
    assert _unwrap_envelope([1, 2]) == [1, 2]
    assert _legacy_to_v2_path("/global/health") == "/api/health"
    with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
        _legacy_to_v2_path("/session/ses_1/abort")
    with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
        _legacy_to_v2_path("/provider")


def test_first_non_health_call_auto_probes_then_uses_legacy() -> None:
    """First normal call auto-probes (legacy first) then uses legacy path."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(
                200, json={"healthy": True, "version": "1.18.30"}, request=request
            )
        if request.url.path == "/provider":
            return httpx.Response(
                200,
                json={"all": [], "connected": [], "default": {}},
                request=request,
            )
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        client = _mock_client(handler)
        try:
            providers = await client.list_providers()
            return providers, dict(client.capability or {})
        finally:
            await client.close()

    providers, cap = asyncio.run(run())
    assert cap["family"] == "legacy"
    assert providers == {"providers": [], "default": {}}
    assert seen == ["/global/health", "/provider"]
    assert "/api/health" not in seen


def test_health_uses_cached_probe_without_duplicate() -> None:
    """Two health() calls cost one probe; readiness-style reuse is free."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.path == "/global/health"
        return httpx.Response(200, json={"healthy": True, "version": "1.18.30"}, request=request)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        client = _mock_client(handler)
        try:
            first = await client.health()
            second = await client.health()
            return first, second
        finally:
            await client.close()

    first, second = asyncio.run(run())
    assert first == {"healthy": True, "version": "1.18.30"}
    assert second == first
    assert seen == ["/global/health"]


def test_v2_health_only_auto_probe_routes_verified_calls() -> None:
    """Auto-probe V2: health works, data plane uses verified routes."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/health":
            return httpx.Response(
                200,
                json={"data": {"healthy": True, "version": "2.0.0"}},
                request=request,
            )
        if request.url.path == "/api/session":
            return httpx.Response(
                200,
                json={"data": {"id": "ses_9", "location": {"directory": "/tmp/w"}}},
                request=request,
            )
        if request.url.path == "/api/session/ses_x/model":
            return httpx.Response(204, request=request)
        if request.url.path == "/api/session/ses_x/prompt":
            return httpx.Response(
                200, json={"data": {"id": "msg_2", "sessionID": "ses_x"}}, request=request
            )
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> tuple[dict[str, Any], str]:
        client = _mock_client(handler)
        try:
            health = await client.health()
            session = await client.create_session("t", "/tmp/w")
            assert session["id"] == "ses_9"
            assert await client.prompt_async("ses_x", "hi", directory="/tmp/w") is True
            # Only sync send_message still fails closed, before any HTTP.
            with pytest.raises(OpencodeError, match="no verified sync-reply mapping"):
                await client.send_message("ses_x", "hi", directory="/tmp/w")
            return health, client.api_family
        finally:
            await client.close()

    health, family = asyncio.run(run())
    assert family == "v2"
    assert health == {"healthy": True, "version": "2.0.0"}
    paths = [path for _, path in seen]
    assert paths[:2] == ["/global/health", "/api/health"]
    assert not any("prompt_async" in p for p in paths)
    assert not any(p.startswith("/session/") for p in paths)


def test_repeated_calls_use_cached_capability() -> None:
    """Second normal call reuses capability with no extra health traffic."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(
                200, json={"healthy": True, "version": "1.18.30"}, request=request
            )
        if request.url.path == "/provider":
            return httpx.Response(
                200,
                json={"all": [], "connected": [], "default": {}},
                request=request,
            )
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.list_providers()
            first_cap = client.capability
            await client.list_providers()
            assert client.capability is first_cap
            await client.health()
        finally:
            await client.close()

    asyncio.run(run())
    assert seen.count("/global/health") == 1
    assert seen.count("/provider") == 2


def test_force_refresh_is_only_reprobe_path() -> None:
    """Normal repeats never re-probe; only explicit force_refresh does."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(
                200, json={"healthy": True, "version": "1.18.30"}, request=request
            )
        if request.url.path == "/provider":
            return httpx.Response(
                200,
                json={"all": [], "connected": [], "default": {}},
                request=request,
            )
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> list[int]:
        client = _mock_client(handler)
        counts: list[int] = []
        try:
            await client.health()
            counts.append(seen.count("/global/health"))
            await client.health()
            counts.append(seen.count("/global/health"))
            await client.list_providers()
            counts.append(seen.count("/global/health"))
            await client.probe_capability()
            counts.append(seen.count("/global/health"))
            await client.probe_capability(force_refresh=True)
            counts.append(seen.count("/global/health"))
            await client.health(force_refresh=True)
            counts.append(seen.count("/global/health"))
            return counts
        finally:
            await client.close()

    assert asyncio.run(run()) == [1, 1, 1, 1, 2, 3]
