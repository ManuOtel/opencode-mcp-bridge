"""Deterministic cross-process tests for the task registry file lock.

No network. No prompts or secrets are persisted or leaked in errors.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server


def _child_locked_add(state_path: str, task_id: str, timeout_s: float) -> None:
    """Child target: add one record under the advisory file lock."""
    os.environ["TASK_STATE_PATH"] = state_path
    with server._held_task_file_lock(timeout_s=timeout_s):
        tasks = server._load_task_state()
        # Read-modify-write under lock; small sleep widens the race
        # window so an unlocked interleaving would deterministically lose.
        time.sleep(0.05)
        tasks[task_id] = {"taskID": task_id}
        server._save_task_state(tasks)


def _child_lock_attempt(
    state_path: str, timeout_s: float, queue: mp.Queue, ready: mp.Event
) -> None:
    """Child target: try to acquire the lock and report the outcome."""
    os.environ["TASK_STATE_PATH"] = state_path
    ready.set()
    try:
        with server._held_task_file_lock(timeout_s=timeout_s):
            queue.put("acquired")
    except RuntimeError as exc:
        queue.put(f"timeout:{exc}")
    except Exception as exc:  # noqa: BLE001 - surfaced to parent for debugging
        queue.put(f"error:{type(exc).__name__}:{exc}")


def test_lock_path_is_sibling_lock_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The lock file is a sibling that never replaces the registry file."""
    state = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(state))
    assert server._task_lock_path() == state.parent / "tasks.json.lock"
    assert server._task_lock_path() != state


def test_cross_process_locked_writes_keep_all_records(tmp_path: Path) -> None:
    """N processes adding distinct records under lock lose nothing."""
    state = tmp_path / "tasks.json"
    ctx = mp.get_context("fork")
    workers = 6
    procs = [
        ctx.Process(target=_child_locked_add, args=(str(state), f"ses_{i}", 10.0))
        for i in range(workers)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(20)
        assert proc.exitcode == 0
    data = json.loads(state.read_text())
    assert set(data) == {"version", "tasks"}
    assert {f"ses_{i}" for i in range(workers)} <= set(data["tasks"])
    assert list(state.parent.glob(".tasks.*.tmp")) == []


def test_file_lock_timeout_fails_closed(tmp_path: Path) -> None:
    """A contended lock times out with a path-only error, registry intact."""
    state = tmp_path / "tasks.json"
    os.environ["TASK_STATE_PATH"] = str(state)
    ctx = mp.get_context("fork")
    queue: mp.Queue = ctx.Queue()
    ready: mp.Event = ctx.Event()
    child = ctx.Process(target=_child_lock_attempt, args=(str(state), 0.3, queue, ready))
    with server._held_task_file_lock(timeout_s=10.0):
        child.start()
        assert ready.wait(10)
        outcome = queue.get(timeout=10)
        child.join(10)
    assert child.exitcode == 0
    assert outcome.startswith("timeout:")
    assert "task registry" in outcome
    assert "busy" in outcome
    assert list(state.parent.glob(".tasks.*.tmp")) == []


def test_unsupported_platform_fails_closed_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing fcntl raises path-only errors without creating sessions."""

    class _FakeClient:
        default_provider_id = "opencode"
        default_model_id = "muse-spark-1.3-contributor-free"

        def __init__(self) -> None:
            self.created: list[tuple[object, object]] = []

        def resolve_model(self, provider_id: object, model_id: object) -> tuple[str, str]:
            return self.default_provider_id, self.default_model_id

        async def create_session(self, title: object, directory: object) -> dict[str, str]:
            self.created.append((title, directory))
            return {"id": "ses_1"}

    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)
    fake = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)

    def _no_fcntl() -> object:
        raise RuntimeError(f"task registry lock unavailable on this platform for {path}")

    monkeypatch.setattr(server, "_require_fcntl", _no_fcntl)
    secret = "lock-secret-prompt-xyz-987"
    with pytest.raises(RuntimeError, match="lock unavailable"):
        asyncio.run(server.worker_run(secret, directory="/tmp/w", requestID="req-lock"))
    assert fake.created == []
    assert secret not in str(path.read_text()) if path.exists() else True


def test_lock_timeout_in_worker_run_creates_no_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A busy registry makes worker_run fail closed with no side effects."""

    class _FakeClient:
        default_provider_id = "opencode"
        default_model_id = "muse-spark-1.3-contributor-free"

        def __init__(self) -> None:
            self.created: list[tuple[object, object]] = []

        def resolve_model(self, provider_id: object, model_id: object) -> tuple[str, str]:
            return self.default_provider_id, self.default_model_id

        async def create_session(self, title: object, directory: object) -> dict[str, str]:
            self.created.append((title, directory))
            return {"id": "ses_1"}

    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "TASK_LOCK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(server, "TASK_LOCK_POLL_S", 0.01)
    fake = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    secret = "busy-secret-prompt-should-never-leak-13579"
    with (
        server._held_task_file_lock(timeout_s=10.0),
        pytest.raises(RuntimeError, match="busy") as exc_info,
    ):
        asyncio.run(server.worker_run(secret, directory="/tmp/w", requestID="req-busy"))
    assert fake.created == []
    assert secret not in str(exc_info.value)


def test_locked_concurrent_async_tasks_keep_all_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent coroutines under the combined lock keep every record."""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)

    class _SlowClient:
        default_provider_id = "opencode"
        default_model_id = "muse-spark-1.3-contributor-free"

        def __init__(self) -> None:
            self.created = 0
            self._next = 0

        def resolve_model(self, provider_id: object, model_id: object) -> tuple[str, str]:
            return self.default_provider_id, self.default_model_id

        async def create_session(self, title: object, directory: object) -> dict[str, str]:
            await asyncio.sleep(0.02)
            self._next += 1
            self.created += 1
            return {"id": f"ses_{self._next}"}

        async def prompt_async(self, *args: object, **kwargs: object) -> bool:
            await asyncio.sleep(0.01)
            return True

        async def get_session(self, session_id: str, directory: object = None) -> dict[str, str]:
            return {"id": session_id}

    fake = _SlowClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)

    async def _run() -> list[dict[str, object]]:
        return await asyncio.gather(
            *(
                server.worker_run(f"msg-{i}", directory="/tmp/w", requestID=f"req-l-{i}")
                for i in range(5)
            )
        )

    results = asyncio.run(_run())
    assert fake.created == 5
    stored = json.loads(path.read_text())["tasks"]
    for result in results:
        assert result["taskID"] in stored
