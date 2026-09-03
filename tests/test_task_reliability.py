"""Focused tests for durable idempotent worker tasks. No network."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server
from opencode_mcp_bridge.opencode_client import OpencodeError


class _FakeTaskClient:
    """Minimal fake with session counting for dedup tests."""

    default_directory = "/home/tester"
    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"

    def __init__(self) -> None:
        self.created: list[tuple[Any, Any]] = []
        self.prompted: list[tuple[Any, ...]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.prompt_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.session_error: Exception | None = None
        self.missing_sessions: set[str] = set()
        self.get_session_calls: list[tuple[Any, Any]] = []
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }
        self.status_dirs: list[Any] = []
        self.latest_dirs: list[Any] = []
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
        return {"id": f"ses_{self._next}", "title": title, "directory": directory or "/home/tester"}

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

    async def abort_session(self, session_id: str, directory: Any = None) -> bool:
        return True

    async def get_session(self, session_id: str, directory: Any = None) -> dict[str, Any]:
        self.get_session_calls.append((session_id, directory))
        if self.session_error is not None:
            raise self.session_error
        if session_id in self.missing_sessions:
            raise OpencodeError("GET", f"/session/{session_id}", 404, "not found")
        return {"id": session_id, "title": None, "directory": directory}

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        self.status_dirs.append(directory)
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.latest_dirs.append(directory)
        return self.latest


def _patch(monkeypatch: pytest.MonkeyPatch) -> _FakeTaskClient:
    fake = _FakeTaskClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def _state_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)
    return path


def test_duplicate_request_returns_existing_without_second_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same requestID + same inputs returns the first task, no second session."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await server.worker_run("do X", directory="/tmp/w", requestID="req-1")
        second = await server.worker_run("do X", directory="/tmp/w", requestID="req-1")
        return first, second

    first, second = asyncio.run(run())
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["taskID"] == first["taskID"]
    assert len(fake.created) == 1
    assert len(fake.prompted) == 1


def test_conflicting_request_id_fails_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same requestID with different inputs fails without creating a session."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    asyncio.run(server.worker_run("do X", directory="/tmp/w", requestID="req-1"))
    created_before = list(fake.created)
    with pytest.raises(ValueError, match="different inputs"):
        asyncio.run(server.worker_run("do Y", directory="/tmp/w", requestID="req-1"))
    assert fake.created == created_before
    with pytest.raises(ValueError, match="different inputs"):
        asyncio.run(server.worker_run("do X", directory="/tmp/other", requestID="req-1"))
    assert fake.created == created_before


def test_fresh_reload_deduplicates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fresh registry load still deduplicates without a second session."""
    path = _state_file(monkeypatch, tmp_path)
    _patch(monkeypatch)
    first = asyncio.run(server.worker_run("hello", directory="/tmp/w", requestID="req-r"))
    assert path.is_file()
    # Simulate a bridge restart: new client, registry reloaded from disk.
    fake2 = _FakeTaskClient()
    monkeypatch.setattr(server, "get_client", lambda: fake2)
    second = asyncio.run(server.worker_run("hello", directory="/tmp/w", requestID="req-r"))
    assert second["deduplicated"] is True
    assert second["taskID"] == first["taskID"]
    assert fake2.created == []


def test_status_and_verify_recover_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Omitting directory recovers the saved directory for status and verify."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("hi", directory="/tmp/saved", title="t"))
    task_id = created["taskID"]
    fake.status_map = {task_id: {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    status = asyncio.run(server.worker_status(task_id))
    assert status["directory"] == "/tmp/saved"
    assert fake.status_dirs[-1] == "/tmp/saved"
    assert fake.latest_dirs[-1] == "/tmp/saved"
    verify = asyncio.run(server.worker_verify(task_id, directory=str(tmp_path)))
    assert verify["taskID"] == task_id
    assert verify["directory"] == str(tmp_path)


def test_verify_recovers_saved_directory_for_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify without directory uses the saved directory for git evidence."""
    import subprocess

    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    created = asyncio.run(server.worker_run("hi", directory=str(repo)))
    fake.status_map = {}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    result = asyncio.run(server.worker_verify(created["taskID"]))
    assert result["verification"]["ok"] is True
    assert result["verification"]["directory"] == str(repo)


def test_no_prompt_or_secrets_persisted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Task JSON stores metadata only, never prompt text or credentials."""
    path = _state_file(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "super-secret-pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "super-secret-token")
    monkeypatch.setattr(server, "_settings", None)
    _patch(monkeypatch)
    secret_prompt = "unique-prompt-xyz-123 with hunter2 details"
    asyncio.run(server.worker_run(secret_prompt, directory="/tmp/w", requestID="req-s"))
    raw = path.read_text()
    assert secret_prompt not in raw
    assert "hunter2" not in raw
    assert "super-secret-pw" not in raw
    assert "super-secret-token" not in raw
    data = json.loads(raw)
    record = next(iter(data["tasks"].values()))
    assert "fingerprint" in record
    assert "message" not in raw.lower() or "messageID" in raw


def test_prompt_failure_removes_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Failed prompt deletes the session and drops the persisted record."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    fake.prompt_error = OpencodeError("POST", "/session/ses_1/prompt_async", 500, "boom")
    with pytest.raises(OpencodeError, match="boom"):
        asyncio.run(server.worker_run("hi", directory="/tmp/w", requestID="req-f"))
    assert fake.deleted != []
    assert not path.is_file() or path.read_text().find("req-f") == -1


def test_cleanup_delete_removes_record_only_after_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Delete removes the record after success; failed delete keeps it."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("hi", directory="/tmp/w"))
    assert created["taskID"] in json.loads(path.read_text())["tasks"]
    result = asyncio.run(server.worker_cleanup(created["taskID"], "/tmp/w", action="delete"))
    assert result["deleted"] is True
    assert created["taskID"] not in json.loads(path.read_text())["tasks"]

    created2 = asyncio.run(server.worker_run("hi again", directory="/tmp/w"))
    fake.delete_error = OpencodeError("DELETE", "/session/x", 500, "nope")
    with pytest.raises(OpencodeError, match="nope"):
        asyncio.run(server.worker_cleanup(created2["taskID"], "/tmp/w", action="delete"))
    assert created2["taskID"] in json.loads(path.read_text())["tasks"]


def test_record_bounds_and_directory_bound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Registry caps record count; stored directories are kept exactly."""
    _state_file(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "TASK_MAX_RECORDS", 3)
    _patch(monkeypatch)
    ids: list[str] = []
    for i in range(5):
        result = asyncio.run(server.worker_run(f"msg-{i}", directory="/tmp/w"))
        ids.append(result["taskID"])
    tasks = server._load_task_state()
    assert len(tasks) == 3
    assert ids[-1] in tasks
    assert ids[0] not in tasks


def test_long_directory_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Over-long directories fail before any OpenCode call, never truncated."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    long_dir = "/tmp/" + "d" * server.TASK_DIRECTORY_MAX_CHARS
    assert len(long_dir) > server.TASK_DIRECTORY_MAX_CHARS
    with pytest.raises(ValueError, match="at most"):
        asyncio.run(server.worker_run("hi", directory=long_dir, requestID="req-long"))
    assert fake.created == []
    assert fake.prompted == []


def test_request_id_validation_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty or over-long request IDs fail without creating sessions."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    with pytest.raises(ValueError, match="requestID"):
        asyncio.run(server.worker_run("hi", requestID="  "))
    with pytest.raises(ValueError, match="requestID"):
        asyncio.run(server.worker_run("hi", requestID="x" * 200))
    assert fake.created == []


def test_legacy_omitted_request_id_still_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Omitting requestID keeps legacy behavior but still records the task."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    result = asyncio.run(server.worker_run("legacy hi", directory="/tmp/w"))
    assert result["deduplicated"] is False
    assert result["requestID"] is None
    assert result["taskID"] in json.loads(path.read_text())["tasks"]
    assert len(fake.created) == 1


def test_equivalent_directory_spelling_deduplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Trailing-separator retries hash identically and do not conflict."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    first = asyncio.run(server.worker_run("do X", directory="/tmp/w", requestID="req-eq"))
    second = asyncio.run(server.worker_run("do X", directory="/tmp/w/", requestID="req-eq"))
    assert second["deduplicated"] is True
    assert second["taskID"] == first["taskID"]
    assert len(fake.created) == 1


@pytest.mark.parametrize(
    "bad_content",
    [
        "not json{{{",
        "[]",
        "{}",
        '{"tasks": []}',
        '{"tasks": {"ses_1": "not-a-record"}}',
    ],
)
def test_corrupt_registry_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_content: str
) -> None:
    """Corrupt or mis-shaped registries raise before any OpenCode call."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    path.write_text(bad_content)
    secret = "secret-prompt-should-never-leak-abc123"
    with pytest.raises(RuntimeError, match="task registry") as exc_info:
        asyncio.run(server.worker_run(secret, directory="/tmp/w", requestID="req-x"))
    assert fake.created == []
    assert fake.prompted == []
    assert secret not in str(exc_info.value)


def test_save_failure_cleans_up_session_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed atomic save deletes the new session and never claims success."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    secret = "save-failure-secret-prompt-xyz789"

    def _failing_save(tasks: dict[str, Any]) -> None:
        raise RuntimeError("task registry is unwritable")

    monkeypatch.setattr(server, "_save_task_state", _failing_save)
    with pytest.raises(RuntimeError, match="unwritable") as exc_info:
        asyncio.run(server.worker_run(secret, directory="/tmp/w", requestID="req-save"))
    assert fake.deleted != []
    assert secret not in str(exc_info.value)


def test_concurrent_same_request_id_creates_one_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent retries with one requestID create a single session."""
    _state_file(monkeypatch, tmp_path)

    class _SlowClient(_FakeTaskClient):
        async def create_session(self, title: Any, directory: Any) -> dict[str, Any]:
            await asyncio.sleep(0.05)
            return await super().create_session(title, directory)

    fake = _SlowClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)

    async def run() -> list[dict[str, Any]]:
        return await asyncio.gather(
            server.worker_run("do X", directory="/tmp/w", requestID="req-race"),
            server.worker_run("do X", directory="/tmp/w", requestID="req-race"),
        )

    first, second = asyncio.run(run())
    assert len(fake.created) == 1
    assert first["taskID"] == second["taskID"]
    assert sorted([first["deduplicated"], second["deduplicated"]]) == [False, True]


def test_concurrent_distinct_tasks_keep_all_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent distinct tasks do not clobber each other's records."""
    path = _state_file(monkeypatch, tmp_path)

    class _SlowClient(_FakeTaskClient):
        async def create_session(self, title: Any, directory: Any) -> dict[str, Any]:
            await asyncio.sleep(0.02)
            return await super().create_session(title, directory)

    fake = _SlowClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)

    async def run() -> list[dict[str, Any]]:
        return await asyncio.gather(
            *(
                server.worker_run(f"msg-{i}", directory="/tmp/w", requestID=f"req-{i}")
                for i in range(5)
            )
        )

    results = asyncio.run(run())
    assert len(fake.created) == 5
    assert len({r["taskID"] for r in results}) == 5
    stored = json.loads(path.read_text())["tasks"]
    for result in results:
        assert result["taskID"] in stored


def test_retry_recreates_session_missing_from_opencode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A retry whose recorded session is gone recreates it, not dedup."""
    path = _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    first = asyncio.run(server.worker_run("do X", directory="/tmp/w", requestID="req-gone"))
    assert first["deduplicated"] is False
    fake.missing_sessions.add(first["taskID"])
    second = asyncio.run(server.worker_run("do X", directory="/tmp/w", requestID="req-gone"))
    assert second["deduplicated"] is False
    assert second["taskID"] != first["taskID"]
    assert len(fake.created) == 2
    stored = json.loads(path.read_text())["tasks"]
    assert second["taskID"] in stored
    assert first["taskID"] not in stored
    with pytest.raises(ValueError, match="different inputs"):
        asyncio.run(server.worker_run("other", directory="/tmp/w", requestID="req-gone"))
    assert len(fake.created) == 2


def test_retry_uncertain_liveness_keeps_stored_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-404 liveness failures return the stored task without side effects."""
    _state_file(monkeypatch, tmp_path)
    fake = _patch(monkeypatch)
    first = asyncio.run(server.worker_run("do X", directory="/tmp/w", requestID="req-flaky"))
    fake.session_error = OpencodeError("GET", "/session/ses_1", 500, "flaky")
    second = asyncio.run(server.worker_run("do X", directory="/tmp/w", requestID="req-flaky"))
    assert second["deduplicated"] is True
    assert second["taskID"] == first["taskID"]
    assert len(fake.created) == 1


def test_concurrent_atomic_saves_leave_valid_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent registry writes never interleave into corrupt JSON."""
    import concurrent.futures

    path = _state_file(monkeypatch, tmp_path)

    def _write(n: int) -> None:
        server._save_task_state({f"ses_{n}": {"taskID": f"ses_{n}"}})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(32)))
    data = json.loads(path.read_text())
    assert set(data) == {"version", "tasks"}
    assert isinstance(data["tasks"], dict)
    assert len(data["tasks"]) == 1
    assert list(path.parent.glob(".tasks.*.tmp")) == []
