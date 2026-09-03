"""Focused tests for the ENABLE_EXEC_RUN opt-in gate. No network."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import config, server


def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "tok")


def test_enable_exec_run_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """New installs fail closed when the flag is unset."""
    _secrets(monkeypatch)
    monkeypatch.delenv("ENABLE_EXEC_RUN", raising=False)
    monkeypatch.setattr(server, "_settings", None)
    assert config.load_settings().enable_exec_run is False
    assert server.get_settings().enable_exec_run is False


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
def test_enable_exec_run_opt_in_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Explicit truthy values opt in; anything else stays off."""
    _secrets(monkeypatch)
    monkeypatch.setenv("ENABLE_EXEC_RUN", value)
    monkeypatch.setattr(server, "_settings", None)
    assert config.load_settings().enable_exec_run is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_enable_exec_run_stays_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Falsy and empty values keep raw shell disabled."""
    _secrets(monkeypatch)
    monkeypatch.setenv("ENABLE_EXEC_RUN", value)
    monkeypatch.setattr(server, "_settings", None)
    assert config.load_settings().enable_exec_run is False


def test_exec_run_disabled_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled calls raise before any subprocess spawns."""
    _secrets(monkeypatch)
    monkeypatch.delenv("ENABLE_EXEC_RUN", raising=False)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)

    async def _no_spawn(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must not spawn when disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _no_spawn)
    with pytest.raises(RuntimeError, match="ENABLE_EXEC_RUN"):
        asyncio.run(server.exec_run("echo should-never-run"))


def test_exec_run_disabled_message_points_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disabled error names the flag and the recommended endpoint."""
    _secrets(monkeypatch)
    monkeypatch.setenv("ENABLE_EXEC_RUN", "false")
    monkeypatch.setattr(server, "_settings", None)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(server.exec_run("echo hi"))
    assert "ENABLE_EXEC_RUN=true" in str(exc.value)
    assert "/worker-mcp" in str(exc.value)


def test_exec_run_enabled_runs_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Opted-in hosts keep legacy shell behavior."""
    _secrets(monkeypatch)
    monkeypatch.setenv("ENABLE_EXEC_RUN", "true")
    monkeypatch.setattr(server, "_settings", None)
    result = asyncio.run(server.exec_run("echo gated-ok", workdir=str(tmp_path)))
    assert result["exit_code"] == 0
    assert "gated-ok" in result["stdout"]
    assert result["workdir"] == str(tmp_path)


def test_exec_tool_stays_listed_for_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gating is runtime-only: /mcp lists exec_run either way."""
    _secrets(monkeypatch)
    for flag in ("false", "true"):
        monkeypatch.setenv("ENABLE_EXEC_RUN", flag)
        monkeypatch.setattr(server, "_settings", None)
        full = {t.name for t in asyncio.run(server.mcp.list_tools())}
        worker = {t.name for t in asyncio.run(server.worker_mcp.list_tools())}
        assert "exec_run" in full
        assert "exec_run" not in worker
        assert worker == set(server.WORKER_TOOL_NAMES)
