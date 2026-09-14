"""Focused tests for the bounded server-side worker_wait tool. No network."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server


class _FakeWaitClient:
    """Read-only fake: tracks LLM-mutating calls to prove wait never prompts."""

    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"
    default_directory = "/home/tester"

    def __init__(self) -> None:
        self.status_calls: list[Any] = []
        self.latest_calls: list[Any] = []
        self.prompted: list[tuple[Any, ...]] = []
        self.created: list[tuple[Any, Any]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.status_script: list[dict[str, Any]] = []
        self.latest_script: list[dict[str, Any]] = []
        self.default_latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }

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
        return True

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        self.deleted.append((session_id, directory))
        return True

    async def abort_session(self, session_id: str, directory: Any = None) -> bool:
        return True

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        self.status_calls.append(directory)
        if self.status_script:
            return self.status_script.pop(0)
        return {"ses_1": {"type": "busy"}}

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.latest_calls.append((session_id, directory, kwargs.get("max_chars")))
        if self.latest_script:
            return self.latest_script.pop(0)
        return dict(self.default_latest)

    async def get_providers_raw(self) -> dict[str, Any]:
        return {}


def _patch(monkeypatch: pytest.MonkeyPatch) -> _FakeWaitClient:
    fake = _FakeWaitClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def test_wait_returns_on_changed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Busy then idle returns early with changed=true, timed_out=false."""
    fake = _patch(monkeypatch)
    fake.status_script = [{"ses_1": {"type": "busy"}}, {}]
    fake.latest_script = [
        {"messageID": None, "text": "", "total_chars": 0, "has_error": False},
        {"messageID": "m1", "text": "done", "total_chars": 4, "has_error": False},
    ]
    start = time.monotonic()
    result = asyncio.run(server.worker_wait("ses_1", timeout_s=5))
    elapsed = time.monotonic() - start
    assert result["taskID"] == "ses_1"
    assert result["sessionID"] == "ses_1"
    assert result["state"] == "idle"
    assert result["changed"] is True
    assert result["timed_out"] is False
    assert result["error_code"] is None
    assert result["retryable"] is False
    assert result["next_action"] == "worker_verify"
    assert result["timeout_s"] == 5
    assert result["elapsed_s"] <= 5
    assert elapsed < 5
    assert result["evidence"]["messageID"] == "m1"
    # Read-only: never creates, prompts, or deletes.
    assert fake.prompted == []
    assert fake.created == []
    assert fake.deleted == []


def test_wait_timeout_returns_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Steady running hits the deadline with timed_out=true, changed=false."""
    fake = _patch(monkeypatch)
    fake.default_latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    start = time.monotonic()
    result = asyncio.run(server.worker_wait("ses_1", timeout_s=1))
    elapsed = time.monotonic() - start
    assert result["state"] == "running"
    assert result["timed_out"] is True
    assert result["changed"] is False
    assert result["retryable"] is True
    assert result["next_action"] == "worker_wait"
    assert result["error_code"] is None
    assert result["timeout_s"] == 1
    assert elapsed < 5
    assert len(fake.status_calls) >= 2
    assert fake.prompted == []


def test_wait_missing_task_returns_unknown_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent session returns unknown + task_not_found without waiting."""
    fake = _patch(monkeypatch)
    fake.status_script = [{}]
    fake.latest_script = [
        {"messageID": None, "text": "", "total_chars": 0, "has_error": False},
    ]
    start = time.monotonic()
    result = asyncio.run(server.worker_wait("ses_missing", timeout_s=30))
    elapsed = time.monotonic() - start
    assert result["state"] == "unknown"
    assert result["timed_out"] is False
    assert result["changed"] is False
    assert result["error_code"] == "task_not_found"
    assert result["retryable"] is False
    assert result["next_action"] == "worker_status"
    assert elapsed < 5


def test_wait_directory_scoping_explicit_and_recovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit dirs are used verbatim; omitted dirs recover the record."""
    import os

    fake = _patch(monkeypatch)
    fake.default_latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}

    explicit = asyncio.run(server.worker_wait("ses_1", directory="/tmp/w", timeout_s=1))
    assert explicit["directory"] == os.path.realpath("/tmp/w")
    assert fake.status_calls[0] == os.path.realpath("/tmp/w")

    record = server._build_task_record(
        "ses_1", None, "fp", os.path.realpath("/tmp/recovered"), None, None, "opencode", "m"
    )
    server._save_task_state({"ses_1": record})
    fake2 = _FakeWaitClient()
    monkeypatch.setattr(server, "get_client", lambda: fake2)
    recovered = asyncio.run(server.worker_wait("ses_1", timeout_s=1))
    assert recovered["directory"] == os.path.realpath("/tmp/recovered")
    assert fake2.status_calls[0] == os.path.realpath("/tmp/recovered")
    assert tmp_path is not None


def test_wait_rejects_out_of_scope_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directories outside the allowlist fail before any backend call."""
    fake = _patch(monkeypatch)
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.worker_wait("ses_1", directory="/definitely-outside-bridge"))
    assert fake.status_calls == []
    assert fake.latest_calls == []


def test_wait_explicit_dir_survives_corrupt_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt TASK_STATE_PATH never blocks an explicit-directory wait."""
    import os

    fake = _patch(monkeypatch)
    fake.default_latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    Path(os.environ["TASK_STATE_PATH"]).write_text("{not-json")
    result = asyncio.run(server.worker_wait("ses_1", directory="/tmp/w", timeout_s=1))
    assert result["taskID"] == "ses_1"
    assert result["state"] in ("running", "unknown", "idle", "error", "stale")


def test_wait_timeout_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeouts clamp to [1, 120]; non-finite values raise."""
    fake = _patch(monkeypatch)
    fake.default_latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    tiny = asyncio.run(server.worker_wait("ses_1", timeout_s=0))
    assert tiny["timeout_s"] == server.WORKER_WAIT_MIN_TIMEOUT_S
    assert tiny["timed_out"] is True
    assert server._clamp_worker_wait_timeout(10**9) == server.WORKER_WAIT_MAX_TIMEOUT_S
    assert server._clamp_worker_wait_timeout(None) == server.WORKER_WAIT_DEFAULT_TIMEOUT_S
    with pytest.raises(ValueError, match="taskID must not be empty"):
        asyncio.run(server.worker_wait("  ", timeout_s=1))
    with pytest.raises(ValueError, match="finite"):
        asyncio.run(server.worker_wait("ses_1", timeout_s=float("inf")))


def test_wait_output_cap_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Huge output caps clamp to the shared max without breaking evidence."""
    fake = _patch(monkeypatch)
    big = "q" * (server.WORKER_OUTPUT_MAX_CHARS + 50)
    steady = {"messageID": "m1", "text": big, "total_chars": len(big), "has_error": False}
    fake.default_latest = dict(steady)
    fake.status_script = [{"ses_1": {"type": "busy"}} for _ in range(10)]
    fake.latest_script = [dict(steady) for _ in range(10)]
    result = asyncio.run(server.worker_wait("ses_1", timeout_s=1, max_output_chars=10**9))
    assert len(result["output"]) == server.WORKER_OUTPUT_MAX_CHARS
    assert result["evidence"]["total_chars"] == len(big)


def test_status_backward_compatible_plus_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker_status keeps every legacy key and adds the stable contract."""
    fake = _patch(monkeypatch)
    fake.status_script = [{"ses_1": {"type": "busy"}}]
    fake.latest_script = [
        {"messageID": "m9", "text": "almost", "total_chars": 6, "has_error": False},
    ]
    result = asyncio.run(server.worker_status("ses_1"))
    for key in (
        "taskID",
        "sessionID",
        "state",
        "status",
        "messageID",
        "output",
        "output_chars",
        "total_chars",
        "truncated_chars",
        "truncated",
        "directory",
        "stale",
        "stale_reason",
        "recovery_hint",
    ):
        assert key in result
    assert result["state"] == "running"
    assert result["timed_out"] is False
    assert result["retryable"] is True
    assert result["next_action"] == "worker_wait"
    assert result["error_code"] is None
    assert result["evidence"] == {
        "status": "busy",
        "messageID": "m9",
        "output_chars": 6,
        "total_chars": 6,
    }


def test_run_stays_async_with_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker_run still starts work async and returns legacy + contract keys."""
    fake = _patch(monkeypatch)
    result = asyncio.run(server.worker_run("do it", title="t"))
    assert result["taskID"] == "ses_1"
    assert result["state"] == "running"
    assert result["deduplicated"] is False
    assert result["timed_out"] is False
    assert result["retryable"] is True
    assert result["next_action"] == "worker_wait"
    assert result["error_code"] is None
    assert result["evidence"]["status"] == "running"
    assert fake.prompted[0][0:2] == ("ses_1", "do it")


def test_annotations_and_schemas_are_truthful() -> None:
    """Wait/status/verify/catalog are read-only; cleanup stays destructive."""

    async def collect() -> dict[str, Any]:
        tools = {t.name: t for t in await server.worker_mcp.list_tools()}
        assert set(tools) == set(server.WORKER_TOOL_NAMES)
        assert "worker_wait" in tools
        out: dict[str, Any] = {}
        for name, tool in tools.items():
            ann = tool.annotations
            out[name] = {
                "read_only": getattr(ann, "read_only_hint", None),
                "destructive": getattr(ann, "destructive_hint", None),
                "output_schema": getattr(tool, "output_schema", None),
            }
        return out

    by_name = asyncio.run(collect())
    for name in ("worker_wait", "worker_status", "worker_verify", "worker_catalog"):
        assert by_name[name]["read_only"] is True
        assert by_name[name]["destructive"] is False
    assert by_name["worker_cleanup"]["destructive"] is True
    assert by_name["worker_cleanup"]["read_only"] is False
    for name, entry in by_name.items():
        schema = entry["output_schema"]
        assert isinstance(schema, dict), f"{name} missing output_schema"
        assert schema.get("type") == "object"
