"""Focused tests for worker orchestration: async prompt, status, catalog. No network."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server
from opencode_mcp_bridge.opencode_client import OpencodeClient, OpencodeError
from opencode_mcp_bridge.server import (
    _is_free_model,
    _map_worker_state,
)


def _mock_client(handler: Any) -> OpencodeClient:
    """Build a client backed by a mock transport."""
    client = OpencodeClient("http://opencode", "u", "p")
    # The default httpx client never made a request, so swapping it out
    # without closing leaks no connections and keeps helpers sync.
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://opencode"
    )
    return client


def test_prompt_async_path_payload_and_defaults() -> None:
    """Async prompt hits prompt_async with directory param and default model."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    async def run() -> bool:
        client = _mock_client(handler)
        try:
            return await client.prompt_async("ses_x", "hello", directory="/tmp/w")
        finally:
            await client.close()

    assert asyncio.run(run()) is True
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/session/ses_x/prompt_async"
    assert dict(request.url.params) == {"directory": "/tmp/w"}
    assert json.loads(request.content) == {
        "parts": [{"type": "text", "text": "hello"}],
        "model": {"providerID": "opencode", "modelID": "muse-spark-1.3-contributor-free"},
    }


def test_prompt_async_overrides_and_agent() -> None:
    """Explicit provider/model/agent overrides are sent through."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    async def run() -> None:
        client = _mock_client(handler)
        try:
            await client.prompt_async(
                "ses_x", "hi", provider_id="acme", model_id="m-1", agent="plan"
            )
        finally:
            await client.close()

    asyncio.run(run())
    assert json.loads(requests[0].content) == {
        "parts": [{"type": "text", "text": "hi"}],
        "model": {"providerID": "acme", "modelID": "m-1"},
        "agent": "plan",
    }


def test_prompt_async_rejects_half_model() -> None:
    """Async prompt needs both provider and model IDs or neither."""
    client = OpencodeClient("http://127.0.0.1:9", "u", "p")
    with pytest.raises(ValueError, match="together"):
        asyncio.run(client.prompt_async("ses_x", "hi", provider_id="only-provider"))
    asyncio.run(client.close())


def test_get_session_status_path_and_shape() -> None:
    """Status polls GET /session/status and returns the raw map."""
    requests: list[httpx.Request] = []
    payload = {"ses_1": {"type": "busy"}, "ses_2": {"type": "idle"}}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload, request=request)

    async def run() -> dict[str, Any]:
        client = _mock_client(handler)
        try:
            return await client.get_session_status("/tmp/w")
        finally:
            await client.close()

    assert asyncio.run(run()) == payload
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/session/status"
    assert dict(requests[0].url.params) == {"directory": "/tmp/w"}


def test_get_latest_assistant_picks_last_and_flags_error() -> None:
    """Latest assistant text wins; provider errors set the flag."""
    payload = [
        {
            "info": {"id": "m1", "role": "user"},
            "parts": [{"type": "text", "text": "go"}],
        },
        {
            "info": {"id": "m2", "role": "assistant"},
            "parts": [{"type": "text", "text": "old"}],
        },
        {
            "info": {"id": "m3", "role": "assistant", "error": {"name": "ApiError"}},
            "parts": [{"type": "text", "text": "boom"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    async def run() -> dict[str, Any]:
        client = _mock_client(handler)
        try:
            return await client.get_latest_assistant("ses_x")
        finally:
            await client.close()

    assert asyncio.run(run()) == {
        "messageID": "m3",
        "text": "boom",
        "total_chars": 4,
        "has_error": True,
    }


def test_get_latest_assistant_respects_max_chars_over_20k() -> None:
    """No hidden 20k cap: full text is measured, returned text honors max_chars."""
    big = "z" * 25000
    payload = [
        {
            "info": {"id": "m1", "role": "assistant"},
            "parts": [{"type": "text", "text": big}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        client = _mock_client(handler)
        try:
            capped = await client.get_latest_assistant("ses_x", max_chars=12000)
            full = await client.get_latest_assistant("ses_x")
            return capped, full
        finally:
            await client.close()

    capped, full = asyncio.run(run())
    assert len(capped["text"]) == 12000
    assert capped["total_chars"] == 25000
    assert len(full["text"]) == 25000
    assert full["total_chars"] == 25000


def test_get_latest_assistant_empty_error_is_not_error() -> None:
    """Falsy error payloads are not errors, matching send_message truthiness."""
    payload = [
        {
            "info": {"id": "m1", "role": "assistant", "error": {}},
            "parts": [{"type": "text", "text": "ok"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    async def run() -> dict[str, Any]:
        client = _mock_client(handler)
        try:
            return await client.get_latest_assistant("ses_x")
        finally:
            await client.close()

    assert asyncio.run(run())["has_error"] is False


def test_resolve_model_defaults_overrides_and_pairing() -> None:
    """Public helper resolves defaults and rejects half pairs."""
    client = OpencodeClient("http://127.0.0.1:9", "u", "p")
    assert client.resolve_model(None, None) == (
        "opencode",
        "muse-spark-1.3-contributor-free",
    )
    assert client.resolve_model("acme", "m-1") == ("acme", "m-1")
    with pytest.raises(ValueError, match="together"):
        client.resolve_model("acme", None)
    asyncio.run(client.close())


def test_send_message_provider_error_status_zero_and_string_data() -> None:
    """Provider-level failures use status 0 and keep string error data."""
    payload = {"info": {"error": {"name": "ApiError", "data": "upstream exploded"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    async def run() -> OpencodeError:
        client = _mock_client(handler)
        try:
            with pytest.raises(OpencodeError) as exc_info:
                await client.send_message("ses_x", "hi")
            return exc_info.value
        finally:
            await client.close()

    error = asyncio.run(run())
    assert error.status == 0
    assert "upstream exploded" in str(error)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"type": "busy"}, "running"),
        ({"type": "retry"}, "running"),
        ({"type": "idle"}, "idle"),
        ({"type": "error"}, "error"),
        ({"type": "failed"}, "error"),
        ({"type": "weird"}, "unknown"),
        ({}, "unknown"),
        (None, "unknown"),
        ("busy", "running"),
        (" IDLE ", "idle"),
    ],
)
def test_map_worker_state(raw: Any, expected: str) -> None:
    """Opencode statuses collapse to the stable worker state set."""
    assert _map_worker_state(raw) == expected


@pytest.mark.parametrize(
    "model_id, name, cost, expected",
    [
        ("muse-spark-1.3-contributor-free", "Spark", None, True),
        ("gpt-5", "GPT-5 FREE tier", None, True),
        ("gpt-5", "GPT-5", {"input": 0, "output": 0}, True),
        ("gpt-5", "GPT-5", {"input": 1, "output": 0}, False),
        ("gpt-5", "GPT-5", None, False),
        ("gpt-5", "GPT-5", {"input": 0}, False),
        ("gpt-5", "GPT-5", {"input": "x", "output": 0}, False),
    ],
)
def test_is_free_model(model_id: str, name: str, cost: Any, expected: bool) -> None:
    """Free means a free marker or zero input/output cost metadata."""
    assert _is_free_model(model_id, name, cost) is expected


class _FakeWorkerClient:
    """Minimal fake for worker tool tests."""

    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"
    default_directory = "/home/tester"

    def __init__(self) -> None:
        self.created: list[tuple[Any, Any]] = []
        self.prompted: list[tuple[Any, ...]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.prompt_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.latest_caps: list[Any] = []
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }
        self.providers_raw: dict[str, Any] = {}

    def resolve_model(self, provider_id: Any, model_id: Any) -> tuple[str, str]:
        if bool(provider_id) != bool(model_id):
            raise ValueError("provider_id and model_id must be given together or omitted")
        return (
            provider_id or self.default_provider_id,
            model_id or self.default_model_id,
        )

    async def create_session(self, title: Any, directory: Any) -> dict[str, Any]:
        self.created.append((title, directory))
        return {"id": "ses_1", "title": title, "directory": directory or "/home/tester"}

    async def prompt_async(self, *args: Any, **kwargs: Any) -> bool:
        self.prompted.append(args)
        if self.prompt_error is not None:
            raise self.prompt_error
        return True

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        self.deleted.append((session_id, directory))
        if self.delete_error is not None:
            raise self.delete_error
        return True

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.latest_caps.append(kwargs.get("max_chars"))
        return self.latest

    async def get_providers_raw(self) -> dict[str, Any]:
        return self.providers_raw


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> _FakeWorkerClient:
    fake = _FakeWorkerClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def test_worker_run_compact_result_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker_run creates, prompts async, and returns compact fields."""
    fake = _patch_client(monkeypatch)
    result = asyncio.run(server.worker_run("do the thing", title="job-1"))
    assert result["taskID"] == "ses_1"
    assert result["sessionID"] == "ses_1"
    assert result["state"] == "running"
    assert result["providerID"] == "opencode"
    assert result["modelID"] == "muse-spark-1.3-contributor-free"
    assert result["directory"] == "/home/tester"
    assert result["title"] == "job-1"
    assert fake.created == [("job-1", None)]
    assert fake.prompted[0][0:2] == ("ses_1", "do the thing")


def test_worker_run_passes_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit model choices reach prompt_async and the result."""
    fake = _patch_client(monkeypatch)
    result = asyncio.run(server.worker_run("hi", providerID="acme", modelID="m-1", agent="plan"))
    assert (result["providerID"], result["modelID"]) == ("acme", "m-1")
    assert fake.prompted[0][2:5] == ("acme", "m-1", "plan")


def test_worker_run_rejects_half_model_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid model pairing fails before any session is created."""
    fake = _patch_client(monkeypatch)
    with pytest.raises(ValueError, match="together"):
        asyncio.run(server.worker_run("hi", providerID="only-provider"))
    assert fake.created == []
    assert fake.prompted == []


def test_worker_run_cleans_up_session_on_prompt_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed async prompt deletes the new session, then re-raises."""
    fake = _patch_client(monkeypatch)
    original = OpencodeError("POST", "/session/ses_1/prompt_async", 500, "boom")
    fake.prompt_error = original
    with pytest.raises(OpencodeError, match="boom") as exc_info:
        asyncio.run(server.worker_run("hi", directory="/tmp/w"))
    assert exc_info.value is original
    assert fake.deleted == [("ses_1", "/tmp/w")]


def test_worker_run_cleanup_failure_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing cleanup must not hide the original prompt error."""
    fake = _patch_client(monkeypatch)
    original = OpencodeError("POST", "/session/ses_1/prompt_async", 500, "original")
    fake.prompt_error = original
    fake.delete_error = OpencodeError("DELETE", "/session/ses_1", 500, "cleanup")
    with pytest.raises(OpencodeError, match="original") as exc_info:
        asyncio.run(server.worker_run("hi"))
    assert exc_info.value is original
    assert fake.deleted == [("ses_1", None)]


def test_worker_status_running_with_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Busy maps to running with the latest assistant text only."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {"ses_1": {"type": "busy"}}
    fake.latest = {"messageID": "m9", "text": "almost done", "total_chars": 11, "has_error": False}
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["taskID"] == "ses_1"
    assert result["sessionID"] == "ses_1"
    assert result["state"] == "running"
    assert result["status"] == "busy"
    assert result["output"] == "almost done"
    assert result["output_chars"] == 11
    assert result["total_chars"] == 11
    assert result["truncated_chars"] == 0
    assert result["truncated"] is False
    assert fake.latest_caps == [server.WORKER_OUTPUT_DEFAULT_CHARS + 1]


def test_worker_status_preserves_string_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """String-form raw statuses survive in the status field."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {"ses_1": "busy"}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["status"] == "busy"
    assert result["state"] == "running"


def test_worker_status_error_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Message errors force error; missing entries map to unknown."""

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        fake = _FakeWorkerClient()
        monkeypatch.setattr(server, "get_client", lambda: fake)
        fake.status_map = {"ses_1": {"type": "idle"}}
        fake.latest = {"messageID": "m1", "text": "boom", "total_chars": 4, "has_error": True}
        error_result = await server.worker_status("ses_1")
        fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
        unknown_result = await server.worker_status("ses_missing")
        return error_result, unknown_result

    error_result, unknown_result = asyncio.run(run())
    assert error_result["state"] == "error"
    assert unknown_result["state"] == "unknown"
    assert unknown_result["status"] is None


def test_worker_status_completed_text_infers_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent status plus a completed assistant message infers idle."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {}
    fake.latest = {"messageID": "m1", "text": "done", "total_chars": 4, "has_error": False}
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["state"] == "idle"
    assert result["status"] is None
    assert result["messageID"] == "m1"
    assert result["output"] == "done"


def test_worker_status_completed_empty_text_infers_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent status plus a messageID with empty text still infers idle."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {}
    fake.latest = {"messageID": "m2", "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["state"] == "idle"
    assert result["status"] is None
    assert result["messageID"] == "m2"
    assert result["output"] == ""


def test_worker_status_absent_assistant_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent status with no assistant message stays unknown."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_status("ses_missing"))
    assert result["state"] == "unknown"
    assert result["status"] is None
    assert result["messageID"] is None


def test_worker_status_absent_with_assistant_error_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent status plus an assistant error stays error, never idle."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {}
    fake.latest = {"messageID": "m3", "text": "boom", "total_chars": 4, "has_error": True}
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["state"] == "error"
    assert result["status"] is None
    assert result["messageID"] == "m3"


def test_worker_status_busy_with_assistant_stays_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Busy status plus an assistant message stays running, never idle."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {"ses_1": {"type": "busy"}}
    fake.latest = {"messageID": "m4", "text": "working", "total_chars": 7, "has_error": False}
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["state"] == "running"
    assert result["status"] == "busy"
    assert result["messageID"] == "m4"
    assert result["output"] == "working"


def test_worker_status_skips_output_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_output=false returns no text without flagging truncation."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {"ses_1": {"type": "idle"}}
    fake.latest = {"messageID": "m1", "text": "hello", "total_chars": 5, "has_error": False}
    result = asyncio.run(server.worker_status("ses_1", include_output=False))
    assert result["output"] is None
    assert result["output_chars"] == 0
    assert result["total_chars"] == 0
    assert result["truncated_chars"] == 0
    assert result["truncated"] is False
    assert result["state"] == "idle"
    assert result["messageID"] is None
    assert fake.latest_caps == []


def test_worker_status_truncates_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-cap output is sliced at the cap with counts and an explicit flag."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {"ses_1": {"type": "busy"}}
    fake.latest = {"messageID": "m1", "text": "x" * 100, "total_chars": 100, "has_error": False}
    result = asyncio.run(server.worker_status("ses_1", max_output_chars=10))
    assert result["output"] == "x" * 10
    assert result["output_chars"] == 10
    assert result["total_chars"] == 100
    assert result["truncated_chars"] == 90
    assert result["truncated"] is True
    assert fake.latest_caps == [11]


def test_worker_status_bounds_output_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Huge caps clamp to the max; zero/negative clamp to one char."""
    fake = _patch_client(monkeypatch)
    fake.status_map = {"ses_1": {"type": "busy"}}
    big = "y" * (server.WORKER_OUTPUT_MAX_CHARS + 100)
    fake.latest = {"messageID": "m1", "text": big, "total_chars": len(big), "has_error": False}

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        clamped = await server.worker_status("ses_1", max_output_chars=10**9)
        tiny = await server.worker_status("ses_1", max_output_chars=0)
        return clamped, tiny

    clamped, tiny = asyncio.run(run())
    assert len(clamped["output"]) == server.WORKER_OUTPUT_MAX_CHARS
    assert clamped["output_chars"] == server.WORKER_OUTPUT_MAX_CHARS
    assert clamped["total_chars"] == server.WORKER_OUTPUT_MAX_CHARS + 100
    assert clamped["truncated_chars"] == 100
    assert clamped["truncated"] is True
    assert tiny["output"] == "y"
    assert tiny["output_chars"] == 1
    assert tiny["truncated"] is True


def test_worker_status_large_output_over_20k_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A >20k assistant reply flows through unclipped with exact counts."""
    big = "w" * 25000
    messages = [
        {
            "info": {"id": "m1", "role": "assistant"},
            "parts": [{"type": "text", "text": big}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            return httpx.Response(200, json={"ses_1": {"type": "idle"}}, request=request)
        return httpx.Response(200, json=messages, request=request)

    async def run() -> dict[str, Any]:
        client = OpencodeClient("http://opencode", "u", "p")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://opencode"
        )
        monkeypatch.setattr(server, "get_client", lambda: client)
        try:
            return await server.worker_status("ses_1")
        finally:
            await client.close()

    result = asyncio.run(run())
    assert result["state"] == "idle"
    assert len(result["output"]) == server.WORKER_OUTPUT_DEFAULT_CHARS
    assert result["output_chars"] == server.WORKER_OUTPUT_DEFAULT_CHARS
    assert result["total_chars"] == 25000
    assert result["truncated_chars"] == 25000 - server.WORKER_OUTPUT_DEFAULT_CHARS
    assert result["truncated"] is True


def _catalog_payload() -> dict[str, Any]:
    return {
        "connected": ["opencode"],
        "default": {},
        "all": [
            {
                "id": "opencode",
                "name": "Opencode",
                "models": {
                    "muse-spark-1.3-contributor-free": {
                        "id": "muse-spark-1.3-contributor-free",
                        "name": "Spark Free",
                        "cost": {"input": 0, "output": 0},
                    },
                    "paid-1": {
                        "id": "paid-1",
                        "name": "Paid One",
                        "cost": {"input": 3, "output": 15},
                    },
                },
            },
            {
                "id": "other",
                "name": "Other",
                "models": {
                    "other-free": {"id": "other-free", "name": "Other Free"},
                },
            },
        ],
    }


def test_worker_catalog_defaults_to_free_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults keep only free models on connected providers."""
    fake = _patch_client(monkeypatch)
    fake.providers_raw = _catalog_payload()
    result = asyncio.run(server.worker_catalog())
    assert [m["modelID"] for m in result["models"]] == ["muse-spark-1.3-contributor-free"]
    assert result["default"] == {
        "providerID": "opencode",
        "modelID": "muse-spark-1.3-contributor-free",
    }
    assert result["total"] == 1


def test_worker_catalog_filters_and_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling filters and querying is case-insensitive."""
    fake = _patch_client(monkeypatch)
    fake.providers_raw = _catalog_payload()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        all_models = await server.worker_catalog(free_only=False, connected_only=False)
        queried = await server.worker_catalog(query="SPARK", free_only=False, connected_only=False)
        return all_models, queried

    all_models, queried = asyncio.run(run())
    assert all_models["total"] == 3
    assert [m["modelID"] for m in queried["models"]] == ["muse-spark-1.3-contributor-free"]


def test_worker_catalog_limit_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limits clamp to 1-100."""
    fake = _patch_client(monkeypatch)
    models = {f"free-{i}": {"id": f"free-{i}", "name": f"Free {i}"} for i in range(105)}
    fake.providers_raw = {
        "connected": ["opencode"],
        "default": {},
        "all": [{"id": "opencode", "name": "Opencode", "models": models}],
    }

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        capped = await server.worker_catalog(free_only=False, limit=200)
        floored = await server.worker_catalog(free_only=False, limit=0)
        return capped, floored

    capped, floored = asyncio.run(run())
    assert len(capped["models"]) == 100
    assert capped["total"] == 105
    assert len(floored["models"]) == 1
