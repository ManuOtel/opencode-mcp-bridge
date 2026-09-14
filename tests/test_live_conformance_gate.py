"""Offline gate checks for the live conformance harness. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.test_live_conformance as live_mod

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "live_conformance.sh"
DOC = REPO / "docs" / "operations.md"


def _clear_live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENCODE_MCP_LIVE_ENABLE",
        "OPENCODE_MCP_LIVE_WORKER_URL",
        "OPENCODE_MCP_LIVE_BEARER_TOKEN",
        "OPENCODE_MCP_LIVE_FULL_URL",
        "OPENCODE_MCP_LIVE_HEALTH_URL",
        "OPENCODE_MCP_LIVE_DIRECTORY",
        "OPENCODE_MCP_LIVE_WAIT_S",
        "MCP_URL",
        "MCP_BEARER_TOKEN",
        "MCP_LIVE_BEARER_TOKEN",
        "OPENCODE_MCP_BEARER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_live_config_requires_explicit_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker URL plus token alone never enables live; ENABLE=1 is required."""
    _clear_live_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_MCP_LIVE_WORKER_URL", "http://127.0.0.1:8087/worker-mcp")
    monkeypatch.setenv("OPENCODE_MCP_LIVE_BEARER_TOKEN", "live-token-001")
    assert live_mod._live_config() is None
    monkeypatch.setenv("OPENCODE_MCP_LIVE_ENABLE", "1")
    config = live_mod._live_config()
    assert config is not None
    assert config["worker_url"] == "http://127.0.0.1:8087/worker-mcp"


def test_live_config_ignores_generic_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic MCP_URL/MCP_BEARER_TOKEN values never launch a real worker."""
    _clear_live_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_MCP_LIVE_ENABLE", "1")
    monkeypatch.setenv("MCP_URL", "http://127.0.0.1:8087/worker-mcp")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "generic-token-002")
    monkeypatch.setenv("MCP_LIVE_BEARER_TOKEN", "generic-token-003")
    monkeypatch.setenv("OPENCODE_MCP_BEARER_TOKEN", "generic-token-004")
    assert live_mod._live_config() is None


def test_live_health_url_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling /health is the default; explicit override wins."""
    _clear_live_env(monkeypatch)
    worker = "http://127.0.0.1:8087/worker-mcp"
    assert live_mod._live_health_url(worker) == "http://127.0.0.1:8087/health"
    monkeypatch.setenv("OPENCODE_MCP_LIVE_HEALTH_URL", "http://127.0.0.1:8087/custom-health")
    assert live_mod._live_health_url(worker) == "http://127.0.0.1:8087/custom-health"


def test_assert_no_secret_redacts_token() -> None:
    """Secret-echo failures never include the raw token value."""
    token = "super-secret-live-token-005"
    with pytest.raises(AssertionError) as exc:
        live_mod._assert_no_secret({"echo": token}, token)
    assert token not in str(exc.value)
    live_mod._assert_no_secret({"ok": True}, token)


def test_live_config_repr_redacts_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest fixture displays must not leak the token."""
    _clear_live_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_MCP_LIVE_ENABLE", "1")
    monkeypatch.setenv("OPENCODE_MCP_LIVE_WORKER_URL", "http://127.0.0.1:8087/worker-mcp")
    monkeypatch.setenv("OPENCODE_MCP_LIVE_BEARER_TOKEN", "live-token-006")
    config = live_mod._live_config()
    assert config is not None
    assert "live-token-006" not in repr(config)
    assert "live-token-006" not in str(config)


def test_lifecycle_cleanup_prefers_canonical_directory() -> None:
    """Cleanup uses the server-returned directory with a safe fallback."""
    text = (REPO / "tests" / "test_live_conformance.py").read_text()
    assert 'task_dir = ""' in text
    assert 'cleanup_dir = task_dir or live["directory"]' in text


def test_shell_gate_uses_header_file_not_process_list() -> None:
    """Bearer token travels in a 0600 file, never in curl argv."""
    text = SCRIPT.read_text()
    assert '-H "Authorization: Bearer $TOKEN"' not in text
    assert "Authorization: Bearer $TOKEN" not in text or "@$AUTH_HEADER_FILE" in text
    assert '-H @"$AUTH_HEADER_FILE"' in text
    assert "chmod 600" in text
    assert "umask 077" in text


def test_shell_gate_redacts_failure_output() -> None:
    """Failure output is token-redacted and bounded."""
    text = SCRIPT.read_text()
    assert "sanitize_output" in text
    assert "[redacted]" in text
    assert "tail -n 20" in text
    assert 'grep -v -i "bearer"' not in text


def test_shell_gate_requires_enable_and_live_vars_only() -> None:
    """Gate requires ENABLE=1 and honors only OPENCODE_MCP_LIVE_* vars."""
    text = SCRIPT.read_text()
    assert "OPENCODE_MCP_LIVE_ENABLE" in text
    assert '!= "1"' in text or "!= '1'" in text
    assert "OPENCODE_MCP_LIVE_BEARER_TOKEN" in text
    assert "${MCP_URL" not in text
    assert "${MCP_BEARER_TOKEN" not in text


def test_shell_health_url_is_configurable() -> None:
    """Health URL override is supported and reported."""
    text = SCRIPT.read_text()
    assert "OPENCODE_MCP_LIVE_HEALTH_URL" in text
    assert "health_url=" in text


def test_docs_document_live_contract() -> None:
    """Runbook documents opt-in, health contract, and safe handling."""
    text = DOC.read_text()
    assert "OPENCODE_MCP_LIVE_ENABLE" in text
    assert "OPENCODE_MCP_LIVE_BEARER_TOKEN" in text
    assert "OPENCODE_MCP_LIVE_HEALTH_URL" in text
    assert "canonical" in text.lower()
    assert "0600" in text or "redacted" in text.lower()
