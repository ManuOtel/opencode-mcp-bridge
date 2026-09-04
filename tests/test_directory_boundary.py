"""Adversarial tests for server-side directory authorization. No network."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import config, server


def _configure(monkeypatch: pytest.MonkeyPatch, root: Path, extra_roots: str = "") -> str:
    """Point settings at a single allowed root (plus optional extras)."""
    root_str = str(root)
    allowed = root_str if not extra_roots else f"{root_str},{extra_roots}"
    monkeypatch.setenv("ALLOWED_DIRECTORIES", allowed)
    monkeypatch.setenv("DEFAULT_DIRECTORY", root_str)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return root_str


class _FakeBoundaryClient:
    """Minimal fake recording directory args for boundary tests."""

    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"

    def __init__(self, default_directory: str) -> None:
        self.default_directory = default_directory
        self.created: list[tuple[Any, Any]] = []
        self.status_dirs: list[Any] = []
        self.aborted: list[tuple[Any, Any]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }

    def resolve_model(self, provider_id: Any, model_id: Any) -> tuple[str, str]:
        return (
            provider_id or self.default_provider_id,
            model_id or self.default_model_id,
        )

    async def create_session(self, title: Any, directory: Any) -> dict[str, Any]:
        self.created.append((title, directory))
        return {"id": "ses_1", "title": title, "directory": directory}

    async def prompt_async(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        self.deleted.append((session_id, directory))
        return True

    async def abort_session(self, session_id: str, directory: Any = None) -> bool:
        self.aborted.append((session_id, directory))
        return True

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        self.status_dirs.append(directory)
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.latest

    async def get_session(self, session_id: str, directory: Any = None) -> dict[str, Any]:
        return {"id": session_id, "title": None, "directory": directory}

    async def list_agents(self, directory: Any = None) -> list[Any]:
        return []

    async def list_sessions(self, directory: Any = None, limit: int = 30) -> list[Any]:
        return []

    async def list_messages(
        self, session_id: str, directory: Any = None, limit: int = 50
    ) -> list[Any]:
        return []

    async def send_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"text": "ok"}

    async def get_diff(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


def _patch(monkeypatch: pytest.MonkeyPatch, root: str) -> _FakeBoundaryClient:
    fake = _FakeBoundaryClient(root)
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def test_config_defaults_to_default_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset ALLOWED_DIRECTORIES defaults to the normalized default."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")
    monkeypatch.delenv("ALLOWED_DIRECTORIES", raising=False)
    monkeypatch.setenv("DEFAULT_DIRECTORY", "/tmp/some-dir/")
    settings = config.load_settings()
    assert settings.default_directory == "/tmp/some-dir"
    assert settings.allowed_directories == ("/tmp/some-dir",)


def test_config_parses_comma_separated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Comma-separated roots parse with whitespace trimmed."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("DEFAULT_DIRECTORY", str(root_a))
    monkeypatch.setenv("ALLOWED_DIRECTORIES", f" {root_a} , {root_b} ")
    settings = config.load_settings()
    assert settings.allowed_directories == (str(root_a), str(root_b))


def test_config_empty_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty or blank ALLOWED_DIRECTORIES fails closed, never unrestricted."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")
    for bad in ("", "   ", " , , "):
        monkeypatch.setenv("ALLOWED_DIRECTORIES", bad)
        with pytest.raises(RuntimeError, match="ALLOWED_DIRECTORIES"):
            config.load_settings()


def test_config_default_outside_allowed_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A default outside the allowed roots fails closed at load time."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")
    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    monkeypatch.setenv("DEFAULT_DIRECTORY", str(other))
    monkeypatch.setenv("ALLOWED_DIRECTORIES", str(root))
    with pytest.raises(RuntimeError, match="DEFAULT_DIRECTORY"):
        config.load_settings()


def test_exact_root_and_child_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Exact root and a child below it authorize and normalize."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    root_str = _configure(monkeypatch, root)
    assert server._authorize_directory(root_str) == os.path.realpath(root_str)
    assert server._authorize_directory(str(child)) == os.path.realpath(str(child))


def test_traversal_escape_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """../ escapes resolving outside the root are rejected."""
    root = tmp_path / "root"
    root.mkdir()
    _configure(monkeypatch, root)
    with pytest.raises(ValueError, match="not within allowed"):
        server._authorize_directory(str(root / ".." / "evil"))
    with pytest.raises(ValueError, match="not within allowed"):
        server._authorize_directory(f"{root}/../evil2")


def test_sibling_prefix_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Sibling-prefix tricks (/root-evil vs /root) are rejected."""
    root = tmp_path / "root"
    root.mkdir()
    sibling = tmp_path / "root-evil"
    sibling.mkdir()
    _configure(monkeypatch, root)
    with pytest.raises(ValueError, match="not within allowed"):
        server._authorize_directory(str(sibling))
    with pytest.raises(ValueError, match="not within allowed"):
        server._authorize_directory(str(sibling / "child"))


def test_symlink_escape_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Symlinks inside the root pointing outside are rejected."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    _configure(monkeypatch, root)
    with pytest.raises(ValueError, match="not within allowed"):
        server._authorize_directory(str(link))
    with pytest.raises(ValueError, match="not within allowed"):
        server._authorize_directory(str(link / "secret.txt"))


def test_worker_run_allows_child_rejects_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """worker_run creates inside the root but fails before side effects outside."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    root_str = _configure(monkeypatch, root)
    fake = _patch(monkeypatch, root_str)
    result = asyncio.run(server.worker_run("hi", directory=str(child)))
    assert result["directory"] == os.path.realpath(str(child))
    assert fake.created[0][1] == os.path.realpath(str(child))
    created_before = list(fake.created)
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.worker_run("hi", directory=str(tmp_path / "evil")))
    assert fake.created == created_before


def test_worker_status_verify_cleanup_reject_outside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Status, verify, and cleanup reject outside directories without side effects."""
    root = tmp_path / "root"
    root.mkdir()
    root_str = _configure(monkeypatch, root)
    fake = _patch(monkeypatch, root_str)
    outside = str(tmp_path / "evil")
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.worker_status("ses_1", outside))
    assert fake.status_dirs == []
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.worker_verify("ses_1", outside))
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.worker_cleanup("ses_1", outside, action="abort"))
    assert fake.aborted == []


def test_exec_run_rejects_outside(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """exec_run authorizes workdir even when the shell gate is open."""
    root = tmp_path / "root"
    root.mkdir()
    root_str = _configure(monkeypatch, root)
    monkeypatch.setenv("ENABLE_EXEC_RUN", "true")
    monkeypatch.setattr(server, "_settings", None)
    result = asyncio.run(server.exec_run("echo ok", workdir=root_str))
    assert result["exit_code"] == 0
    assert result["workdir"] == os.path.realpath(root_str)
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.exec_run("echo no", workdir=str(tmp_path / "evil")))


def test_legacy_tools_reject_outside(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every legacy directory path enforces the same boundary (no /mcp bypass)."""
    root = tmp_path / "root"
    root.mkdir()
    root_str = _configure(monkeypatch, root)
    fake = _patch(monkeypatch, root_str)
    outside = str(tmp_path / "evil")

    async def run() -> None:
        with pytest.raises(ValueError, match="not within allowed"):
            await server.create_session("t", outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.send_message("ses_x", message="hi", directory=outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.list_sessions(outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.get_session("ses_x", outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.list_messages("ses_x", outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.abort_session("ses_x", outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.delete_session("ses_x", outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.get_diff("ses_x", directory=outside)
        with pytest.raises(ValueError, match="not within allowed"):
            await server.list_agents(outside)

    asyncio.run(run())
    assert fake.created == []
    assert fake.aborted == []
    assert fake.deleted == []


def test_errors_do_not_leak_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rejection messages carry no prompt, token, or password material."""
    root = tmp_path / "root"
    root.mkdir()
    _configure(monkeypatch, root)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "super-secret-token-abc")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "super-secret-pw-xyz")
    monkeypatch.setattr(server, "_settings", None)
    secret_prompt = "secret-prompt-hunter2-xyz-123"
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(server.worker_run(secret_prompt, directory=str(tmp_path / "evil")))
    message = str(exc_info.value)
    assert secret_prompt not in message
    assert "super-secret-token-abc" not in message
    assert "super-secret-pw-xyz" not in message
