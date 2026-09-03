"""Marketplace tests: Git-backed install path stays valid and documented. No network."""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARKETPLACE = REPO / ".agents" / "plugins" / "marketplace.json"
PLUGIN_MANIFEST = REPO / ".codex-plugin" / "plugin.json"
DOCS = REPO / "docs" / "client-setup.md"
README = REPO / "README.md"

ADD_COMMAND = "codex plugin marketplace add ManuOtel/opencode-mcp-bridge --ref master"
PLUGIN_NAME = "opencode-worker"
REPO_URL = "https://github.com/ManuOtel/opencode-mcp-bridge.git"
REF = "master"


def _marketplace() -> dict:
    return json.loads(MARKETPLACE.read_text())


def _plugin_entry() -> dict:
    data = _marketplace()
    entries = [p for p in data["plugins"] if p["name"] == PLUGIN_NAME]
    assert len(entries) == 1, "marketplace must expose opencode-worker exactly once"
    return entries[0]


def test_marketplace_file_is_valid_json_with_name() -> None:
    """Marketplace parses and identifies itself with a display name."""
    assert MARKETPLACE.is_file()
    data = _marketplace()
    assert data["name"]
    assert data["interface"]["displayName"]


def test_marketplace_exposes_root_plugin_via_git_url_source() -> None:
    """Root plugin uses a Git-backed root source: source=url, repo URL, ref master."""
    entry = _plugin_entry()
    source = entry["source"]
    assert source["source"] == "url"
    assert source["url"] == REPO_URL
    assert source["ref"] == REF
    assert "path" not in source, "root plugin needs no subdir path"


def test_marketplace_entry_has_policy_and_category() -> None:
    """Each entry carries install policy, auth policy, and a category."""
    entry = _plugin_entry()
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_INSTALL"
    assert entry["category"] == "Productivity"


def test_marketplace_matches_root_plugin_manifest() -> None:
    """Marketplace name/category track the root plugin manifest (no fork)."""
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    entry = _plugin_entry()
    assert entry["name"] == manifest["name"]
    assert entry["category"] == manifest["interface"]["category"]


def test_root_plugin_not_restructured_or_duplicated() -> None:
    """Root plugin stays at the repo root; no copied plugin directory."""
    assert PLUGIN_MANIFEST.is_file()
    assert not (REPO / "plugins" / PLUGIN_NAME).exists()


def test_docs_carry_exact_marketplace_add_command() -> None:
    """Client setup doc shows the exact CLI flow and the plugin to install."""
    text = DOCS.read_text()
    assert ADD_COMMAND in text
    assert PLUGIN_NAME in text
    assert ".agents/plugins/marketplace.json" in text


def test_docs_distinguish_codex_marketplace_from_claude_marketplace() -> None:
    """Docs separate the Codex and Claude marketplaces and keep the MCP helper."""
    text = DOCS.read_text()
    flat = " ".join(text.split())
    assert "claude mcp add --transport http" in text
    assert ".claude-plugin/marketplace.json" in text
    assert ".agents/plugins/marketplace.json" in text
    assert "no Claude plugin marketplace format in this repo" not in flat
    assert "source of truth" in text


def test_readme_points_at_marketplace_flow() -> None:
    """README shows the add command and links the detailed doc section."""
    text = README.read_text()
    assert ADD_COMMAND in text
    assert PLUGIN_NAME in text
    assert "docs/client-setup.md" in text
    # Endpoint, token, and opinionated skill guidance stay intact.
    assert "https://opencode-mcp.manuotel.com/worker-mcp" in text
    assert "OPENCODE_MCP_BEARER_TOKEN" in text
    assert "delegate-to-opencode" in text
