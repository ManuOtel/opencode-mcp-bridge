"""Provider error classification: sanitized allowlisted enum, no raw leaks.

Covers the reliability fix end to end: classifier allowlist, legacy+V2
message normalization parity, worker snapshot error_code, bounded
evidence, serialization redaction, and preservation of status mapping
plus task_not_found/approval codes. No network.
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

from opencode_mcp_bridge import server
from opencode_mcp_bridge.opencode_client import (
    PROVIDER_ERROR_AUTH,
    PROVIDER_ERROR_GENERIC,
    PROVIDER_ERROR_QUOTA_EXHAUSTED,
    PROVIDER_ERROR_ROUTE_UNAVAILABLE,
    PROVIDER_ERRORS,
    OpencodeClient,
    classify_provider_error,
)

CANARY_MESSAGE = "CANARY-PROVIDER-MESSAGE-9f8e7d6c5b4a"
CANARY_PROMPT = "CANARY-PROMPT-1a2b3c4d5e6f"
CANARY_TOKEN = "CANARY-TOKEN-0f9e8d7c6b5a"

ALLOWED_ERROR_CODES = frozenset(
    {
        None,
        "task_not_found",
        "approval_rejected",
        "approval_expired",
        *PROVIDER_ERRORS,
    }
)
ALLOWED_EVIDENCE_KEYS = frozenset(
    {"status", "messageID", "output_chars", "total_chars", "provider_error"}
)


def test_classifier_generic_for_unallowlisted_truthy() -> None:
    """Truthy but unallowlisted errors map to generic, never specific."""
    assert classify_provider_error({"name": "ApiError"}) == PROVIDER_ERROR_GENERIC
    assert classify_provider_error({"type": "provider", "message": "boom"}) == (
        PROVIDER_ERROR_GENERIC
    )
    assert classify_provider_error("upstream exploded") == PROVIDER_ERROR_GENERIC
    assert classify_provider_error(500) == PROVIDER_ERROR_GENERIC
    assert classify_provider_error(True) == PROVIDER_ERROR_GENERIC


def test_classifier_falsy_is_none_even_with_text_signals() -> None:
    """Falsy errors never imply quota; message text is never inspected."""
    assert classify_provider_error(None) is None
    assert classify_provider_error({}) is None
    assert classify_provider_error("") is None
    assert classify_provider_error(False) is None
    assert classify_provider_error(None, finish="stop") is None
    # A quota word in free-form text without structured evidence is None:
    # classifier takes the error object, not output text, so there is no
    # signal to read here at all.
    assert classify_provider_error(None, finish=None) is None


def test_classifier_finish_error_is_generic() -> None:
    """V2 finish=error alone is explicit generic evidence, never quota."""
    assert classify_provider_error(None, finish="error") == PROVIDER_ERROR_GENERIC
    assert classify_provider_error({}, finish="error") == PROVIDER_ERROR_GENERIC
    assert classify_provider_error("", finish="error") == PROVIDER_ERROR_GENERIC


def test_classifier_quota_only_on_allowlisted_signals() -> None:
    """Quota requires an explicit structured signal; message text ignored."""
    assert classify_provider_error({"code": "quota_exceeded"}) == PROVIDER_ERROR_QUOTA_EXHAUSTED
    assert (
        classify_provider_error({"name": "Rate_Limit_Exceeded"}) == PROVIDER_ERROR_QUOTA_EXHAUSTED
    )
    assert classify_provider_error({"type": "429"}) == PROVIDER_ERROR_QUOTA_EXHAUSTED
    assert classify_provider_error({"status": 429}) == PROVIDER_ERROR_QUOTA_EXHAUSTED
    # Free-form message carrying a quota word does NOT imply quota.
    assert (
        classify_provider_error({"type": "provider", "message": "quota exploded"})
        == PROVIDER_ERROR_GENERIC
    )
    assert (
        classify_provider_error({"name": "ApiError", "message": CANARY_MESSAGE})
        == PROVIDER_ERROR_GENERIC
    )


def test_classifier_route_and_auth_distinct() -> None:
    """Route and auth signals map to their own codes, not generic/quota."""
    assert classify_provider_error({"code": "model_not_found"}) == PROVIDER_ERROR_ROUTE_UNAVAILABLE
    assert (
        classify_provider_error({"type": "route_unavailable"}) == PROVIDER_ERROR_ROUTE_UNAVAILABLE
    )
    assert classify_provider_error({"name": "unauthorized"}) == PROVIDER_ERROR_AUTH
    assert classify_provider_error({"code": "invalid_api_key"}) == PROVIDER_ERROR_AUTH
    assert classify_provider_error({"statusCode": 401}) == PROVIDER_ERROR_AUTH
    assert classify_provider_error({"status": 403}) == PROVIDER_ERROR_AUTH


def test_classifier_conflicting_classes_are_generic() -> None:
    """Distinct quota/auth/route signals collide to generic, never first match."""
    assert (
        classify_provider_error({"code": "quota_exceeded", "name": "unauthorized"})
        == PROVIDER_ERROR_GENERIC
    )
    assert (
        classify_provider_error({"code": "quota_exceeded", "type": "model_not_found"})
        == PROVIDER_ERROR_GENERIC
    )
    assert (
        classify_provider_error({"code": "unauthorized", "type": "model_not_found"})
        == PROVIDER_ERROR_GENERIC
    )
    # Repeated signals within one class keep that single class.
    assert (
        classify_provider_error({"code": "quota_exceeded", "type": "429"})
        == PROVIDER_ERROR_QUOTA_EXHAUSTED
    )
    assert classify_provider_error({"code": "unauthorized", "type": "401"}) == PROVIDER_ERROR_AUTH
    assert (
        classify_provider_error({"code": "model_not_found", "type": "route_unavailable"})
        == PROVIDER_ERROR_ROUTE_UNAVAILABLE
    )


def _legacy_client(payload: list[dict[str, Any]]) -> OpencodeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True}, request=request)
        return httpx.Response(200, json=payload, request=request)

    client = OpencodeClient("http://opencode", "u", "p")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://opencode"
    )
    return client


def _v2_client(messages: list[dict[str, Any]]) -> OpencodeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(404, json={}, request=request)
        if request.url.path == "/api/info":
            return httpx.Response(
                200,
                json={"version": "2.0.0", "pid": 1, "urls": [], "paths": {"tmp": "/tmp"}},
                request=request,
            )
        return httpx.Response(200, json={"data": messages}, request=request)

    client = OpencodeClient("http://opencode", "u", "p")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://opencode"
    )
    return client


def test_legacy_normalization_quota_route_auth_generic() -> None:
    """Legacy info.error shapes normalize to the allowlisted enum."""
    cases = [
        ({"code": "quota_exceeded"}, PROVIDER_ERROR_QUOTA_EXHAUSTED),
        ({"code": "model_not_found"}, PROVIDER_ERROR_ROUTE_UNAVAILABLE),
        ({"name": "unauthorized"}, PROVIDER_ERROR_AUTH),
        ({"name": "ApiError"}, PROVIDER_ERROR_GENERIC),
        ({"name": "ApiError", "message": CANARY_MESSAGE}, PROVIDER_ERROR_GENERIC),
    ]
    for error, expected in cases:

        async def run(err: Any = error) -> dict[str, Any]:
            payload = [
                {
                    "info": {"id": "m1", "role": "assistant", "error": err},
                    "parts": [{"type": "text", "text": "detail"}],
                }
            ]
            client = _legacy_client(payload)
            try:
                return await client.get_latest_assistant("ses_x")
            finally:
                await client.close()

        result = asyncio.run(run())
        assert result["has_error"] is True
        assert result["provider_error"] == expected


def test_legacy_empty_error_is_not_error() -> None:
    """Falsy legacy errors stay clean, never quota."""
    for empty in ({}, None, "", False):

        async def run(err: Any = empty) -> dict[str, Any]:
            payload = [
                {
                    "info": {"id": "m1", "role": "assistant", "error": err},
                    "parts": [{"type": "text", "text": ""}],
                }
            ]
            client = _legacy_client(payload)
            try:
                return await client.get_latest_assistant("ses_x")
            finally:
                await client.close()

        result = asyncio.run(run())
        assert result["has_error"] is False
        assert result["provider_error"] is None


def test_v2_matches_legacy_parity() -> None:
    """V2 error/finish behavior matches legacy: same signals, same enums."""
    # Explicit quota signal, both families.
    legacy_quota = asyncio.run(_run_legacy({"code": "quota_exceeded"}, "detail"))
    v2_quota = asyncio.run(_run_v2({"type": "quota_exceeded"}, "error", "detail"))
    assert (
        legacy_quota["provider_error"]
        == v2_quota["provider_error"]
        == (PROVIDER_ERROR_QUOTA_EXHAUSTED)
    )
    # Generic backend failure, both families.
    legacy_generic = asyncio.run(_run_legacy({"name": "ApiError"}, "boom"))
    v2_generic = asyncio.run(_run_v2({"type": "provider", "message": "boom"}, "error", "x"))
    assert (
        legacy_generic["provider_error"] == v2_generic["provider_error"] == (PROVIDER_ERROR_GENERIC)
    )
    assert legacy_generic["has_error"] is True
    assert v2_generic["has_error"] is True
    # Finish=error without an error object is generic, like legacy truthy.
    v2_finish_only = asyncio.run(_run_v2(None, "error", "partial"))
    assert v2_finish_only["has_error"] is True
    assert v2_finish_only["provider_error"] == PROVIDER_ERROR_GENERIC
    # Finish=stop without error is clean in both families.
    v2_clean = asyncio.run(_run_v2(None, "stop", "done"))
    assert v2_clean["has_error"] is False
    assert v2_clean["provider_error"] is None


async def _run_legacy(error: Any, text: str) -> dict[str, Any]:
    payload = [
        {
            "info": {"id": "m1", "role": "assistant", "error": error},
            "parts": [{"type": "text", "text": text}],
        }
    ]
    client = _legacy_client(payload)
    try:
        return await client.get_latest_assistant("ses_x")
    finally:
        await client.close()


async def _run_v2(error: Any, finish: str | None, text: str) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": "msg_1",
        "type": "assistant",
        "content": [{"type": "text", "text": text}],
        "time": {"created": 1},
    }
    if error is not None:
        message["error"] = error
    if finish is not None:
        message["finish"] = finish
    client = _v2_client([message])
    try:
        return await client.get_latest_assistant("ses_1")
    finally:
        await client.close()


class _FakeSnapshotClient:
    """Fake returning caller-controlled assistant views for snapshot tests."""

    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"
    default_directory = "/home/tester"

    def __init__(self) -> None:
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
            "provider_error": None,
        }

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return dict(self.latest)


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch, fake: _FakeSnapshotClient) -> None:
    monkeypatch.setattr(server, "get_client", lambda: fake)


def test_snapshot_generic_error_code_and_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic provider errors map to the generic code with no raw leak."""
    fake = _FakeSnapshotClient()
    fake.status_map = {"ses_1": {"type": "busy"}}
    fake.latest = {
        "messageID": "m9",
        "text": CANARY_MESSAGE,
        "total_chars": len(CANARY_MESSAGE),
        "has_error": True,
        "provider_error": PROVIDER_ERROR_GENERIC,
    }
    _patch_snapshot(monkeypatch, fake)
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["state"] == "error"
    assert result["error_code"] == PROVIDER_ERROR_GENERIC
    assert result["provider_error"] == PROVIDER_ERROR_GENERIC
    assert result["evidence"]["provider_error"] == PROVIDER_ERROR_GENERIC
    assert result["output"] == ""
    serialized = json.dumps(result)
    assert CANARY_MESSAGE not in serialized
    assert CANARY_PROMPT not in serialized
    assert CANARY_TOKEN not in serialized
    assert result["error_code"] in ALLOWED_ERROR_CODES
    assert set(result["evidence"]) == ALLOWED_EVIDENCE_KEYS


def test_snapshot_distinct_codes_and_no_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota/route/auth map to distinct codes only on explicit evidence."""
    cases = [
        (PROVIDER_ERROR_QUOTA_EXHAUSTED, "provider_quota_exhausted"),
        (PROVIDER_ERROR_ROUTE_UNAVAILABLE, "provider_route_unavailable"),
        (PROVIDER_ERROR_AUTH, "provider_auth"),
    ]
    for enum_value, code in cases:
        fake = _FakeSnapshotClient()
        fake.status_map = {"ses_1": {"type": "busy"}}
        fake.latest = {
            "messageID": "m1",
            "text": CANARY_MESSAGE,
            "total_chars": 4,
            "has_error": True,
            "provider_error": enum_value,
        }
        _patch_snapshot(monkeypatch, fake)
        result = asyncio.run(server.worker_status("ses_1"))
        assert result["state"] == "error"
        assert result["error_code"] == code
        assert result["evidence"]["provider_error"] == enum_value
        assert CANARY_MESSAGE not in json.dumps(result)


def test_snapshot_empty_or_retry_never_implies_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty output and retry status without evidence stay quota-free."""
    fake = _FakeSnapshotClient()
    _patch_snapshot(monkeypatch, fake)

    async def run() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        fake.status_map = {"ses_1": {"type": "retry"}}
        fake.latest = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
            "provider_error": None,
        }
        retry_view = await server.worker_status("ses_1")
        fake.status_map = {"ses_1": {"type": "busy"}}
        empty_view = await server.worker_status("ses_1")
        fake.status_map = {}
        unknown_view = await server.worker_status("ses_missing")
        return retry_view, empty_view, unknown_view

    retry_view, empty_view, unknown_view = asyncio.run(run())
    assert retry_view["state"] == "running"
    assert retry_view["error_code"] is None
    assert retry_view["evidence"]["provider_error"] is None
    assert empty_view["state"] == "running"
    assert empty_view["error_code"] is None
    assert unknown_view["state"] == "unknown"
    assert unknown_view["error_code"] == "task_not_found"
    for view in (retry_view, empty_view, unknown_view):
        assert view["error_code"] != PROVIDER_ERROR_QUOTA_EXHAUSTED
        assert view["evidence"]["provider_error"] != PROVIDER_ERROR_QUOTA_EXHAUSTED


def test_snapshot_legacy_fake_without_enum_falls_back_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old fakes without provider_error still map errors to generic safely."""
    fake = _FakeSnapshotClient()
    fake.status_map = {"ses_1": {"type": "idle"}}
    fake.latest = {"messageID": "m1", "text": "boom", "total_chars": 4, "has_error": True}
    _patch_snapshot(monkeypatch, fake)
    result = asyncio.run(server.worker_status("ses_1"))
    assert result["state"] == "error"
    assert result["error_code"] == PROVIDER_ERROR_GENERIC
    assert "boom" not in json.dumps({k: v for k, v in result.items() if k != "output"})
    assert result["output"] == ""


def test_status_mapping_and_legacy_codes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Busy/retry/idle/error/unknown mapping plus legacy codes preserved."""
    fake = _FakeSnapshotClient()
    _patch_snapshot(monkeypatch, fake)

    async def run() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        fake.status_map = {"s": {"type": "busy"}}
        fake.latest = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
            "provider_error": None,
        }
        out["busy"] = await server.worker_status("s")
        fake.status_map = {"s": {"type": "retry"}}
        out["retry"] = await server.worker_status("s")
        fake.status_map = {"s": {"type": "idle"}}
        out["idle"] = await server.worker_status("s")
        fake.status_map = {"s": {"type": "weird"}}
        out["weird"] = await server.worker_status("s")
        fake.status_map = {}
        fake.latest = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
            "provider_error": None,
        }
        out["missing"] = await server.worker_status("absent")
        return out

    views = asyncio.run(run())
    assert views["busy"]["state"] == "running"
    assert views["retry"]["state"] == "running"
    assert views["idle"]["state"] == "idle"
    assert views["weird"]["state"] == "unknown"
    assert views["missing"]["state"] == "unknown"
    assert views["missing"]["error_code"] == "task_not_found"
    assert server._error_code_for_snapshot("running", "busy", "m1", None) is None
    assert server._error_code_for_snapshot("idle", None, "m1", None) is None
    assert server._error_code_for_snapshot("rejected", None, None) == "approval_rejected"
    assert server._error_code_for_snapshot("expired", None, None) == "approval_expired"


def test_wait_returns_provider_error_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker_wait surfaces provider errors without polling and redacted."""
    fake = _FakeSnapshotClient()
    fake.status_map = {"ses_1": {"type": "busy"}}
    fake.latest = {
        "messageID": "m1",
        "text": CANARY_MESSAGE,
        "total_chars": len(CANARY_MESSAGE),
        "has_error": True,
        "provider_error": PROVIDER_ERROR_QUOTA_EXHAUSTED,
    }
    _patch_snapshot(monkeypatch, fake)
    result = asyncio.run(server.worker_wait("ses_1", timeout_s=1))
    assert result["state"] == "error"
    assert result["error_code"] == PROVIDER_ERROR_QUOTA_EXHAUSTED
    assert result["timed_out"] is False
    assert CANARY_MESSAGE not in json.dumps(result)


def test_output_schemas_still_declare_error_contract() -> None:
    """Output schemas keep the stable error_code/evidence contract."""
    for schema in (
        server.WORKER_RUN_OUTPUT_SCHEMA,
        server.WORKER_STATUS_OUTPUT_SCHEMA,
        server.WORKER_WAIT_OUTPUT_SCHEMA,
        server.WORKER_VERIFY_OUTPUT_SCHEMA,
    ):
        assert schema.get("type") == "object"
        props = schema.get("properties", {})
        assert "error_code" in props
        assert "evidence" in props


def test_output_schemas_declare_provider_error_enum() -> None:
    """Status/wait/verify schemas declare the sanitized enum/null contract."""
    for schema in (
        server.WORKER_STATUS_OUTPUT_SCHEMA,
        server.WORKER_WAIT_OUTPUT_SCHEMA,
        server.WORKER_VERIFY_OUTPUT_SCHEMA,
    ):
        props = schema.get("properties", {})
        assert "provider_error" in props
        field = props["provider_error"]
        assert set(field.get("type", [])) == {"string", "null"}
        assert set(field.get("enum", [])) == set(PROVIDER_ERRORS) | {None}
    # Verify merges a full status snapshot, so its contract stays a superset.
    status_props = set(server.WORKER_STATUS_OUTPUT_SCHEMA["properties"])
    verify_props = set(server.WORKER_VERIFY_OUTPUT_SCHEMA["properties"])
    assert status_props <= verify_props


def test_emitted_results_match_provider_error_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every emitted provider_error value validates against the schema enum."""
    schemas = (
        server.WORKER_STATUS_OUTPUT_SCHEMA,
        server.WORKER_WAIT_OUTPUT_SCHEMA,
        server.WORKER_VERIFY_OUTPUT_SCHEMA,
    )
    fake = _FakeSnapshotClient()
    fake.status_map = {"ses_1": {"type": "busy"}}
    fake.latest = {
        "messageID": "m1",
        "text": CANARY_MESSAGE,
        "total_chars": len(CANARY_MESSAGE),
        "has_error": True,
        "provider_error": PROVIDER_ERROR_QUOTA_EXHAUSTED,
    }
    _patch_snapshot(monkeypatch, fake)

    async def run() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        status_view = await server.worker_status("ses_1")
        wait_view = await server.worker_wait("ses_1", timeout_s=1)
        plain = tmp_path / "plain"
        plain.mkdir(exist_ok=True)
        verify_view = await server.worker_verify("ses_1", directory=str(plain))
        return status_view, wait_view, verify_view

    status_view, wait_view, verify_view = asyncio.run(run())
    for view, schema in zip((status_view, wait_view, verify_view), schemas, strict=True):
        allowed = set(schema["properties"]["provider_error"]["enum"])
        assert view["provider_error"] in allowed
        assert view["provider_error"] == PROVIDER_ERROR_QUOTA_EXHAUSTED
        assert view["evidence"]["provider_error"] in allowed
        assert set(view) <= set(schema["properties"])
    assert "provider_error" in verify_view
