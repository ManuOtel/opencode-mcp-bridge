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
    state_path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STATE_PATH", str(state_path))
    monkeypatch.setattr(server, "_settings", None)
    return state_path
