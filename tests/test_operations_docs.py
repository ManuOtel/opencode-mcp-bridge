"""Operations runbook docs tests: linked, complete, placeholder-only. No network."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "operations.md"
README = REPO / "README.md"

MAINTAINER_URL = "https://opencode-mcp.manuotel.com/worker-mcp"


def _text() -> str:
    return DOC.read_text()


def test_operations_doc_exists_and_linked_from_readme() -> None:
    """Runbook lives at docs/operations.md and README points at it."""
    assert DOC.exists()
    assert "docs/operations.md" in README.read_text()


def test_clean_release_without_overwriting_dirty() -> None:
    """Worktree flow refuses dirty checkouts."""
    text = _text()
    assert "git worktree" in text
    assert "git status --short" in text
    assert "dirty" in text.lower()


def test_persistent_systemd_configuration() -> None:
    """Env file, state dir, daemon-reload, and enablement are explicit."""
    text = _text()
    assert "/etc/opencode-mcp-bridge/env" in text
    assert "StateDirectory" in text
    assert "daemon-reload" in text
    assert "enable --now" in text


def test_pre_post_deploy_checks() -> None:
    """Health, 401, initialize, tools/list counts, and worker_catalog."""
    text = _text()
    assert "/health" in text
    assert "401" in text
    assert "initialize" in text
    assert "tools/list" in text
    assert "worker_catalog" in text
    assert "16" in text and "5" in text
    assert "scripts/smoke.sh" in text


def test_rotation_rollback_and_logs() -> None:
    """Primary/secondary rotation, rollback, and status inspection."""
    text = _text()
    assert "MCP_BEARER_TOKEN" in text
    assert "MCP_BEARER_TOKEN_SECONDARY" in text
    assert "rollback" in text.lower()
    assert "journalctl" in text or "systemctl status" in text
    assert "docker compose logs" in text


def test_exec_run_warning_and_placeholders_only() -> None:
    """Warns /mcp may expose exec_run while /worker-mcp never does."""
    text = _text()
    flat = " ".join(text.split()).lower()
    assert "/mcp" in text and "/worker-mcp" in text
    assert "exec_run" in text
    assert "never" in flat and "opt-in" in flat or "opt in" in flat
    assert "https://<your-domain>" in text
    assert "$MCP_BEARER_TOKEN" in text or "MCP_BEARER_TOKEN" in text
    assert MAINTAINER_URL not in text
    assert "change-me" not in text
