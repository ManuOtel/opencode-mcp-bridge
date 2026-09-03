"""Smoke tests for config, client helpers, and auth middleware. No network."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import config
from opencode_mcp_bridge.opencode_client import OpencodeClient, extract_text, simplify_message
from opencode_mcp_bridge.server import BearerAuthMiddleware, _truncate


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "OPENCODE_BASE_URL",
        "OPENCODE_SERVER_USERNAME",
        "OPENCODE_SERVER_PASSWORD",
        "MCP_BEARER_TOKEN",
        "MCP_HOST",
        "MCP_PORT",
        "DEFAULT_DIRECTORY",
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


def test_create_session_rejects_half_model() -> None:
    """Model override needs both provider and model IDs."""
    client = OpencodeClient("http://127.0.0.1:9", "u", "p")
    with pytest.raises(ValueError, match="together"):
        asyncio.run(client.create_session(provider_id="only-provider"))
    asyncio.run(client.close())


def test_send_message_rejects_half_model() -> None:
    """Message model override needs both provider and model IDs."""
    client = OpencodeClient("http://127.0.0.1:9", "u", "p")
    with pytest.raises(ValueError, match="together"):
        asyncio.run(client.send_message("ses_x", "hi", model_id="only-model"))
    asyncio.run(client.close())


def test_truncate_marks() -> None:
    """Truncation appends the dropped char count."""
    assert _truncate("abcdef", 4) == "abcd\n...[truncated 2 chars]"
    assert _truncate("abc", 4) == "abc"


def _scope(path: str, token: str | None) -> dict:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {"type": "http", "path": path, "headers": headers}


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
