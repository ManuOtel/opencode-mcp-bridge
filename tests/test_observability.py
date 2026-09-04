"""Focused observability tests: lifecycle events fire, secrets never leak."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import observability, server
from opencode_mcp_bridge.opencode_client import OpencodeError

LOGGER_NAME = "opencode_mcp_bridge.observability"


class _FakeObsClient:
    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"

    def __init__(self) -> None:
        self.prompt_error: Exception | None = None
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }

    def resolve_model(self, provider_id: Any, model_id: Any) -> tuple[str, str]:
        if bool(provider_id) != bool(model_id):
            raise ValueError("provider_id and model_id must be given together")
        return (provider_id or self.default_provider_id, model_id or self.default_model_id)

    async def create_session(self, title: Any, directory: Any) -> dict[str, Any]:
        return {"id": "ses_1", "title": title, "directory": directory}

    async def prompt_async(self, *args: Any, **kwargs: Any) -> bool:
        if self.prompt_error is not None:
            raise self.prompt_error
        return True

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        return True

    async def get_session(self, session_id: str, directory: Any = None) -> dict[str, Any]:
        return {"id": session_id}

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.latest


def _events(caplog: pytest.LogCaptureFixture) -> tuple[list[dict[str, Any]], str]:
    found: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != LOGGER_NAME:
            continue
        try:
            found.append(json.loads(record.getMessage()))
        except ValueError:
            continue
    combined = "\n".join(record.getMessage() for record in caplog.records)
    return found, combined


def _by_tool(events: list[dict[str, Any]], tool: str, outcome: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("tool") == tool and e.get("outcome") == outcome]


def test_worker_run_success_emits_lifecycle_without_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeObsClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    secret_prompt = "SECRET-PROMPT-UNIQUE-ABC-123"
    secret_request = "SECRET-REQUEST-UNIQUE-XYZ-789"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()
    result = asyncio.run(server.worker_run(secret_prompt, requestID=secret_request))
    assert result["taskID"] == "ses_1"
    events, combined = _events(caplog)
    assert _by_tool(events, "worker_run", "started")
    succeeded = _by_tool(events, "worker_run", "succeeded")
    assert succeeded
    assert succeeded[0].get("duration_ms") is not None
    assert succeeded[0].get("task_id") == "ses_1"
    assert secret_prompt not in combined
    assert secret_request not in combined
    assert "/home/tester" not in combined


def test_worker_run_rejected_validation_without_leak(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeObsClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()
    with pytest.raises(ValueError, match="requestID"):
        asyncio.run(server.worker_run("do work", requestID="  "))
    events, combined = _events(caplog)
    rejected = _by_tool(events, "worker_run", "rejected")
    assert rejected
    assert rejected[0].get("error_class") == "ValueError"
    assert rejected[0].get("duration_ms") is not None
    assert "do work" not in combined


def test_worker_run_backend_failure_hides_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeObsClient()
    fake.prompt_error = OpencodeError(
        "POST", "/session/ses_1/prompt_async", 500, "SECRET-BODY-UNIQUE-456"
    )
    monkeypatch.setattr(server, "get_client", lambda: fake)
    secret_prompt = "SECRET-PROMPT-FAILURE-999"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()
    with pytest.raises(OpencodeError):
        asyncio.run(server.worker_run(secret_prompt))
    events, combined = _events(caplog)
    failed = _by_tool(events, "worker_run", "failed")
    assert failed
    assert failed[0].get("error_class") == "OpencodeError"
    assert failed[0].get("status_code") == 500
    assert "SECRET-BODY-UNIQUE-456" not in combined
    assert secret_prompt not in combined


def test_worker_status_rejected_hides_user_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeObsClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    evil_path = "/etc/secret-unique-path-XYZ-123"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()
    with pytest.raises(ValueError, match="allowed"):
        asyncio.run(server.worker_status("ses_1", directory=evil_path))
    events, combined = _events(caplog)
    rejected = _by_tool(events, "worker_status", "rejected")
    assert rejected
    assert rejected[0].get("error_class") == "ValueError"
    assert evil_path not in combined


def test_auth_rejection_emits_without_token_leak(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from starlette.testclient import TestClient

    secret_token = "SECRET-BEARER-UNIQUE-777"
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", secret_token)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()
    with TestClient(server.create_app()) as client:
        response = client.post(
            "/mcp", json={}, headers={"Authorization": "Bearer wrong-token-XYZ-111"}
        )
    assert response.status_code == 401
    events, combined = _events(caplog)
    rejected = [e for e in events if e.get("event") == "mcp.auth"]
    assert rejected
    assert rejected[0].get("outcome") == "rejected"
    assert secret_token not in combined
    assert "wrong-token-XYZ-111" not in combined
    assert "Authorization" not in combined


def test_redact_request_id_never_returns_raw() -> None:
    assert observability.redact_request_id(None) is None
    assert observability.redact_request_id("   ") is None
    raw = "my-secret-request-id-123"
    redacted = observability.redact_request_id(raw)
    assert redacted is not None
    assert raw not in redacted
    assert redacted.startswith("sha256:")
    assert observability.redact_request_id(raw) == redacted
