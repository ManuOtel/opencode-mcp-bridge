"""Smoke tests for config, client helpers, and auth middleware. No network."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import config, server
from opencode_mcp_bridge.opencode_client import (
    OpencodeClient,
    OpencodeError,
    extract_text,
    simplify_message,
)
from opencode_mcp_bridge.server import BearerAuthMiddleware, _truncate


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "OPENCODE_BASE_URL",
        "OPENCODE_SERVER_USERNAME",
        "OPENCODE_SERVER_PASSWORD",
        "MCP_BEARER_TOKEN",
        "MCP_BEARER_TOKEN_SECONDARY",
        "MCP_HOST",
        "MCP_PORT",
        "DEFAULT_DIRECTORY",
        "ALLOWED_DIRECTORIES",
        "DEFAULT_PROVIDER_ID",
        "DEFAULT_MODEL_ID",
        "EXEC_TIMEOUT_S",
        "EXEC_MAX_OUTPUT_CHARS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_load_settings_requires_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing bearer token must fail fast."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
        config.load_settings()


def test_load_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults apply when only secrets are set."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")
    settings = config.load_settings()
    assert settings.opencode_base_url == "http://127.0.0.1:4096"
    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 8087
    assert settings.default_directory == os.path.expanduser("~")
    assert settings.default_provider_id == "opencode"
    assert settings.default_model_id == "muse-spark-1.3-contributor-free"


def test_load_settings_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model defaults can be changed through environment variables."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")
    monkeypatch.setenv("DEFAULT_PROVIDER_ID", "custom-provider")
    monkeypatch.setenv("DEFAULT_MODEL_ID", "custom-model")
    settings = config.load_settings()
    assert settings.default_provider_id == "custom-provider"
    assert settings.default_model_id == "custom-model"


def test_load_settings_rejects_bad_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-numeric ports must fail with a clear error."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")
    monkeypatch.setenv("MCP_PORT", "not-a-port")
    with pytest.raises(RuntimeError, match="Invalid numeric"):
        config.load_settings()


def test_extract_text_joins_and_truncates() -> None:
    """Text parts join; over-cap output gets a marker."""
    parts = [
        {"type": "text", "text": "hello"},
        {"type": "tool", "text": "ignored? no, type matters"},
        {"type": "text", "text": "world"},
    ]
    assert extract_text(parts) == "hello\nworld"
    long_text = "x" * 100
    assert extract_text([{"type": "text", "text": long_text}], max_chars=10).endswith("chars]")


def test_simplify_message_shape() -> None:
    """Message reduction keeps id/role/text/time."""
    item = {
        "info": {"id": "msg_1", "role": "assistant", "time": {"created": 1}},
        "parts": [{"type": "text", "text": "done"}],
    }
    assert simplify_message(item) == {
        "id": "msg_1",
        "role": "assistant",
        "text": "done",
        "time": {"created": 1},
    }


def test_send_message_rejects_half_model() -> None:
    """Message model override needs both provider and model IDs."""
    client = OpencodeClient("http://127.0.0.1:9", "u", "p")
    with pytest.raises(ValueError, match="together"):
        asyncio.run(client.send_message("ses_x", "hi", model_id="only-model"))
    asyncio.run(client.close())


def test_send_message_uses_configured_model_defaults() -> None:
    """Omitted model overrides are sent to OpenCode explicitly."""
    requests = []

    async def run() -> None:
        client = OpencodeClient(
            "http://opencode",
            "u",
            "p",
            default_provider_id="default-provider",
            default_model_id="default-model",
        )
        await client._client.aclose()

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"info": {"id": "msg_1"}, "parts": [{"type": "text", "text": "ok"}]},
                request=request,
            )

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        try:
            await client.send_message("ses_x", "hello")
        finally:
            await client.close()

    asyncio.run(run())
    assert requests[0].read() == (
        b'{"parts":[{"type":"text","text":"hello"}],'
        b'"model":{"providerID":"default-provider","modelID":"default-model"}}'
    )


def test_send_message_returns_normalized_model_metadata() -> None:
    """Live info fields are normalized without dropping an existing model object."""
    payloads = [
        (
            {
                "info": {
                    "id": "msg_1",
                    "providerID": "opencode",
                    "modelID": "muse-spark-1.3-contributor-free",
                },
                "parts": [{"type": "text", "text": "ok"}],
            },
            {"providerID": "opencode", "modelID": "muse-spark-1.3-contributor-free"},
        ),
        (
            {
                "info": {"id": "msg_2", "model": {"providerID": "custom", "modelID": "custom-1"}},
                "parts": [{"type": "text", "text": "ok"}],
            },
            {"providerID": "custom", "modelID": "custom-1"},
        ),
    ]

    async def run(payload: dict) -> dict:
        client = OpencodeClient("http://opencode", "u", "p")
        await client._client.aclose()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        try:
            return await client.send_message("ses_x", "hello")
        finally:
            await client.close()

    for payload, expected_model in payloads:
        assert asyncio.run(run(payload))["model"] == expected_model


@pytest.mark.parametrize("argument", [{"message": "natural"}, {"prompt": "legacy"}])
def test_server_send_message_accepts_message_or_prompt(
    monkeypatch: pytest.MonkeyPatch, argument: dict[str, str]
) -> None:
    """The MCP alias is translated to the client's single message argument."""
    calls = []

    class FakeClient:
        async def send_message(self, *args: object) -> dict[str, str]:
            calls.append(args)
            return {"text": "ok"}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = asyncio.run(server.send_message("ses_x", **argument))
    assert result == {"text": "ok"}
    assert calls[0][0:2] == ("ses_x", next(iter(argument.values())))


def test_server_send_message_requires_exactly_one_text_argument() -> None:
    """The MCP interface rejects missing and duplicate message aliases."""

    async def run() -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            await server.send_message("ses_x")
        with pytest.raises(ValueError, match="Exactly one"):
            await server.send_message("ses_x", message="one", prompt="two")

    asyncio.run(run())


def test_create_session_only_sends_supported_fields() -> None:
    """Session creation sends title in the body and directory in the query only."""
    requests = []

    async def run() -> None:
        client = OpencodeClient("http://opencode", "u", "p")
        await client._client.aclose()

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "ses_1"}, request=request)

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        try:
            await client.create_session("Test session", "/tmp/project")
        finally:
            await client.close()

    asyncio.run(run())
    assert requests[0].content == b'{"title":"Test session"}'
    assert dict(requests[0].url.params) == {"directory": "/tmp/project"}


@pytest.mark.parametrize(
    "payload, expected_snippet",
    [
        (
            {"info": {"error": {"name": "ProviderError", "data": {"message": "upstream failed"}}}},
            "upstream failed",
        ),
        ({"info": {"role": "assistant"}, "parts": [{"type": "tool"}]}, "no usable text"),
    ],
)
def test_send_message_rejects_empty_success_response(payload: dict, expected_snippet: str) -> None:
    """A successful HTTP response must still contain a usable assistant result."""

    async def run() -> None:
        client = OpencodeClient("http://opencode", "u", "p")
        await client._client.aclose()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://opencode",
        )
        try:
            with pytest.raises(OpencodeError, match=expected_snippet):
                await client.send_message("ses_x", "hi")
        finally:
            await client.close()

    asyncio.run(run())


def test_truncate_marks() -> None:
    """Truncation appends the dropped char count."""
    assert _truncate("abcdef", 4) == "abcd\n...[truncated 2 chars]"
    assert _truncate("abc", 4) == "abc"


def _scope(path: str, token: str | None, method: str = "GET") -> dict:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {"type": "http", "path": path, "method": method, "headers": headers}


def test_bearer_middleware_allows_health_without_token() -> None:
    """Health stays open for Traefik checks."""
    calls = []

    async def downstream(scope: dict, receive: object, send: object) -> None:
        calls.append(scope["path"])

    async def run() -> None:
        middleware = BearerAuthMiddleware(downstream, "secret")
        await middleware(_scope("/health", None), None, None)

    asyncio.run(run())
    assert calls == ["/health"]


def test_bearer_middleware_rejects_bad_token() -> None:
    """Wrong tokens get 401 and never reach the app."""
    calls = []
    statuses = []

    async def downstream(scope: dict, receive: object, send: object) -> None:
        calls.append(True)

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def run() -> None:
        middleware = BearerAuthMiddleware(downstream, "secret")
        await middleware(_scope("/mcp", "wrong"), None, send)

    asyncio.run(run())
    assert calls == []
    assert statuses == [401]


def test_bearer_middleware_accepts_good_token() -> None:
    """Correct token passes through to the MCP app."""
    calls = []

    async def downstream(scope: dict, receive: object, send: object) -> None:
        calls.append(True)

    async def run() -> None:
        middleware = BearerAuthMiddleware(downstream, "secret")
        await middleware(_scope("/mcp", "secret"), None, None)

    asyncio.run(run())
    assert calls == [True]
