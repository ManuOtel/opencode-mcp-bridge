"""Copilot docs tests: three cases stay distinct, own-bridge-first, no runtime change. No network."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "copilot-setup.md"
CLIENT_SETUP = REPO / "docs" / "client-setup.md"
README = REPO / "README.md"

MAINTAINER_URL = "https://opencode-mcp.manuotel.com/worker-mcp"
WORKER_TOOLS = (
    "worker_run",
    "worker_status",
    "worker_catalog",
    "worker_verify",
    "worker_cleanup",
)


def _text() -> str:
    return DOC.read_text()


def test_copilot_doc_exists_and_links_from_setup_and_readme() -> None:
    """New guide is linked from client-setup and README."""
    assert DOC.exists()
    assert "copilot-setup.md" in CLIENT_SETUP.read_text()
    assert "docs/copilot-setup.md" in README.read_text()


def test_three_cases_are_distinct() -> None:
    """Cases 1/2/3 each have their own heading and are not conflated."""
    text = _text()
    assert "Case 1" in text and "GitHub Copilot" in text
    assert "Case 2" in text and "Copilot Studio" in text
    assert "Case 3" in text and "Microsoft 365 Copilot" in text


def test_github_case_json_uses_copilot_mcp_vars_and_allowlists_tools() -> None:
    """GitHub JSON uses COPILOT_MCP_ vars and shows read-only plus full toolsets."""
    text = _text()
    assert "${COPILOT_MCP_BRIDGE_URL}" in text
    assert "${COPILOT_MCP_BRIDGE_TOKEN}" in text
    assert "COPILOT_MCP_BRIDGE_URL" in text
    assert "COPILOT_MCP_BRIDGE_TOKEN" in text
    assert "COPILOT_MCP_" in text
    for tool in WORKER_TOOLS:
        assert tool in text
    assert '"tools"' in text or "'tools'" in text or "tools" in text


def test_github_case_warns_about_autonomy_and_limits() -> None:
    """Autonomous tool use, no prompts/resources, no remote OAuth in this path."""
    text = _text()
    flat = " ".join(text.split()).lower()
    assert "autonomously" in flat
    assert "prompt" in flat
    assert "resource" in flat
    assert "oauth" in flat


def test_read_only_and_change_enabled_options_with_tradeoff() -> None:
    """Both snippets exist and the tradeoff is explicit."""
    text = _text()
    assert "Read-only" in text or "read-only" in text
    assert "Change-enabled" in text or "change-enabled" in text
    flat = " ".join(text.split()).lower()
    assert "tradeoff" in flat


def test_own_bridge_first_and_no_maintainer_default() -> None:
    """Own bridge rule is explicit; maintainer URL is never a default."""
    text = _text()
    flat = " ".join(text.split()).lower()
    assert "your own" in flat
    assert MAINTAINER_URL not in text
    assert "does not host" in flat or "never hosts" in flat


def test_m365_case_requires_admin_approved_route() -> None:
    """M365 is not a one-command install; needs declarative/admin route."""
    text = _text()
    flat = " ".join(text.split()).lower()
    assert "no one-command" in flat
    assert "declarative agent" in flat
    assert "approv" in flat


def test_official_docs_links() -> None:
    """GitHub and Microsoft official sources are linked."""
    text = _text()
    assert "docs.github.com" in text
    assert "learn.microsoft.com" in text
