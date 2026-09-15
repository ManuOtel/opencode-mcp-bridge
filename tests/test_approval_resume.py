"""Approval and resume state contract: pause, decide, resume. No network."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server


class _FakeApprovalClient:
    """Minimal fake counting every OpenCode side effect."""

    default_directory = "/home/tester"
    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"

    def __init__(self) -> None:
        self.created: list[tuple[Any, Any]] = []
        self.prompted: list[tuple[Any, ...]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.aborted: list[tuple[Any, Any]] = []
        self.status_calls: list[Any] = []
        self.latest_calls: list[Any] = []
        self.get_session_calls: list[tuple[Any, Any]] = []
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

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        self.deleted.append((session_id, directory))
        return True

    async def abort_session(self, session_id: str, directory: Any = None) -> bool:
        self.aborted.append((session_id, directory))
        return True

    async def get_session(self, session_id: str, directory: Any = None) -> dict[str, Any]:
        self.get_session_calls.append((session_id, directory))
        return {"id": session_id, "title": None, "directory": directory}

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        self.status_calls.append(directory)
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.latest_calls.append(directory)
        return self.latest


def _patch(monkeypatch: pytest.MonkeyPatch) -> _FakeApprovalClient:
    fake = _FakeApprovalClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def _approve(
    task_id: str, token: str, decision: str = "approve", directory: Any = None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"taskID": task_id, "decision": decision, "approval_token": token}
    if directory is not None:
        kwargs["directory"] = directory
    return asyncio.run(server.worker_decide(**kwargs))


def _resume(task_id: str, token: str, message: str) -> dict[str, Any]:
    return asyncio.run(server.worker_resume(taskID=task_id, approval_token=token, message=message))


def test_pause_before_risky_action_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """requires_approval pauses with no session, no prompt, durable record."""
    fake = _patch(monkeypatch)
    result = asyncio.run(
        server.worker_run("deploy the thing", directory="/tmp/w", requires_approval=True)
    )
    assert result["state"] == "approval_required"
    assert result["approval_state"] == "approval_required"
    assert result["taskID"].startswith("apr_")
    assert result["sessionID"] is None
    assert result["approval_token"]
    assert result["expires_at"] > time.time()
    assert result["next_action"] == "worker_decide"
    assert fake.created == []
    assert fake.prompted == []
    assert fake.deleted == []


def test_pause_via_risky_action_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded risky_action descriptor also pauses without side effects."""
    fake = _patch(monkeypatch)
    result = asyncio.run(
        server.worker_run("migrate db", directory="/tmp/w", risky_action="deploy-prod")
    )
    assert result["state"] == "approval_required"
    assert result["risky_action"] == "deploy-prod"
    assert fake.created == []
    assert fake.prompted == []


def test_default_run_starts_immediately_without_approval_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy default path is unchanged: immediate start, null approval fields."""
    fake = _patch(monkeypatch)
    result = asyncio.run(server.worker_run("normal work", directory="/tmp/w"))
    assert result["state"] == "running"
    assert result["approval_state"] is None
    assert result["approval_token"] is None
    assert result["risky_action"] is None
    assert result["expires_at"] is None
    assert len(fake.created) == 1
    assert len(fake.prompted) == 1


def test_risky_action_too_long_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-long descriptors fail before any OpenCode call or record."""
    fake = _patch(monkeypatch)
    with pytest.raises(ValueError, match="risky_action"):
        asyncio.run(
            server.worker_run("hi", directory="/tmp/w", risky_action="x" * 500),
        )
    assert fake.created == []
    assert fake.prompted == []


def test_no_prompt_persisted_for_approvals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Paused approval records store hashes only, never prompt text."""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)
    _patch(monkeypatch)
    secret = "approval-secret-prompt-abc-987"
    asyncio.run(server.worker_run(secret, directory="/tmp/w", requires_approval=True))
    raw = path.read_text()
    assert secret not in raw
    data = json.loads(raw)
    record = next(iter(data["tasks"].values()))
    assert record["approval_state"] == "approval_required"
    assert "approval_token" in record


def test_decide_approve_without_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approve records the decision and still starts nothing."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    decided = _approve(created["taskID"], created["approval_token"], "approve")
    assert decided["state"] == "approved"
    assert decided["approval_state"] == "approved"
    assert decided["decision"] == "approve"
    assert decided["next_action"] == "worker_resume"
    assert "approval_token" not in decided
    assert fake.created == []
    assert fake.prompted == []


def test_decide_reject_blocks_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejected approvals can never resume."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    decided = _approve(created["taskID"], created["approval_token"], "reject")
    assert decided["state"] == "rejected"
    assert decided["approval_state"] == "rejected"
    with pytest.raises(ValueError, match="rejected"):
        _resume(created["taskID"], created["approval_token"], "risky")
    assert fake.created == []
    assert fake.prompted == []


def test_resume_starts_same_task_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approved resume creates one session and one prompt, state resumed."""
    fake = _patch(monkeypatch)
    created = asyncio.run(
        server.worker_run("do the deploy", directory="/tmp/w", requires_approval=True)
    )
    _approve(created["taskID"], created["approval_token"], "approve")
    resumed = _resume(created["taskID"], created["approval_token"], "do the deploy")
    assert resumed["state"] == "resumed"
    assert resumed["approval_state"] == "resumed"
    assert resumed["taskID"] == created["taskID"]
    assert resumed["sessionID"] == "ses_1"
    assert resumed["directory"] == os.path.realpath("/tmp/w")
    assert len(fake.created) == 1
    assert len(fake.prompted) == 1
    assert fake.prompted[0][1] == "do the deploy"


def test_resume_rejects_different_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume with different inputs fails with no session created."""
    fake = _patch(monkeypatch)
    created = asyncio.run(
        server.worker_run("original task", directory="/tmp/w", requires_approval=True)
    )
    _approve(created["taskID"], created["approval_token"], "approve")
    with pytest.raises(ValueError, match="same task|match"):
        _resume(created["taskID"], created["approval_token"], "different task")
    assert fake.created == []
    assert fake.prompted == []


def test_resume_requires_prior_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume before decide fails with no side effects."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    with pytest.raises(ValueError, match="pending|approve first"):
        _resume(created["taskID"], created["approval_token"], "risky")
    assert fake.created == []
    assert fake.prompted == []


def test_duplicate_decide_rejected_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second decision on the same task fails without state change."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    _approve(created["taskID"], created["approval_token"], "approve")
    with pytest.raises(ValueError, match="already decided"):
        _approve(created["taskID"], created["approval_token"], "reject")
    status = asyncio.run(server.worker_status(created["taskID"]))
    assert status["state"] == "approved"
    assert fake.created == []
    assert fake.prompted == []


def test_duplicate_resume_rejected_without_second_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resuming twice fails; only one session ever starts."""
    fake = _patch(monkeypatch)
    created = asyncio.run(
        server.worker_run("once only", directory="/tmp/w", requires_approval=True)
    )
    _approve(created["taskID"], created["approval_token"], "approve")
    first = _resume(created["taskID"], created["approval_token"], "once only")
    with pytest.raises(ValueError, match="already resumed"):
        _resume(created["taskID"], created["approval_token"], "once only")
    assert first["sessionID"] == "ses_1"
    assert len(fake.created) == 1
    assert len(fake.prompted) == 1


def test_mismatched_token_rejected_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrong tokens fail on decide and resume with no state change."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    with pytest.raises(ValueError, match="match"):
        _approve(created["taskID"], "wrong-token", "approve")
    with pytest.raises(ValueError, match="match"):
        _resume(created["taskID"], "wrong-token", "risky")
    status = asyncio.run(server.worker_status(created["taskID"]))
    assert status["state"] == "approval_required"
    assert fake.created == []
    assert fake.prompted == []


def test_unknown_task_ids_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide and resume on unknown IDs fail without side effects."""
    fake = _patch(monkeypatch)
    with pytest.raises(ValueError, match="unknown"):
        _approve("apr_missing", "tok", "approve")
    with pytest.raises(ValueError, match="unknown"):
        _resume("apr_missing", "tok", "msg")
    assert fake.created == []
    assert fake.prompted == []


def test_expiration_blocks_decide_and_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past-deadline approvals report expired and never start work."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("slow", directory="/tmp/w", requires_approval=True))
    tasks = server._load_task_state()
    tasks[created["taskID"]]["expires_at"] = time.time() - 1.0
    server._save_task_state(tasks)
    status = asyncio.run(server.worker_status(created["taskID"]))
    assert status["state"] == "expired"
    assert status["approval_state"] == "expired"
    assert status["error_code"] == "approval_expired"
    with pytest.raises(ValueError, match="expired"):
        _approve(created["taskID"], created["approval_token"], "approve")
    with pytest.raises(ValueError, match="expired"):
        _resume(created["taskID"], created["approval_token"], "slow")
    assert fake.created == []
    assert fake.prompted == []


def test_approved_but_unresumed_can_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approvals expire when resume comes too late, even after decide."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("slow", directory="/tmp/w", requires_approval=True))
    _approve(created["taskID"], created["approval_token"], "approve")
    tasks = server._load_task_state()
    tasks[created["taskID"]]["expires_at"] = time.time() - 1.0
    server._save_task_state(tasks)
    with pytest.raises(ValueError, match="expired"):
        _resume(created["taskID"], created["approval_token"], "slow")
    assert fake.created == []
    assert fake.prompted == []


def test_request_id_dedup_returns_same_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same requestID plus same inputs returns the paused task, no duplicate."""
    fake = _patch(monkeypatch)
    first = asyncio.run(
        server.worker_run("risky", directory="/tmp/w", requestID="req-a", requires_approval=True)
    )
    second = asyncio.run(
        server.worker_run("risky", directory="/tmp/w", requestID="req-a", requires_approval=True)
    )
    assert second["deduplicated"] is True
    assert second["taskID"] == first["taskID"]
    assert second["approval_token"] == first["approval_token"]
    assert fake.created == []
    assert fake.prompted == []


def test_request_id_conflict_fails_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conflicting reuse of a requestID fails without new records or sessions."""
    fake = _patch(monkeypatch)
    asyncio.run(
        server.worker_run("one", directory="/tmp/w", requestID="req-c", requires_approval=True)
    )
    with pytest.raises(ValueError, match="different inputs"):
        asyncio.run(
            server.worker_run("two", directory="/tmp/w", requestID="req-c", requires_approval=True)
        )
    with pytest.raises(ValueError, match="different inputs"):
        asyncio.run(server.worker_run("one", directory="/tmp/w", requestID="req-c"))
    assert fake.created == []
    assert fake.prompted == []


def test_restart_keeps_approval_then_resumes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Registry reload across a restart preserves pause, decide, and one resume."""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)
    fake1 = _FakeApprovalClient()
    monkeypatch.setattr(server, "get_client", lambda: fake1)
    created = asyncio.run(
        server.worker_run("restart me", directory="/tmp/w", requires_approval=True)
    )
    assert path.is_file()
    fake2 = _FakeApprovalClient()
    monkeypatch.setattr(server, "get_client", lambda: fake2)
    decided = asyncio.run(
        server.worker_decide(
            taskID=created["taskID"], decision="approve", approval_token=created["approval_token"]
        )
    )
    assert decided["state"] == "approved"
    assert fake2.created == []
    resumed = asyncio.run(
        server.worker_resume(
            taskID=created["taskID"], approval_token=created["approval_token"], message="restart me"
        )
    )
    assert resumed["state"] == "resumed"
    assert len(fake2.created) == 1
    assert len(fake2.prompted) == 1
    fake3 = _FakeApprovalClient()
    monkeypatch.setattr(server, "get_client", lambda: fake3)
    with pytest.raises(ValueError, match="already resumed"):
        asyncio.run(
            server.worker_resume(
                taskID=created["taskID"],
                approval_token=created["approval_token"],
                message="restart me",
            )
        )
    assert fake3.created == []


def test_status_reports_pending_without_opencode_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker_status on a paused task never touches OpenCode."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    fake.status_calls.clear()
    fake.latest_calls.clear()
    status = asyncio.run(server.worker_status(created["taskID"]))
    assert status["state"] == "approval_required"
    assert status["approval_state"] == "approval_required"
    assert status["risky_action"] is None
    assert status["expires_at"] == created["expires_at"]
    assert status["next_action"] == "worker_decide"
    assert "approval_token" not in status
    assert fake.status_calls == []
    assert fake.latest_calls == []


def test_status_reports_resumed_live_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """After resume, status polls the live session and keeps approval_state."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("live", directory="/tmp/w", requires_approval=True))
    _approve(created["taskID"], created["approval_token"], "approve")
    resumed = _resume(created["taskID"], created["approval_token"], "live")
    fake.status_map = {resumed["sessionID"]: {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    status = asyncio.run(server.worker_status(created["taskID"]))
    assert status["approval_state"] == "resumed"
    assert status["state"] == "running"
    assert status["taskID"] == created["taskID"]


def test_wait_returns_immediately_for_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker_wait never polls on approvals; it returns the snapshot at once."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    waited = asyncio.run(server.worker_wait(created["taskID"], timeout_s=5))
    assert waited["state"] == "approval_required"
    assert waited["changed"] is False
    assert waited["timed_out"] is False
    assert waited["approval_state"] == "approval_required"
    assert fake.status_calls == []


def test_cleanup_delete_removes_pending_without_opencode_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete on a paused approval drops the record with no session calls."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    result = asyncio.run(server.worker_cleanup(created["taskID"], "/tmp/w", action="delete"))
    assert result["deleted"] is True
    assert result["aborted"] is False
    assert result["sessionID"] is None
    assert fake.aborted == []
    assert fake.deleted == []
    with pytest.raises(ValueError, match="unknown"):
        _approve(created["taskID"], created["approval_token"], "approve")


def test_cleanup_abort_on_pending_is_safe_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Abort before start reports no-op without touching OpenCode."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    result = asyncio.run(server.worker_cleanup(created["taskID"], "/tmp/w", action="abort"))
    assert result["aborted"] is False
    assert result["deleted"] is False
    assert fake.aborted == []


def test_directory_mismatch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide and resume with a non-matching directory fail safely."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("risky", directory="/tmp/w", requires_approval=True))
    with pytest.raises(ValueError, match="match"):
        _approve(created["taskID"], created["approval_token"], "approve", "/tmp/other")
    _approve(created["taskID"], created["approval_token"], "approve")
    with pytest.raises(ValueError, match="match"):
        asyncio.run(
            server.worker_resume(
                taskID=created["taskID"],
                approval_token=created["approval_token"],
                message="risky",
                directory="/tmp/other",
            )
        )
    assert fake.created == []
    assert fake.prompted == []


def test_new_tools_have_explicit_schemas_and_annotations() -> None:
    """worker_decide and worker_resume expose object schemas and safe hints."""
    tools = asyncio.run(server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    for name in ("worker_decide", "worker_resume"):
        assert name in by_name
        assert name in server.WORKER_TOOL_NAMES
        assert name in server.ALL_TOOL_NAMES
        wire = by_name[name].to_mcp_tool()
        assert wire.annotations is not None
        assert wire.annotations.read_only_hint is False
        assert wire.annotations.destructive_hint is False
        assert wire.annotations.idempotent_hint is False
    assert server.WORKER_DECIDE_OUTPUT_SCHEMA["type"] == "object"
    assert server.WORKER_RESUME_OUTPUT_SCHEMA["type"] == "object"
    worker_tools = asyncio.run(server.worker_mcp.list_tools())
    worker_names = {t.name for t in worker_tools}
    assert {"worker_decide", "worker_resume"} <= worker_names
    assert "exec_run" not in worker_names


def test_symlink_and_dotdot_spellings_resume_same_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Symlink and a/../b spellings fingerprint the canonical directory."""
    fake = _patch(monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    created = asyncio.run(
        server.worker_run("link work", directory=str(link), requires_approval=True)
    )
    assert created["directory"] == os.path.realpath(link)
    _approve(created["taskID"], created["approval_token"], "approve")
    resumed = _resume(created["taskID"], created["approval_token"], "link work")
    assert resumed["state"] == "resumed"
    assert resumed["directory"] == os.path.realpath(real)
    assert len(fake.created) == 1

    dotted = os.path.join(str(tmp_path), "a", "..", real.name)
    created2 = asyncio.run(
        server.worker_run("dotdot work", directory=dotted, requires_approval=True)
    )
    assert created2["directory"] == os.path.realpath(dotted)
    _approve(created2["taskID"], created2["approval_token"], "approve")
    resumed2 = asyncio.run(
        server.worker_resume(
            taskID=created2["taskID"],
            approval_token=created2["approval_token"],
            message="dotdot work",
            directory=str(real),
        )
    )
    assert resumed2["state"] == "resumed"
    assert resumed2["directory"] == os.path.realpath(real)
    assert len(fake.created) == 2


def test_over_cap_title_and_agent_approve_then_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-cap titles/agents hash like the bounded stored values."""
    fake = _patch(monkeypatch)
    big_title = "t" * (server.TASK_TITLE_MAX_CHARS + 300)
    big_agent = "a" * (server.TASK_AGENT_MAX_CHARS + 50)
    created = asyncio.run(
        server.worker_run(
            "big meta",
            directory="/tmp/w",
            title=big_title,
            agent=big_agent,
            requestID="req-big-meta",
            requires_approval=True,
        )
    )
    stored = server._load_task_state()[created["taskID"]]
    assert stored["title"] == big_title[: server.TASK_TITLE_MAX_CHARS]
    assert stored["agent"] == big_agent[: server.TASK_AGENT_MAX_CHARS]
    second = asyncio.run(
        server.worker_run(
            "big meta",
            directory="/tmp/w",
            title=big_title,
            agent=big_agent,
            requestID="req-big-meta",
            requires_approval=True,
        )
    )
    assert second["deduplicated"] is True
    assert second["taskID"] == created["taskID"]
    _approve(created["taskID"], created["approval_token"], "approve")
    resumed = _resume(created["taskID"], created["approval_token"], "big meta")
    assert resumed["state"] == "resumed"
    assert resumed["title"] == big_title[: server.TASK_TITLE_MAX_CHARS]
    assert resumed["agent"] == big_agent[: server.TASK_AGENT_MAX_CHARS]
    assert len(fake.created) == 1
    assert len(fake.prompted) == 1


def test_empty_worker_run_message_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty worker_run messages fail like worker_resume, with no session."""
    fake = _patch(monkeypatch)
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="message must not be empty"):
            asyncio.run(server.worker_run(blank, directory="/tmp/w"))
        with pytest.raises(ValueError, match="message must not be empty"):
            asyncio.run(server.worker_run(blank, directory="/tmp/w", requires_approval=True))
    assert fake.created == []
    assert fake.prompted == []


def test_wait_on_resumed_long_polls_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker_wait on a resumed task polls until the bounded deadline."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("live", directory="/tmp/w", requires_approval=True))
    _approve(created["taskID"], created["approval_token"], "approve")
    resumed = _resume(created["taskID"], created["approval_token"], "live")
    fake.status_map = {resumed["sessionID"]: {"type": "busy"}}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
    fake.status_calls.clear()
    start = time.monotonic()
    waited = asyncio.run(server.worker_wait(created["taskID"], timeout_s=1))
    elapsed = time.monotonic() - start
    assert waited["taskID"] == created["taskID"]
    assert waited["sessionID"] == resumed["sessionID"]
    assert waited["state"] == "running"
    assert waited["approval_state"] == "resumed"
    assert waited["timed_out"] is True
    assert waited["changed"] is False
    assert waited["timeout_s"] == 1
    assert elapsed >= 0.9
    assert len(fake.status_calls) >= 2


def test_wait_on_resumed_returns_on_live_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker_wait on a resumed task returns early when output progresses."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("live", directory="/tmp/w", requires_approval=True))
    _approve(created["taskID"], created["approval_token"], "approve")
    resumed = _resume(created["taskID"], created["approval_token"], "live")
    fake.status_map = {resumed["sessionID"]: {"type": "busy"}}
    calls = {"n": 0}

    async def _changing_latest(
        session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 2:
            return {"messageID": None, "text": "", "total_chars": 0, "has_error": False}
        return {"messageID": "m1", "text": "done", "total_chars": 4, "has_error": False}

    monkeypatch.setattr(fake, "get_latest_assistant", _changing_latest)
    start = time.monotonic()
    waited = asyncio.run(server.worker_wait(created["taskID"], timeout_s=5))
    elapsed = time.monotonic() - start
    assert waited["taskID"] == created["taskID"]
    assert waited["approval_state"] == "resumed"
    assert waited["changed"] is True
    assert waited["timed_out"] is False
    assert waited["messageID"] == "m1"
    assert elapsed < 5


def test_cleanup_delete_on_expired_pending_reports_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete on an expired pending approval reports state expired."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("slow", directory="/tmp/w", requires_approval=True))
    tasks = server._load_task_state()
    tasks[created["taskID"]]["expires_at"] = time.time() - 1.0
    server._save_task_state(tasks)
    result = asyncio.run(server.worker_cleanup(created["taskID"], "/tmp/w", action="delete"))
    assert result["state"] == "expired"
    assert result["deleted"] is True
    assert result["aborted"] is False
    assert result["sessionID"] is None
    assert result["evidence"]["status"] == "expired"
    assert fake.aborted == []
    assert fake.deleted == []
    with pytest.raises(ValueError, match="unknown"):
        _approve(created["taskID"], created["approval_token"], "approve")


def test_registry_file_stays_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Registry creation and replacement keep owner-only permissions."""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(path))
    monkeypatch.setattr(server, "_settings", None)
    _patch(monkeypatch)
    asyncio.run(server.worker_run("perm check", directory="/tmp/w", requires_approval=True))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    os.chmod(path, 0o644)
    asyncio.run(server.worker_run("perm check again", directory="/tmp/w"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
