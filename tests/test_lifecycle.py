"""Focused tests for worker_verify, worker_cleanup, annotations, and profiles."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server


class _FakeLifecycleClient:
    """Minimal fake for verify/cleanup tests."""

    default_directory = "/home/tester"

    def __init__(self) -> None:
        self.aborted: list[tuple[Any, Any]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.abort_error: Exception | None = None
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.latest

    async def abort_session(self, session_id: str, directory: Any = None) -> bool:
        self.aborted.append((session_id, directory))
        if self.abort_error is not None:
            raise self.abort_error
        return True

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        self.deleted.append((session_id, directory))
        return True


def _patch(monkeypatch: pytest.MonkeyPatch) -> _FakeLifecycleClient:
    fake = _FakeLifecycleClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def _git_repo(tmp_path: Path, dirty: bool = True) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    if dirty:
        (tmp_path / "a.txt").write_text("one\ntwo\n")
    return tmp_path


def test_worker_verify_git_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify returns worker state plus a bounded git bundle."""
    fake = _patch(monkeypatch)
    fake.status_map = {"ses_1": {"type": "idle"}}
    fake.latest = {"messageID": "m1", "text": "done", "total_chars": 4, "has_error": False}
    repo = _git_repo(tmp_path)

    result = asyncio.run(server.worker_verify("ses_1", str(repo)))
    assert result["taskID"] == "ses_1"
    assert result["state"] == "idle"
    assert result["output"] == "done"
    bundle = result["verification"]
    assert bundle["ok"] is True
    assert bundle["directory"] == str(repo)
    assert "a.txt" in bundle["status_short"]
    assert isinstance(bundle["diff_stat"], str)
    assert bundle["diff_check"]["exit_code"] == 0
    assert "a.txt" in bundle["changed_files"]
    assert bundle["changed_count"] == len(bundle["changed_files"])
    assert bundle["error"] is None
    assert len(bundle["status_short"]) <= server.WORKER_VERIFY_GIT_MAX_CHARS
    assert len(bundle["changed_files"]) <= server.WORKER_VERIFY_GIT_MAX_FILES


def test_worker_verify_missing_and_nongit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Missing directories and non-git paths fail cleanly with ok=False."""
    fake = _patch(monkeypatch)
    fake.status_map = {}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        missing = await server.worker_verify("ses_x", str(tmp_path / "nope"))
        plain = tmp_path / "plain"
        plain.mkdir()
        nongit = await server.worker_verify("ses_x", str(plain))
        return missing, nongit

    missing, nongit = asyncio.run(run())
    assert missing["state"] == "unknown"
    assert missing["verification"]["ok"] is False
    assert missing["verification"]["error"] is not None
    assert nongit["verification"]["ok"] is False
    assert nongit["verification"]["changed_files"] == []


def test_worker_verify_rejects_empty_task_id() -> None:
    """Empty task IDs fail before any git work."""
    with pytest.raises(ValueError, match="taskID"):
        asyncio.run(server.worker_verify("  ", "/tmp"))


def test_worker_cleanup_abort_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Abort stops; delete aborts best-effort then deletes."""
    fake = _patch(monkeypatch)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        abort = await server.worker_cleanup("ses_1", "/tmp/w", action="abort")
        delete = await server.worker_cleanup("ses_1", "/tmp/w", action="delete")
        return abort, delete

    abort, delete = asyncio.run(run())
    assert abort == {
        "taskID": "ses_1",
        "sessionID": "ses_1",
        "action": "abort",
        "aborted": True,
        "deleted": False,
        "directory": "/tmp/w",
        "cleanup_warning": None,
    }
    assert delete["action"] == "delete"
    assert delete["aborted"] is True
    assert delete["deleted"] is True
    assert delete["cleanup_warning"] is None
    assert fake.aborted == [("ses_1", "/tmp/w"), ("ses_1", "/tmp/w")]
    assert fake.deleted == [("ses_1", "/tmp/w")]


def test_worker_cleanup_delete_reports_failed_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed pre-delete abort reports aborted=false with a bounded warning."""
    from opencode_mcp_bridge.opencode_client import OpencodeError

    fake = _patch(monkeypatch)
    fake.abort_error = OpencodeError("POST", "/session/ses_1/abort", 500, "boom-internal")
    result = asyncio.run(server.worker_cleanup("ses_1", "/tmp/w", action="delete"))
    assert result["action"] == "delete"
    assert result["aborted"] is False
    assert result["deleted"] is True
    assert isinstance(result["cleanup_warning"], str)
    assert len(result["cleanup_warning"]) <= server.WORKER_CLEANUP_WARNING_MAX_CHARS
    assert "boom-internal" not in (result["cleanup_warning"] or "")
    assert fake.deleted == [("ses_1", "/tmp/w")]


def test_run_git_preserves_leading_whitespace(tmp_path: Path) -> None:
    """Porcelain's leading status column must survive _run_git."""
    repo = _git_repo(tmp_path)
    code, out = asyncio.run(server._run_git(str(repo), ["status", "--short"]))
    assert code == 0
    assert " M a.txt" in out.splitlines()
    assert out.splitlines()[0].startswith(" M")


def test_parse_status_files_exact_columns() -> None:
    """Two status columns parse positionally, renames resolve to new path."""
    raw = " M a.txt\nM  b.txt\n?? c.txt\nR  old.txt -> new.txt\n"
    assert server._parse_status_files(raw) == ["a.txt", "b.txt", "c.txt", "new.txt"]
    assert server._parse_status_files(" M loner.txt\n") == ["loner.txt"]
    assert server._parse_status_files("") == []


def test_worker_verify_latest_commit_clean_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clean tree after a commit still carries latest-commit evidence."""
    fake = _patch(monkeypatch)
    fake.status_map = {"ses_1": {"type": "idle"}}
    fake.latest = {"messageID": "m1", "text": "done", "total_chars": 4, "has_error": False}
    repo = _git_repo(tmp_path, dirty=False)

    result = asyncio.run(server.worker_verify("ses_1", str(repo)))
    bundle = result["verification"]
    assert bundle["ok"] is True
    assert bundle["status_short"] == ""
    assert bundle["changed_files"] == []
    assert bundle["changed_count"] == 0
    assert isinstance(bundle["latest_commit"], str)
    assert bundle["latest_commit"] != ""
    assert "init" in bundle["latest_commit"]
    assert len(bundle["latest_commit"]) <= server.WORKER_VERIFY_COMMIT_MAX_CHARS


def test_worker_verify_latest_commit_shape_when_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing and non-git directories expose latest_commit as empty string."""
    fake = _patch(monkeypatch)
    fake.status_map = {}
    fake.latest = {"messageID": None, "text": "", "total_chars": 0, "has_error": False}

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        missing = await server.worker_verify("ses_x", str(tmp_path / "nope"))
        plain = tmp_path / "plain"
        plain.mkdir()
        nongit = await server.worker_verify("ses_x", str(plain))
        return missing, nongit

    missing, nongit = asyncio.run(run())
    assert missing["verification"]["latest_commit"] == ""
    assert nongit["verification"]["latest_commit"] == ""


def test_worker_cleanup_validates_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid action and empty IDs never touch the client."""
    fake = _patch(monkeypatch)
    with pytest.raises(ValueError, match="action"):
        asyncio.run(server.worker_cleanup("ses_1", action="nuke"))
    with pytest.raises(ValueError, match="taskID"):
        asyncio.run(server.worker_cleanup("  ", action="abort"))
    assert fake.aborted == []
    assert fake.deleted == []


EXPECTED_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "list_providers": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "list_agents": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "create_session": {"readOnly": False, "destructive": False, "idempotent": False, "open": False},
    "send_message": {"readOnly": False, "destructive": False, "idempotent": False, "open": False},
    "list_sessions": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "get_session": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "list_messages": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "abort_session": {"readOnly": False, "destructive": True, "idempotent": True, "open": False},
    "delete_session": {"readOnly": False, "destructive": True, "idempotent": False, "open": False},
    "get_diff": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "worker_run": {"readOnly": False, "destructive": False, "idempotent": False, "open": True},
    "worker_status": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "worker_catalog": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "exec_run": {"readOnly": False, "destructive": True, "idempotent": False, "open": True},
    "worker_verify": {"readOnly": True, "destructive": False, "idempotent": True, "open": False},
    "worker_cleanup": {"readOnly": False, "destructive": True, "idempotent": False, "open": False},
}


def test_tool_annotations_exposed_at_schema_level() -> None:
    """Annotations are exposed on the MCP wire schema via to_mcp_tool."""
    tools = asyncio.run(server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert set(EXPECTED_ANNOTATIONS) <= set(by_name)
    for name, expected in EXPECTED_ANNOTATIONS.items():
        wire = by_name[name].to_mcp_tool().annotations
        assert wire is not None, name
        assert wire.read_only_hint is expected["readOnly"], name
        assert wire.destructive_hint is expected["destructive"], name
        assert wire.idempotent_hint is expected["idempotent"], name
        assert wire.open_world_hint is expected["open"], name


def test_instructions_worker_first_and_bounded() -> None:
    """Instructions steer bosses to workers and stay under 512 chars."""
    text = server.WORKER_INSTRUCTIONS
    assert len(text) < 512
    for tool in (
        "worker_catalog",
        "worker_run",
        "worker_status",
        "worker_verify",
        "worker_cleanup",
    ):
        assert tool in text
    assert "advanced compatibility" in text


def test_dual_servers_list_expected_names() -> None:
    """Full server keeps all 16 tools; worker server exposes five only."""
    full_names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert full_names == set(server.ALL_TOOL_NAMES)
    assert set(server.WORKER_TOOL_NAMES) <= full_names
    assert "exec_run" in full_names
    worker_names = {t.name for t in asyncio.run(server.worker_mcp.list_tools())}
    assert worker_names == set(server.WORKER_TOOL_NAMES)
    assert "exec_run" not in worker_names
