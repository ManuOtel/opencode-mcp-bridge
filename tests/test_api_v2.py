"""Deterministic tests for the conservative OpenCode V2 data-plane adapter.

All tests run against httpx.MockTransport: no network. The fake V2 world
below mirrors the documented contracts at https://opencode.ai/v2/docs/api
(envelope shapes, required bodies, status codes) so every asserted route
is a verified route, never a guess.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge.opencode_client import (
    OpencodeClient,
    OpencodeError,
    _require_v2_data,
    _require_v2_info,
    _simplify_v2_message,
    extract_v2_text,
)

SECRET_CANARY = "sk-canary-secret-001"
PROMPT_CANARY = "canary-prompt-002"

V2_PROVIDERS = [
    {
        "id": "opencode",
        "name": "OpenCode",
        "activation": "enabled",
        "package": "opencode",
        "settings": {"apiKey": SECRET_CANARY},
        "headers": {"Authorization": SECRET_CANARY},
        "body": {"token": SECRET_CANARY},
    },
    {
        "id": "acme",
        "name": "Acme",
        "activation": "disabled",
        "package": "acme",
    },
]

V2_MODELS = [
    {
        "id": "opencode/muse-spark-1.3-contributor-free",
        "modelID": "muse-spark-1.3-contributor-free",
        "providerID": "opencode",
        "name": "Muse Spark Free",
        "settings": {"secret": SECRET_CANARY},
        "headers": {"Authorization": SECRET_CANARY},
        "body": {"token": SECRET_CANARY},
        "cost": [{"input": 0.0, "output": 0.0, "cache": {"read": 0.0, "write": 0.0}}],
    },
    {
        "id": "opencode/muse-spark-pro",
        "modelID": "muse-spark-pro",
        "providerID": "opencode",
        "name": "Muse Spark Pro",
        "cost": [{"input": 1.0, "output": 2.0, "cache": {"read": 0.5, "write": 1.0}}],
    },
    {
        "id": "acme/m-1",
        "modelID": "m-1",
        "providerID": "acme",
        "name": "Acme One",
        "cost": [],
    },
]

V2_INFO = {
    "version": "2.0.0",
    "pid": 123,
    "urls": ["http://127.0.0.1:4096"],
    "paths": {"tmp": "/tmp"},
}

V2_DEFAULT_MODEL = V2_MODELS[0]

V2_AGENTS = [
    {"id": "a-plan", "name": "plan", "mode": "primary", "description": "Planner"},
    {"id": "a-build", "name": "build", "mode": "all", "description": "x" * 500},
]

V2_SESSION = {
    "id": "ses_1",
    "projectID": "proj_1",
    "agent": "build",
    "model": {"providerID": "opencode", "id": "muse-spark-1.3-contributor-free"},
    "title": "demo",
    "time": {"created": 10, "updated": 20},
    "cost": 0.5,
    "tokens": {"input": 7, "output": 9},
    "location": {"directory": "/tmp/w"},
}

V2_MESSAGES = [
    {"id": "msg_1", "type": "user", "text": "hello", "time": {"created": 1}},
    {
        "id": "msg_2",
        "type": "assistant",
        "agent": "build",
        "model": {"providerID": "opencode", "id": "muse-spark-1.3-contributor-free"},
        "content": [
            {"type": "text", "text": "first"},
            {"type": "reasoning", "text": "hidden-thoughts"},
            {"type": "tool", "text": "ignored"},
            {"type": "text", "text": "second"},
        ],
        "time": {"created": 2},
        "finish": "stop",
    },
    {
        "id": "msg_3",
        "type": "assistant",
        "agent": "build",
        "model": {"providerID": "opencode", "id": "muse-spark-1.3-contributor-free"},
        "content": [{"type": "text", "text": "partial"}],
        "time": {"created": 3},
        "finish": "error",
        "error": {"type": "provider", "message": "boom"},
    },
    {"id": "msg_4", "type": "idle", "outcome": "succeeded", "time": {"created": 4}},
]

# Every V2 path the adapter may request. Anything else is a regression.
# GET /api/info is the documented capability probe (direct ServerInfo);
# the undocumented GET /api/health is never requested.
V2_ALLOWLIST = {
    "/global/health",
    "/api/info",
    "/api/provider",
    "/api/model",
    "/api/model/default",
    "/api/agent",
    "/api/session",
    "/api/session/active",
    "/api/session/ses_1",
    "/api/session/ses_1/model",
    "/api/session/ses_1/agent",
    "/api/session/ses_1/prompt",
    "/api/session/ses_1/message",
    "/api/session/ses_1/interrupt",
    "/api/session/ses_1/diff",
}

FORBIDDEN_SUBSTRINGS = ("prompt_async", "/session/status")


class V2World:
    """Fake V2 server recording every request for allowlist assertions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if path == "/api/info":
            return httpx.Response(200, json=dict(V2_INFO), request=request)
        if path == "/api/provider":
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": V2_PROVIDERS},
                request=request,
            )
        if path == "/api/model":
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": V2_MODELS},
                request=request,
            )
        if path == "/api/model/default":
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": V2_DEFAULT_MODEL},
                request=request,
            )
        if path == "/api/agent":
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": V2_AGENTS},
                request=request,
            )
        if path == "/api/session" and request.method == "POST":
            return httpx.Response(200, json={"data": V2_SESSION}, request=request)
        if path == "/api/session" and request.method == "GET":
            return httpx.Response(200, json={"data": [V2_SESSION], "cursor": {}}, request=request)
        if path == "/api/session/active":
            return httpx.Response(
                200, json={"data": {"ses_1": {"type": "running"}}}, request=request
            )
        if path == "/api/session/ses_1/model" and request.method == "POST":
            return httpx.Response(204, request=request)
        if path == "/api/session/ses_1/agent" and request.method == "POST":
            return httpx.Response(204, request=request)
        if path == "/api/session/ses_1/prompt" and request.method == "POST":
            return httpx.Response(
                200,
                json={"data": {"id": "msg_9", "sessionID": "ses_1"}},
                request=request,
            )
        if path == "/api/session/ses_1/message":
            return httpx.Response(200, json={"data": V2_MESSAGES, "cursor": {}}, request=request)
        if path == "/api/session/ses_1/interrupt":
            return httpx.Response(200, json={"interrupted": True}, request=request)
        if path == "/api/session/ses_1" and request.method == "GET":
            return httpx.Response(200, json={"data": V2_SESSION}, request=request)
        if path == "/api/session/ses_1" and request.method == "DELETE":
            return httpx.Response(204, request=request)
        if path == "/api/session/ses_1/diff":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "file": "a.py",
                            "patch": "@@",
                            "additions": 1,
                            "deletions": 0,
                            "status": "modified",
                        }
                    ]
                },
                request=request,
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def assert_allowlisted(self) -> None:
        for path in self.paths():
            assert path in V2_ALLOWLIST, f"unverified V2 path requested: {path}"
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in path, f"guessed path requested: {path}"
        assert not any(path.startswith("/session/") for path in self.paths())
        assert "/provider" not in self.paths()
        assert "/agent" not in self.paths()


def _mock_client(world: V2World) -> OpencodeClient:
    client = OpencodeClient("http://opencode", "u", "p")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(world.handler), base_url="http://opencode"
    )
    return client


def _bodies(world: V2World, path: str) -> list[Any]:
    out = []
    for request in world.requests:
        if request.url.path == path and request.content:
            out.append(json.loads(request.content.decode()))
    return out


def test_v2_provider_envelope_normalization() -> None:
    """Provider/model/default envelopes merge into the legacy shape."""
    world = V2World()

    async def run() -> tuple[dict[str, Any], dict[str, Any], str]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            assert client.api_family == "v2"
            summary = await client.list_providers("/tmp/w")
            raw = await client.get_providers_raw("/tmp/w")
            return summary, raw, client.default_directory
        finally:
            await client.close()

    summary, raw, _ = asyncio.run(run())
    assert summary["providers"] == [
        {
            "providerID": "opencode",
            "name": "OpenCode",
            "modelIDs": ["muse-spark-1.3-contributor-free", "muse-spark-pro"],
            "connected": True,
        },
        {
            "providerID": "acme",
            "name": "Acme",
            "modelIDs": ["m-1"],
            "connected": False,
        },
    ]
    assert summary["default"] == {
        "providerID": "opencode",
        "modelID": "muse-spark-1.3-contributor-free",
    }
    assert raw["connected"] == ["opencode"]
    assert raw["default"] == summary["default"]
    opencode_entry = next(p for p in raw["all"] if p["id"] == "opencode")
    free_entry = opencode_entry["models"]["muse-spark-1.3-contributor-free"]
    assert free_entry["name"] == "Muse Spark Free"
    assert free_entry["cost"] == {"input": 0.0, "output": 0.0}
    assert "m-1" in next(p for p in raw["all"] if p["id"] == "acme")["models"]
    # No provider secrets leak into normalized shapes.
    assert SECRET_CANARY not in json.dumps(summary)
    assert SECRET_CANARY not in json.dumps(raw)
    # Each snapshot route preserves location[directory] (httpx deepObject
    # encoding: literal "location[directory]" query key).
    for path in ("/api/provider", "/api/model", "/api/model/default"):
        matches = [r for r in world.requests if r.url.path == path]
        assert len(matches) == 2
        for request in matches:
            assert dict(request.url.params) == {"location[directory]": "/tmp/w"}
    world.assert_allowlisted()


def test_v2_provider_snapshot_uses_default_directory() -> None:
    """Omitting directory still sends the configured default location."""
    world = V2World()

    async def run() -> str:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            await client.list_providers()
            return client.default_directory
        finally:
            await client.close()

    default_dir = asyncio.run(run())
    for path in ("/api/provider", "/api/model", "/api/model/default"):
        matches = [r for r in world.requests if r.url.path == path]
        assert len(matches) == 1
        assert dict(matches[0].url.params) == {"location[directory]": default_dir}
    world.assert_allowlisted()


def test_v2_agent_envelope_normalization() -> None:
    """Agent envelope unwraps and keeps the legacy summary shape."""
    world = V2World()

    async def run() -> list[dict[str, Any]]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            return await client.list_agents("/tmp/w")
        finally:
            await client.close()

    agents = asyncio.run(run())
    assert agents[0] == {"name": "plan", "mode": "primary", "description": "Planner"}
    assert agents[1]["name"] == "build"
    assert len(agents[1]["description"]) == 300
    agent_requests = [r for r in world.requests if r.url.path == "/api/agent"]
    assert len(agent_requests) == 1
    assert dict(agent_requests[0].url.params) == {"location[directory]": "/tmp/w"}
    world.assert_allowlisted()


def test_v2_session_create_sends_location_body() -> None:
    """Create posts {title, location} and returns the Session.Info."""
    world = V2World()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            titled = await client.create_session("demo", "/tmp/w")
            untitled = await client.create_session(None, "/tmp/w")
            return titled, untitled
        finally:
            await client.close()

    titled, untitled = asyncio.run(run())
    assert titled["id"] == "ses_1"
    assert untitled["id"] == "ses_1"
    bodies = _bodies(world, "/api/session")
    assert bodies[0] == {"title": "demo", "location": {"directory": "/tmp/w"}}
    assert bodies[1] == {"location": {"directory": "/tmp/w"}}
    world.assert_allowlisted()


def test_v2_prompt_async_applies_bridge_default_model() -> None:
    """Prompt without overrides still selects the free-first bridge model."""
    world = V2World()

    async def run() -> bool:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            return await client.prompt_async("ses_1", PROMPT_CANARY, directory="/tmp/w")
        finally:
            await client.close()

    assert asyncio.run(run()) is True
    model_bodies = _bodies(world, "/api/session/ses_1/model")
    assert model_bodies == [
        {
            "model": {
                "providerID": "opencode",
                "id": "muse-spark-1.3-contributor-free",
            }
        }
    ]
    assert _bodies(world, "/api/session/ses_1/agent") == []
    assert _bodies(world, "/api/session/ses_1/prompt") == [{"text": PROMPT_CANARY}]
    assert world.paths().count("/api/session/ses_1/prompt") == 1
    world.assert_allowlisted()


def test_v2_prompt_async_with_model_agent_overrides() -> None:
    """Explicit overrides travel via the switch routes, then prompt."""
    world = V2World()

    async def run() -> bool:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            return await client.prompt_async(
                "ses_1",
                "hi",
                provider_id="acme",
                model_id="m-1",
                agent="plan",
                directory="/tmp/w",
            )
        finally:
            await client.close()

    assert asyncio.run(run()) is True
    assert _bodies(world, "/api/session/ses_1/model") == [
        {"model": {"providerID": "acme", "id": "m-1"}}
    ]
    assert _bodies(world, "/api/session/ses_1/agent") == [{"agent": "plan"}]
    assert _bodies(world, "/api/session/ses_1/prompt") == [{"text": "hi"}]
    world.assert_allowlisted()


def test_v2_prompt_async_rejects_split_model_pair_before_http() -> None:
    """Half model overrides raise ValueError without extra requests."""
    world = V2World()

    async def run() -> int:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            before = len(world.requests)
            with pytest.raises(ValueError, match="together"):
                await client.prompt_async("ses_1", "hi", provider_id="acme", directory="/tmp/w")
            return len(world.requests) - before
        finally:
            await client.close()

    assert asyncio.run(run()) == 0
    world.assert_allowlisted()


def test_v2_active_status_mapping() -> None:
    """Active sessions normalize to {type: busy}; absent ones have no entry."""
    world = V2World()

    async def run() -> dict[str, Any]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            return await client.get_session_status("/tmp/w")
        finally:
            await client.close()

    assert asyncio.run(run()) == {"ses_1": {"type": "busy"}}
    world.assert_allowlisted()


def test_v2_message_mapping_and_assistant_extraction() -> None:
    """Projected messages map to {id, role, text, time}; text skips noise."""
    assert extract_v2_text([{"type": "reasoning", "text": "x"}]) == ""
    assert extract_v2_text("nope") == ""
    simplified = _simplify_v2_message(
        {"id": "m", "type": "user", "text": "hi", "time": {"created": 1}}
    )
    assert simplified == {"id": "m", "role": "user", "text": "hi", "time": {"created": 1}}

    world = V2World()

    async def run() -> list[dict[str, Any]]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            return await client.list_messages("ses_1", "/tmp/w", limit=50)
        finally:
            await client.close()

    messages = asyncio.run(run())
    assert [(m["id"], m["role"]) for m in messages] == [
        ("msg_1", "user"),
        ("msg_2", "assistant"),
        ("msg_3", "assistant"),
        ("msg_4", "idle"),
    ]
    assert messages[0]["text"] == "hello"
    assert messages[1]["text"] == "first\nsecond"
    assert "hidden-thoughts" not in messages[1]["text"]
    # Only the documented limit parameter is sent; no directory guess.
    message_requests = [r for r in world.requests if r.url.path.endswith("/message")]
    assert len(message_requests) == 1
    assert dict(message_requests[0].url.params) == {"limit": "50"}
    world.assert_allowlisted()


def test_v2_latest_assistant_reports_error_and_caps() -> None:
    """Newest assistant wins; error/finish surface; max_chars caps text."""
    world = V2World()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            full = await client.get_latest_assistant("ses_1", "/tmp/w")
            capped = await client.get_latest_assistant("ses_1", "/tmp/w", max_chars=3)
            return full, capped
        finally:
            await client.close()

    full, capped = asyncio.run(run())
    assert full == {
        "messageID": "msg_3",
        "text": "partial",
        "total_chars": 7,
        "has_error": True,
    }
    assert capped["text"] == "par"
    assert capped["total_chars"] == 7
    world.assert_allowlisted()


def test_v2_session_list_and_get_simplify_location() -> None:
    """Session list/get unwrap data and map location.directory."""
    world = V2World()

    async def run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            sessions = await client.list_sessions("/tmp/w", limit=10)
            one = await client.get_session("ses_1", "/tmp/w")
            return sessions, one
        finally:
            await client.close()

    sessions, one = asyncio.run(run())
    assert sessions == [
        {
            "id": "ses_1",
            "title": "demo",
            "directory": "/tmp/w",
            "agent": "build",
            "model": {"providerID": "opencode", "id": "muse-spark-1.3-contributor-free"},
            "time": {"created": 10, "updated": 20},
            "cost": 0.5,
        }
    ]
    assert one == sessions[0]
    list_requests = [r for r in world.requests if r.url.path == "/api/session"]
    assert dict(list_requests[0].url.params) == {"directory": "/tmp/w", "limit": "10"}
    world.assert_allowlisted()


def test_v2_interrupt_delete_diff() -> None:
    """Interrupt/delete/diff use verified routes with documented params."""
    world = V2World()

    async def run() -> tuple[bool, bool, list[dict[str, Any]]]:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            aborted = await client.abort_session("ses_1", "/tmp/w")
            deleted = await client.delete_session("ses_1", "/tmp/w")
            diff = await client.get_diff("ses_1", message_id="msg_1", directory="/tmp/w")
            return aborted, deleted, diff
        finally:
            await client.close()

    aborted, deleted, diff = asyncio.run(run())
    assert aborted is True
    assert deleted is True
    assert diff == [
        {
            "file": "a.py",
            "patch": "@@",
            "additions": 1,
            "deletions": 0,
            "status": "modified",
        }
    ]
    diff_requests = [r for r in world.requests if r.url.path == "/api/session/ses_1/diff"]
    assert dict(diff_requests[0].url.params) == {"from": "msg_1"}
    interrupt_posts = [
        r
        for r in world.requests
        if r.url.path == "/api/session/ses_1/interrupt" and r.method == "POST"
    ]
    assert len(interrupt_posts) == 1
    world.assert_allowlisted()


def test_v2_send_message_fails_closed_before_http() -> None:
    """Sync send has no V2 mapping: precise error, no request sent."""
    world = V2World()

    async def run() -> int:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            before = len(world.requests)
            with pytest.raises(OpencodeError, match="no verified sync-reply mapping"):
                await client.send_message("ses_1", "hi", directory="/tmp/w")
            return len(world.requests) - before
        finally:
            await client.close()

    assert asyncio.run(run()) == 0
    world.assert_allowlisted()


def test_v2_full_worker_loop_stays_allowlisted() -> None:
    """End-to-end worker loop touches only verified V2 routes."""
    world = V2World()

    async def run() -> None:
        client = _mock_client(world)
        try:
            await client.probe_capability()
            await client.health()
            await client.get_providers_raw()
            await client.list_agents("/tmp/w")
            session = await client.create_session("job", "/tmp/w")
            assert session["id"] == "ses_1"
            assert await client.prompt_async("ses_1", "work", directory="/tmp/w")
            assert await client.get_session_status("/tmp/w") == {"ses_1": {"type": "busy"}}
            latest = await client.get_latest_assistant("ses_1", "/tmp/w")
            assert latest["messageID"] == "msg_3"
            assert await client.list_messages("ses_1", "/tmp/w") != []
            assert await client.get_session("ses_1", "/tmp/w") != {}
            assert await client.list_sessions("/tmp/w") != []
            assert await client.get_diff("ses_1", directory="/tmp/w") != []
            assert await client.abort_session("ses_1", "/tmp/w") is True
            assert await client.delete_session("ses_1", "/tmp/w") is True
        finally:
            await client.close()

    asyncio.run(run())
    world.assert_allowlisted()


def test_v2_malformed_envelopes_fail_closed() -> None:
    """Wrong envelope shapes raise instead of returning guessed data."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/info":
            return httpx.Response(200, json=dict(V2_INFO), request=request)
        if request.url.path == "/api/provider":
            return httpx.Response(200, json={"nodata": []}, request=request)
        if request.url.path in ("/api/model", "/api/model/default"):
            return httpx.Response(
                200,
                json={"location": {"directory": "/tmp/w"}, "data": []},
                request=request,
            )
        if request.url.path == "/api/session/ses_1/interrupt":
            return httpx.Response(200, json={"data": {"oops": 1}}, request=request)
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"data": ["not-a-session"]}, request=request)
        raise AssertionError(f"unexpected {request.url.path}")

    async def run() -> None:
        client = OpencodeClient("http://opencode", "u", "p")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        try:
            await client.probe_capability()
            with pytest.raises(OpencodeError, match="unsupported V2 envelope"):
                await client.list_providers()
            with pytest.raises(OpencodeError, match="unsupported V2 interrupt"):
                await client.abort_session("ses_1", "/tmp/w")
            with pytest.raises(OpencodeError, match="unsupported V2 session"):
                await client.create_session("t", "/tmp/w")
            with pytest.raises(OpencodeError, match="unsupported V2 envelope"):
                _require_v2_data({"nodata": 1}, "GET", "/api/session")
        finally:
            await client.close()

    asyncio.run(run())


def test_v2_errors_never_echo_secrets_or_prompts() -> None:
    """V2 transport errors stay redacted like legacy ones."""
    secret_password = "secret-pw-009"
    seen: list[str] = []

    def probe_fail_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404, json={}, request=request)

    def v2_fail_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/info":
            return httpx.Response(200, json=dict(V2_INFO), request=request)
        return httpx.Response(500, text="boom", request=request)

    async def run() -> str:
        client = OpencodeClient("http://opencode", "u", secret_password)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(probe_fail_handler),
            base_url="http://opencode",
        )
        try:
            with pytest.raises(OpencodeError) as excinfo:
                await client.probe_capability()
            assert secret_password not in str(excinfo.value)
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(v2_fail_handler),
                base_url="http://opencode",
            )
            await client.probe_capability(force_refresh=True)
            assert client.api_family == "v2"
            with pytest.raises(OpencodeError) as prompt_exc:
                await client.prompt_async("ses_1", PROMPT_CANARY, directory="/tmp/w")
            message = str(prompt_exc.value)
            assert secret_password not in message
            assert PROMPT_CANARY not in message
            return message
        finally:
            await client.close()

    asyncio.run(run())
    assert "/api/session/ses_1/model" in seen
    assert not any("prompt_async" in path for path in seen)


def test_v2_capability_probe_uses_info_direct_shape() -> None:
    """Regression: probe is GET /api/info with a direct ServerInfo body.

    Per the official V2 OpenAPI, GET /api/info returns ServerInfo
    {version, pid, urls, paths} directly: no {data} envelope and no
    healthy field. The undocumented GET /api/health must never fire.
    """
    world = V2World()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        client = _mock_client(world)
        try:
            cap = await client.probe_capability()
            health = await client.health()
            return cap, health
        finally:
            await client.close()

    cap, health = asyncio.run(run())
    assert cap["family"] == "v2"
    assert cap["version"] == "2.0.0"
    assert world.paths().count("/api/info") == 1
    assert "/api/health" not in world.paths()
    assert health == V2_INFO
    assert "data" not in health
    assert "healthy" not in health
    assert set(health) == {"version", "pid", "urls", "paths"}
    world.assert_allowlisted()


def test_v2_interrupt_accepts_direct_shape_rejects_envelope() -> None:
    """Regression: interrupt takes the direct {interrupted: bool} shape.

    Per the official V2 OpenAPI, POST /api/session/{id}/interrupt
    returns SessionInterruptResponse directly. A legacy-style {data}
    envelope, a missing flag, or a non-bool flag fails closed.
    """
    seen: list[tuple[str, str]] = []

    async def run_once(payload: Any) -> bool:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path == "/global/health":
                return httpx.Response(404, json={}, request=request)
            if request.url.path == "/api/info":
                return httpx.Response(200, json=dict(V2_INFO), request=request)
            if request.url.path == "/api/session/ses_1/interrupt":
                return httpx.Response(200, json=payload, request=request)
            raise AssertionError(f"unexpected {request.url.path}")

        client = OpencodeClient("http://opencode", "u", "p")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        try:
            await client.probe_capability()
            return await client.abort_session("ses_1", "/tmp/w")
        finally:
            await client.close()

    assert asyncio.run(run_once({"interrupted": True})) is True
    assert asyncio.run(run_once({"interrupted": False})) is True
    for bad in (
        {"data": {"interrupted": True}},
        {"oops": 1},
        {"interrupted": "yes"},
    ):
        with pytest.raises(OpencodeError, match="unsupported V2 interrupt"):
            asyncio.run(run_once(bad))
    assert ("POST", "/api/session/ses_1/interrupt") in seen
    assert not any(path == "/api/health" for _, path in seen)


def test_v2_info_shape_validation_rejects_malformed() -> None:
    """Regression: GET /api/info must be a full direct ServerInfo object.

    Per the official V2 OpenAPI, ServerInfo requires version (string),
    pid (integer), urls (array), and paths (object containing tmp).
    A {data} envelope, an empty dict, missing fields, or wrong types
    fail closed and never select V2.
    """
    good = dict(V2_INFO)
    assert _require_v2_info(dict(good), "GET", "/api/info") == good

    bad_payloads: list[Any] = [
        {},
        {"data": dict(V2_INFO)},
        {"version": "2.0.0"},
        {**good, "version": 123},
        {**good, "pid": "123"},
        {**good, "pid": True},
        {**good, "urls": "http://127.0.0.1:4096"},
        {**good, "paths": {}},
        {**good, "paths": []},
        {**good, "paths": {"tmp": 123}},
        {k: v for k, v in good.items() if k != "version"},
        {k: v for k, v in good.items() if k != "pid"},
        {k: v for k, v in good.items() if k != "urls"},
        {k: v for k, v in good.items() if k != "paths"},
    ]
    for bad in bad_payloads:
        with pytest.raises(OpencodeError, match="unsupported V2 ServerInfo"):
            _require_v2_info(bad, "GET", "/api/info")
    with pytest.raises(OpencodeError, match="unsupported V2 ServerInfo"):
        _require_v2_info(["not-a-server-info"], "GET", "/api/info")

    async def run_once(payload: Any) -> list[str]:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/global/health":
                return httpx.Response(404, json={}, request=request)
            if request.url.path == "/api/info":
                return httpx.Response(200, json=payload, request=request)
            raise AssertionError(f"unexpected {request.url.path}")

        client = OpencodeClient("http://opencode", "u", "p")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        try:
            with pytest.raises(OpencodeError, match="unsupported V2 ServerInfo"):
                await client.probe_capability()
            assert client.api_family == "legacy"
            assert client.capability is None
            return seen
        finally:
            await client.close()

    for bad in bad_payloads:
        seen = asyncio.run(run_once(bad))
        assert seen == ["/global/health", "/api/info"]
        assert "/api/health" not in seen
