"""OpenHands plugin packaging tests: native plugin + skill + MCP + guide.

Deterministic structural validation only. No network, no OpenHands runtime.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "plugins" / "openhands"
PLUGIN_MANIFEST = PLUGIN_DIR / ".plugin" / "plugin.json"
PLUGIN_MCP = PLUGIN_DIR / ".mcp.json"
SKILL = PLUGIN_DIR / "skills" / "coordinate-opencode-worker" / "SKILL.md"
README = PLUGIN_DIR / "README.md"
CODEX_MANIFEST = REPO / ".codex-plugin" / "plugin.json"
CLAUDE_MANIFEST = REPO / "plugins" / "claude-code" / ".claude-plugin" / "plugin.json"

URL_PLACEHOLDER = "https://YOUR-BRIDGE-HOST/worker-mcp"
URL_REF = "https://<your-domain>/worker-mcp"
TOKEN_REF = "${OPENCODE_MCP_BEARER_TOKEN}"
MAINTAINER_URL = "https://opencode-mcp.manuotel.com/worker-mcp"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

DOC_SOURCES = (
    "https://docs.openhands.dev/overview/plugins",
    "https://docs.openhands.dev/overview/skills",
    "https://docs.openhands.dev/openhands/usage/cli/mcp-servers",
    "https://docs.openhands.dev/openhands/usage/settings/mcp-settings",
)


def _bridge_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_manifest_is_native_openhands_shape() -> None:
    """Manifest uses OpenHands-native .plugin path with required metadata."""
    assert PLUGIN_MANIFEST.is_file()
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert manifest["name"] == "opencode-worker"
    assert SEMVER.match(manifest["version"]), "plugin version must be semver"
    assert manifest["description"]
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"


def test_manifest_tracks_bridge_version_without_bump() -> None:
    """OpenHands plugin publishes with the bridge version (no separate bump)."""
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert manifest["version"] == _bridge_version()


def test_manifest_has_discovery_metadata() -> None:
    """Manifest carries author/homepage/repository/license/keywords."""
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert manifest["author"]["name"] == "ManuOtel"
    assert manifest["homepage"].startswith("https://")
    assert manifest["repository"].startswith("https://")
    assert manifest["license"]
    assert "openhands" in manifest["keywords"]
    assert manifest["name"] == json.loads(CODEX_MANIFEST.read_text())["name"]
    assert manifest["name"] == json.loads(CLAUDE_MANIFEST.read_text())["name"]


def test_mcp_uses_safe_endpoint_with_placeholders_only() -> None:
    """MCP config points at safe /worker-mcp placeholder, no secrets."""
    raw = PLUGIN_MCP.read_text()
    config = json.loads(raw)
    server = config["mcpServers"]["opencode"]
    assert server["url"] == URL_PLACEHOLDER
    assert server["url"].endswith("/worker-mcp")
    assert server["transport"] == "http"
    assert server["headers"]["Authorization"] == f"Bearer {TOKEN_REF}"
    assert TOKEN_REF in raw
    assert MAINTAINER_URL not in raw, "must not point at any shared server"
    assert '/mcp"' not in raw.replace('/worker-mcp"', ""), "safe endpoint only"
    redacted = raw.replace(f"Bearer {TOKEN_REF}", "").replace(URL_PLACEHOLDER, "")
    assert "bearer" not in redacted.lower(), "no hardcoded bearer token allowed"
    for secret_hint in ("token_urlsafe", "sk-", "ghp_", "password"):
        assert secret_hint not in raw.lower()


def test_skill_frontmatter_and_behavior() -> None:
    """Skill follows AgentSkills shape and covers the coordinator behavior."""
    text = SKILL.read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == "coordinate-opencode-worker"
    assert meta["name"] == SKILL.parent.name
    assert meta["description"]
    for phrase in (
        "muse-spark-1.3-contributor-free",
        "opencode-go/muse-spark-1.3-contributor",
        "worker_catalog",
        "worker_run",
        "worker_wait",
        "worker_status",
        "worker_verify",
        "worker_cleanup",
        "worker_decide",
        "worker_resume",
        "worktree",
        "branch",
    ):
        assert phrase in text, phrase
    flat = " ".join(text.split())
    assert "sequentially" in flat or "one at a time" in flat
    assert "Never" in text
    assert "/worker-mcp" in text
    assert "exec_run" in text
    assert "eight worker tools" in flat
    assert "six worker tools" not in flat
    assert "five worker tools" not in flat
    assert "worker_wait" in text
    assert "30" in text
    assert "1-120" in text


def test_readme_is_self_serve_setup_guide() -> None:
    """Guide covers install, MCP registration, workflow, and doc sources."""
    text = README.read_text()
    assert "openhands --plugin" in text
    assert "openhands mcp add opencode --transport http" in text
    assert "~/.openhands/mcp.json" in text
    assert "shttp_servers" in text
    assert URL_PLACEHOLDER in text or "YOUR-BRIDGE-HOST" in text
    assert URL_REF in text
    assert TOKEN_REF in text
    assert "bring your own" in text.lower() or "your own bridge" in text.lower()
    assert "no shared" in text.lower() or "no shared server" in text.lower()
    assert MAINTAINER_URL not in text, "no shared server URL in package"
    for phrase in (
        "muse-spark-1.3-contributor-free",
        "opencode-go/muse-spark-1.3-contributor",
        "worktree",
        "worker_catalog",
        "worker_run",
        "worker_wait",
        "worker_status",
        "worker_verify",
        "worker_cleanup",
        "worker_decide",
        "worker_resume",
    ):
        assert phrase in text, phrase
    for source in DOC_SOURCES:
        assert source in text, source
    flat = " ".join(text.split())
    assert "eight worker tools" in flat
    assert "six worker tools" not in flat
    assert "five worker tools" not in flat
    assert "30" in text
    assert "1-120" in text


def test_package_stays_separate_from_codex_and_claude() -> None:
    """OpenHands files live only under plugins/openhands; others untouched."""
    assert PLUGIN_DIR.is_dir()
    assert not (REPO / "plugins" / "openhands" / ".claude-plugin").exists()
    codex_raw = CODEX_MANIFEST.read_text()
    assert "openhands" not in codex_raw.lower()
    assert "plugins/openhands" not in codex_raw
    claude_raw = CLAUDE_MANIFEST.read_text()
    assert "openhands" not in claude_raw.lower()
    assert "plugins/openhands" not in claude_raw
