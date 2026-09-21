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

    Health reporting works via the verified /api/health envelope, but
    worker lifecycle calls fail closed without sending guessed V2 paths.
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
        raise AssertionError(f"unexpected path {request.url.path}")

    async def run() -> tuple[dict[str, Any], dict[str, Any], str | None]:
        client = _mock_client(handler)
        try:
            cap = await client.probe_capability()
            health = await client.health()
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.create_session("t", "/tmp/w")
            return cap, health, client.server_version
        finally:
            await client.close()

    cap, health, version = asyncio.run(run())
    assert cap["family"] == "v2"
    assert cap["legacy_available"] is False
    assert cap["v2_available"] is True
    assert version == "2.0.0"
    assert health == {"healthy": True, "version": "2.0.0"}
    assert "/api/session" not in seen
    assert seen.count("/api/health") == 2


def test_v2_lifecycle_fails_closed_without_guessed_paths() -> None:
    """V2 worker lifecycle fails closed; only the health envelope is used."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if path == "/api/health":
            return httpx.Response(200, json={"data": {"healthy": True}}, request=request)
        raise AssertionError(f"unexpected {request.method} {path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.probe_capability()
            assert client.api_family == "v2"
            health = await client.health()
            assert health == {"healthy": True}
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.list_providers()
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.get_session_status("/tmp/w")
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.list_messages("ses_1", "/tmp/w")
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.abort_session("ses_1", "/tmp/w")
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.delete_session("ses_1", "/tmp/w")
        finally:
            await client.close()

    asyncio.run(run())
    paths = [r.url.path for r in requests]
    assert paths == ["/global/health", "/api/health", "/api/health"]


def test_v2_prompt_async_never_sends_guessed_path() -> None:
    """V2 prompt_async fails closed instead of guessing prompt_async/prompt."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"data": {"healthy": True}}, request=request)
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.probe_capability()
            assert client.api_family == "v2"
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.prompt_async(
                    "ses_x",
                    "hi",
                    provider_id="acme",
                    model_id="m-1",
                    agent="plan",
                    directory="/tmp/w",
                )
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.send_message("ses_x", "hi", directory="/tmp/w")
        finally:
            await client.close()

    asyncio.run(run())
    assert seen == ["/global/health", "/api/health"]
    assert not any("prompt_async" in path for path in seen)
    assert not any(path.startswith("/api/session") for path in seen)


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


def test_v2_lifecycle_blocked_before_any_guessed_request() -> None:
    """V2 lifecycle raises before sending any guessed session/provider path."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"data": {"healthy": True}}, request=request)
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.probe_capability()
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.abort_session("ses_1", "/tmp/w")
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.list_sessions("/tmp/w")
            with pytest.raises(OpencodeError, match="V2 lifecycle adapter"):
                await client.get_diff("ses_1", directory="/tmp/w")
        finally:
            await client.close()

    asyncio.run(run())
    assert seen == ["/global/health", "/api/health"]


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
