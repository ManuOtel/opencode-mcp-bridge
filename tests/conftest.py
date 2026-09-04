"""Isolate durable task state per test. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server


@pytest.fixture(autouse=True)
def _isolated_task_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point TASK_STATE_PATH at a per-test file and drop cached settings."""
    import os

    state_path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(state_path))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", os.environ.get("OPENCODE_SERVER_PASSWORD", "pw"))
    monkeypatch.setenv("MCP_BEARER_TOKEN", os.environ.get("MCP_BEARER_TOKEN", "tok"))
    home = os.path.expanduser("~")
    monkeypatch.setenv("DEFAULT_DIRECTORY", os.environ.get("DEFAULT_DIRECTORY", "/home/tester"))
    monkeypatch.setenv(
        "ALLOWED_DIRECTORIES",
        os.environ.get("ALLOWED_DIRECTORIES", f"/tmp,/home/tester,{home}"),
    )
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return state_path
