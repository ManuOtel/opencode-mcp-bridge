"""Stale worker lifecycle: timeout classification and idempotent cleanup.

Covers the coordinator-side observability slice for tasks that stay
running/busy with empty output: bounded startup/progress timeout,
clear stale status with a recovery hint, safe idempotent cleanup that
touches only the given taskID, and backward compatibility with legacy
records. No network, no credentials, no unbounded output.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server
from opencode_mcp_bridge.opencode_client import OpencodeError


class _FakeStaleClient:
    """Minimal fake for stale-lifecycle tests."""

    default_directory = "/home/tester"
    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"

    def __init__(self) -> None:
        self.created: list[tuple[Any, Any]] = []
        self.prompted: list[tuple[Any, ...]] = []
        self.aborted: list[tuple[Any, Any]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.abort_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }
        self._next = 0

    def resolve_model(self, provider_id: Any, model_id: Any) -> tuple[str, str]:
        if bool(provider_id) != bool(model_id):
            raise ValueError("provider_id and model_id must be given together or omitted")
        return (
            provider_id or self.default_provider_id,
            model_id or self.default_model_id,
        )

    async def create_session(self, title: Any, directory: Any) -> dict[str, Any]:
        self.created.append((title, directory))
        self._next += 1
        return {"id": f"ses_{self._next}", "title": title, "directory": directory}

    async def prompt_async(self, *args: Any, **kwargs: Any) -> bool:
        self.prompted.append(args)
        return True

    async def abort_session(self, session_id: str, directory: Any = None) -> bool:
        self.aborted.append((session_id, directory))
        if self.abort_error is not None:
            raise self.abort_error
        return True

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        self.deleted.append((session_id, directory))
        if self.delete_error is not None:
            raise self.delete_error
        return True

    async def get_session(self, session_id: str, directory: Any = None) -> dict[str, Any]:
        return {"id": session_id, "title": None, "directory": directory}

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.latest


def _patch(monkeypatch: pytest.MonkeyPatch) -> _FakeStaleClient:
    fake = _FakeStaleClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def _state_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)
    return path


def _backdate_record(path: Path, task_id: str, age_s: float) -> None:
    data = json.loads(path.read_text())
    data["tasks"][task_id]["created_at"] = time.time() - age_s
    path.write_text(json.dumps({"version": 1, "tasks": data["tasks"]}))


def test_normal_success_never_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fresh running task with output and idle task both report stale=false."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("do work", directory="/tmp/w"))
    task_id = created["taskID"]

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        fake.status_map = {task_id: {"type": "busy"}}
        fake.latest = {"messageID": "m1", "text": "progress", "total_chars": 8, "has_error": False}
        running = await server.worker_status(task_id)
        fake.status_map = {}
        fake.latest = {"messageID": "m2", "text": "done", "total_chars": 4, "has_error": False}
        idle = await server.worker_status(task_id)
        return running, idle

    running, idle = asyncio.run(run())
    assert running["state"] == "running"
    assert running["stale"] is False
    assert running["stale_reason"] is None
    assert running["recovery_hint"] is None
    assert idle["state"] == "idle"
    assert idle["stale"] is False


def test_retry_empty_output_fresh_not_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Busy with empty output but within the timeout stays running, not stale."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("do work", directory="/tmp/w", requestID="req-1"))
    fake.status_map = {created["taskID"]: {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_status(created["taskID"]))
    assert result["state"] == "running"
    assert result["stale"] is False
    assert result["stale_reason"] is None
    # requestID idempotency still holds for fresh tasks.
    second = asyncio.run(server.worker_run("do work", directory="/tmp/w", requestID="req-1"))
    assert second["deduplicated"] is True
    assert second["taskID"] == created["taskID"]
    assert len(fake.created) == 1


def test_stale_timeout_classification(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Running with empty output past the timeout classifies stale with recovery hint."""
    path = _state_file(monkeypatch, tmp_path)
    monkeypatch.setenv("TASK_STALE_AFTER_S", "60")
    monkeypatch.setattr(server, "_settings", None)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("do work", directory="/tmp/w"))
    task_id = created["taskID"]
    _backdate_record(path, task_id, age_s=600.0)
    fake.status_map = {task_id: {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_status(task_id))
    assert result["state"] == "stale"
    assert result["stale"] is True
    assert isinstance(result["stale_reason"], str)
    assert len(result["stale_reason"]) <= server.TASK_STALE_REASON_MAX_CHARS
    assert "recovery" in result["stale_reason"].lower()
    assert result["recovery_hint"] == server.WORKER_STALE_RECOVERY_HINT


def test_stale_explicit_directory_and_skipped_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit directory still classifies stale; skipped output never does."""
    path = _state_file(monkeypatch, tmp_path)
    monkeypatch.setenv("TASK_STALE_AFTER_S", "60")
    monkeypatch.setattr(server, "_settings", None)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("do work", directory="/tmp/w"))
    task_id = created["taskID"]
    _backdate_record(path, task_id, age_s=600.0)
    fake.status_map = {task_id: {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        explicit = await server.worker_status(task_id, directory="/tmp/w")
        skipped = await server.worker_status(task_id, include_output=False)
        return explicit, skipped

    explicit, skipped = asyncio.run(run())
    assert explicit["state"] == "stale"
    assert explicit["stale"] is True
    assert skipped["stale"] is False
    assert skipped["state"] == "running"


def test_status_redaction_and_bounds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stale status carries no prompt, token, header, or unbounded output."""
    path = _state_file(monkeypatch, tmp_path)
    monkeypatch.setenv("TASK_STALE_AFTER_S", "60")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "token-should-never-appear-abc")
    monkeypatch.setattr(server, "_settings", None)
    fake = _patch(monkeypatch)
    secret_prompt = "stale-secret-prompt-unique-xyz-999"
    created = asyncio.run(server.worker_run(secret_prompt, directory="/tmp/w"))
    task_id = created["taskID"]
    _backdate_record(path, task_id, age_s=600.0)
    fake.status_map = {task_id: {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_status(task_id, max_output_chars=10**9))
    flat = json.dumps(result)
    assert secret_prompt not in flat
    assert "token-should-never-appear-abc" not in flat
    assert "Authorization" not in flat
    assert "Bearer" not in flat
    assert "/tmp/w" in flat  # directory itself is returned exactly, by design
    assert result["stale_reason"] is not None
    assert len(result["stale_reason"]) <= server.TASK_STALE_REASON_MAX_CHARS
    assert len(result["recovery_hint"] or "") <= server.TASK_STALE_REASON_MAX_CHARS
    assert result["output_chars"] <= server.WORKER_OUTPUT_MAX_CHARS


def test_cleanup_after_timeout_removes_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Delete on a stale task removes the record and touches only that taskID."""
    path = _state_file(monkeypatch, tmp_path)
    monkeypatch.setenv("TASK_STALE_AFTER_S", "60")
    monkeypatch.setattr(server, "_settings", None)
    fake = _patch(monkeypatch)
    first = asyncio.run(server.worker_run("job one", directory="/tmp/w"))
    second = asyncio.run(server.worker_run("job two", directory="/tmp/w"))
    _backdate_record(path, first["taskID"], age_s=600.0)
    result = asyncio.run(server.worker_cleanup(first["taskID"], "/tmp/w", action="delete"))
    assert result["deleted"] is True
    assert result["action"] == "delete"
    stored = json.loads(path.read_text())["tasks"]
    assert first["taskID"] not in stored
    assert second["taskID"] in stored
    # Only the requested session was aborted/deleted; no unrelated kills.
    assert {t for t, _ in fake.aborted} == {first["taskID"]}
    assert {t for t, _ in fake.deleted} == {first["taskID"]}


def test_cleanup_delete_missing_session_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Delete when OpenCode already dropped the session still removes the record."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("do work", directory="/tmp/w"))
    task_id = created["taskID"]
    gone = OpencodeError("GET", f"/session/{task_id}", 404, "not found")
    fake.abort_error = gone
    fake.delete_error = gone
    result = asyncio.run(server.worker_cleanup(task_id, "/tmp/w", action="delete"))
    assert result["deleted"] is True
    assert result["aborted"] is False
    assert isinstance(result["cleanup_warning"], str)
    assert len(result["cleanup_warning"]) <= server.WORKER_CLEANUP_WARNING_MAX_CHARS
    assert "not found" not in (result["cleanup_warning"] or "")
    stored = json.loads(path.read_text())["tasks"]
    assert task_id not in stored
    # Repeating the delete for an untracked task with a gone session succeeds.
    repeat = asyncio.run(server.worker_cleanup(task_id, "/tmp/w", action="delete"))
    assert repeat["deleted"] is True


def test_cleanup_delete_failure_keeps_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-404 delete failures still raise and keep the record (no silent loss)."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("do work", directory="/tmp/w"))
    fake.delete_error = OpencodeError("DELETE", "/session/x", 500, "boom")
    with pytest.raises(OpencodeError, match="boom"):
        asyncio.run(server.worker_cleanup(created["taskID"], "/tmp/w", action="delete"))
    assert created["taskID"] in json.loads(path.read_text())["tasks"]


def test_backward_compat_legacy_records_never_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Records written before created_at existed load fine and never go stale."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    legacy = {
        "version": 1,
        "tasks": {
            "ses_legacy": {
                "taskID": "ses_legacy",
                "requestID": "req-legacy",
                "fingerprint": "abc",
                "directory": "/tmp/w",
                "title": None,
                "agent": None,
                "providerID": "opencode",
                "modelID": "muse-spark-1.3-contributor-free",
            }
        },
    }
    path.write_text(json.dumps(legacy))
    fake.status_map = {"ses_legacy": {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_status("ses_legacy"))
    assert result["state"] == "running"
    assert result["stale"] is False
    assert result["stale_reason"] is None
    assert result["directory"] == "/tmp/w"
    # New records carry created_at while legacy ones keep working.
    created = asyncio.run(server.worker_run("fresh", directory="/tmp/w"))
    stored = json.loads(path.read_text())["tasks"]
    assert isinstance(stored[created["taskID"]]["created_at"], float)
    assert "created_at" not in stored["ses_legacy"]


def test_stale_after_config_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK_STALE_AFTER_S defaults to 600 and rejects out-of-range values."""
    from opencode_mcp_bridge import config

    monkeypatch.delenv("TASK_STALE_AFTER_S", raising=False)
    monkeypatch.setattr(server, "_settings", None)
    assert config.load_settings().task_stale_after_s == 600
    for bad in ("not-a-number", "10", "99999"):
        monkeypatch.setenv("TASK_STALE_AFTER_S", bad)
        monkeypatch.setattr(server, "_settings", None)
        with pytest.raises(RuntimeError, match="TASK_STALE_AFTER_S|numeric"):
            config.load_settings()
    monkeypatch.setenv("TASK_STALE_AFTER_S", "120")
    monkeypatch.setattr(server, "_settings", None)
    assert config.load_settings().task_stale_after_s == 120
