"""Release coherence: published versions and changelog match the merged tree."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server

REPO = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
STALE_PHRASES = ("companion branch", "lands via", "if absent here")
WORKER_TOOLS = (
    "worker_run",
    "worker_status",
    "worker_catalog",
    "worker_verify",
    "worker_cleanup",
)


def _bridge_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_bridge_and_codex_plugin_share_version() -> None:
    """Bridge and Codex plugin publish together; their versions must match."""
    manifest = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text())
    assert SEMVER.match(_bridge_version()), "bridge version must be semver"
    assert SEMVER.match(manifest["version"]), "codex version must be semver"
    assert manifest["version"] == _bridge_version()


def test_claude_marketplace_tracks_claude_manifest() -> None:
    """Nested Claude plugin and its marketplace entry publish the same version."""
    manifest = json.loads(
        (REPO / "plugins" / "claude-code" / ".claude-plugin" / "plugin.json").read_text()
    )
    marketplace = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert SEMVER.match(manifest["version"]), "claude version must be semver"
    entries = [p for p in marketplace["plugins"] if p["name"] == "opencode-worker"]
    assert len(entries) == 1
    assert entries[0]["version"] == manifest["version"]


def test_all_worker_tools_are_merged() -> None:
    """All five worker tools exist on the server module (no branch-pending API)."""
    for name in WORKER_TOOLS:
        assert callable(getattr(server, name, None)), f"{name} must be defined"


def test_no_stale_branch_claims_in_docs() -> None:
    """README and CHANGELOG must not claim merged tools are branch-only."""
    for path in (REPO / "README.md", REPO / "CHANGELOG.md"):
        lowered = path.read_text().lower()
        for phrase in STALE_PHRASES:
            assert phrase not in lowered, f"{path.name} still claims: {phrase}"


def test_changelog_names_published_plugin_versions() -> None:
    """Changelog names the Codex and Claude plugin versions it publishes."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    codex = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text())["version"]
    claude = json.loads(
        (REPO / "plugins" / "claude-code" / ".claude-plugin" / "plugin.json").read_text()
    )["version"]
    assert codex in changelog
    assert claude in changelog
