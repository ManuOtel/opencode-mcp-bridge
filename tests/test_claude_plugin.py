"""Claude plugin packaging tests: nested plugin + root marketplace. No network."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "plugins" / "claude-code"
PLUGIN_MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
PLUGIN_MCP = PLUGIN_DIR / ".mcp.json"
SKILL = PLUGIN_DIR / "skills" / "coordinate-opencode-worker" / "SKILL.md"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
CODEX_MANIFEST = REPO / ".codex-plugin" / "plugin.json"
DOCS = REPO / "docs" / "client-setup.md"
README = REPO / "README.md"

WORKER_URL = "https://opencode-mcp.manuotel.com/worker-mcp"
TOKEN_REF = "${OPENCODE_MCP_BEARER_TOKEN}"


def test_claude_manifest_has_required_fields() -> None:
    """Nested Claude manifest carries name, version, description, skills, MCP."""
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert manifest["name"] == "opencode-worker"
    assert manifest["version"]
    assert manifest["description"]
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"


def test_claude_manifest_has_discovery_metadata() -> None:
    """Manifest carries author/homepage/repository/license/keywords."""
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert manifest["author"]["name"] == "ManuOtel"
    assert manifest["homepage"].startswith("https://")
    assert manifest["repository"].startswith("https://")
    assert manifest["license"]
    assert "claude-code" in manifest["keywords"]
    assert manifest["name"] == json.loads(CODEX_MANIFEST.read_text())["name"]


def test_claude_mcp_uses_env_var_reference_not_token() -> None:
    """Claude MCP config uses remote HTTP + env-var header, no hardcoded token."""
    raw = PLUGIN_MCP.read_text()
    config = json.loads(raw)
    server = config["mcpServers"]["opencode"]
    assert server["type"] == "http"
    assert server["url"] == WORKER_URL
    assert server["headers"]["Authorization"] == f"Bearer {TOKEN_REF}"
    assert TOKEN_REF in raw
    redacted = raw.replace(f"Bearer {TOKEN_REF}", "")
    assert "bearer" not in redacted.lower(), "no hardcoded bearer token allowed"


def test_claude_skill_frontmatter_and_behavior() -> None:
    """Skill has valid frontmatter and covers the coordinator behavior."""
    text = SKILL.read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == "coordinate-opencode-worker"
    assert meta["description"]
    flat = " ".join(text.split())
    for phrase in (
        "muse-spark-1.3-contributor-free",
        "worktree",
        "worker_status",
        "worker_verify",
        "worker_cleanup",
        "Never",
    ):
        assert phrase in text, phrase
    assert "sequentially" in flat or "one at a time" in flat


def test_claude_marketplace_lists_nested_plugin() -> None:
    """Root Claude marketplace lists the nested plugin via relative source."""
    data = json.loads(MARKETPLACE.read_text())
    assert data["owner"]["name"] == "ManuOtel"
    entries = [p for p in data["plugins"] if p["name"] == "opencode-worker"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "./plugins/claude-code"
    assert entry["version"] == json.loads(PLUGIN_MANIFEST.read_text())["version"]


def test_codex_root_plugin_untouched() -> None:
    """Codex manifest keeps its own skills/MCP paths; no Claude files inside."""
    manifest = json.loads(CODEX_MANIFEST.read_text())
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    raw = CODEX_MANIFEST.read_text()
    assert ".claude-plugin" not in raw
    assert "plugins/claude-code" not in raw


def test_docs_and_readme_cover_claude_marketplace() -> None:
    """Docs distinguish Codex vs Claude installs; no npm/Brew claim anywhere."""
    docs = DOCS.read_text()
    readme = README.read_text()
    assert "/plugin marketplace add ManuOtel/opencode-mcp-bridge" in docs
    assert "coordinate-opencode-worker" in docs
    assert "coordinate-opencode-worker" in readme
    assert ".claude-plugin/marketplace.json" in readme
    for text in (docs, readme):
        lowered = text.lower()
        assert "npm install" not in lowered
        assert "brew install" not in lowered
