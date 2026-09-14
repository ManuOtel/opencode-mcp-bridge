"""Adoption proof: compatibility matrix, first-call, BYO bridge, registry. No network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "compatibility.md"
REGISTRY_DOC = REPO / "docs" / "registry.md"
README = REPO / "README.md"
HELPER = REPO / "scripts" / "install-client.sh"
SERVER_JSON = REPO / "server.json"
GLAMA_JSON = REPO / "glama.json"

MAINTAINER_URL = "https://opencode-mcp.manuotel.com/worker-mcp"
WORKER_TOOLS = (
    "worker_catalog",
    "worker_run",
    "worker_wait",
    "worker_status",
    "worker_verify",
    "worker_cleanup",
    "worker_decide",
    "worker_resume",
)
HARNESSES = (
    "Codex",
    "Claude Code",
    "ChatGPT",
    "Cursor",
    "VS Code",
    "Gemini CLI",
    "OpenHands",
    "Pi",
    "Hermes",
)
SOURCES = (
    "developers.openai.com/codex/cli/reference",
    "docs.anthropic.com/en/docs/claude-code/mcp",
    "help.openai.com",
    "developers.openai.com/api/docs/guides/developer-mode",
    "cursor.com/docs/mcp",
    "code.visualstudio.com/docs/agents/reference/mcp-configuration",
    "google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html",
    "docs.openhands.dev/openhands/usage/settings/mcp-settings",
    "pi.dev/packages/pi-mcp-adapter",
    "hermes-agent.nousresearch.com/docs/reference/mcp-config-reference",
)
BANNED_APPROVAL_CLAIMS = (
    "officially approved",
    "verified publisher",
    "certified connector",
)


def _text() -> str:
    return DOC.read_text()


def test_compatibility_doc_exists_and_linked_from_readme() -> None:
    """Matrix doc exists and README harness setup points at it."""
    assert DOC.exists()
    assert "docs/compatibility.md" in README.read_text()


def test_matrix_lists_all_required_harnesses() -> None:
    """All nine named harnesses appear by name (Pi and Hermes share one row)."""
    text = _text()
    for harness in HARNESSES:
        assert harness in text, f"missing harness: {harness}"


def test_matrix_labels_status_and_sources() -> None:
    """Each row carries Automated/Manual/Unverified status plus a source URL."""
    text = _text()
    assert "Automated" in text
    assert "Manual" in text
    assert "Unverified" in text
    for source in SOURCES:
        assert source in text, f"missing source: {source}"


def test_matrix_states_validation_boundary() -> None:
    """Doc states automated wire proof is not end-to-end client proof."""
    flat = " ".join(_text().split()).lower()
    assert "validation boundary" in flat or "runtime validation boundary" in flat
    assert "do not prove" in flat or "does not prove" in flat
    assert "protocol-level" in flat


def test_no_approval_oauth_hosting_or_credential_claims() -> None:
    """Matrix and registry docs claim no approvals, OAuth, hosting, or creds."""
    combined = (_text() + "\n" + REGISTRY_DOC.read_text()).lower()
    for claim in BANNED_APPROVAL_CLAIMS:
        assert claim not in combined, f"banned claim: {claim}"
    assert "no approval" in combined
    assert "supplies no token" in _text() or "no token" in combined


def test_first_call_examples_use_safe_endpoint_and_placeholders() -> None:
    """Curl examples hit /worker-mcp with env-var Bearer, eight tools named."""
    text = _text()
    assert "tools/list" in text
    assert "worker_catalog" in text
    assert "https://<your-domain>/worker-mcp" in text
    assert "OPENCODE_MCP_BEARER_TOKEN" in text
    assert "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN" in text
    for tool in WORKER_TOOLS:
        assert tool in text
    assert "exec_run" in text
    assert "never" in " ".join(text.split()).lower()


def test_byo_bridge_wording_and_no_demo_default() -> None:
    """BYO wording is explicit; maintainer URL is opt-in only, never a default."""
    text = _text()
    flat = " ".join(text.split()).lower()
    assert "bring your own bridge" in flat
    assert "never point" in flat or "generic installs never" in flat
    assert "opt-in only" in flat
    assert "not for production" in flat
    assert MAINTAINER_URL in text
    assert "example.invalid" not in text
    first_call = text.index("## First call")
    live_check = text.index("## Opt-in live check")
    first_call_section = text[first_call:live_check]
    assert "https://<your-domain>/worker-mcp" in first_call_section
    assert MAINTAINER_URL not in first_call_section


def test_clean_install_and_opt_in_live_documented() -> None:
    """Dry-run is the clean check; smoke.sh is the opt-in live check only."""
    text = _text()
    assert "./scripts/install-client.sh both --dry-run" in text
    assert "./scripts/smoke.sh" in text
    assert "without network" in text or "network-free" in text


def test_registry_proof_is_metadata_only() -> None:
    """Registry section names server.json/glama.json without claiming approval."""
    text = _text()
    assert "server.json" in text
    assert "glama.json" in text
    assert "not submitted" in text.lower() or "not approved" in text.lower()
    metadata = json.loads(SERVER_JSON.read_text())
    assert metadata["name"] == "io.github.ManuOtel/opencode-mcp-bridge"
    assert metadata["remotes"][0]["url"] == MAINTAINER_URL
    glama = json.loads(GLAMA_JSON.read_text())
    assert "ManuOtel" in json.dumps(glama)
    for path in (SERVER_JSON, GLAMA_JSON):
        raw = path.read_text()
        assert "OPENCODE_MCP_BEARER_TOKEN" not in raw
        assert "paste-token" not in raw.lower()


def test_helper_dry_run_stays_network_free(tmp_path: Path) -> None:
    """Dry-run validates inputs with an empty PATH and never prints the token."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    canary = "canary-token-adoption-proof-001"
    url = "https://bridge.example.com/worker-mcp"
    env = {k: v for k, v in os.environ.items() if k != "OPENCODE_MCP_BEARER_TOKEN"}
    env.update(
        {
            "OPENCODE_MCP_BEARER_TOKEN": canary,
            "OPENCODE_MCP_URL": url,
            "PATH": str(empty_bin),
        }
    )
    bash = shutil.which("bash") or "bash"
    proc = subprocess.run(
        [bash, str(HELPER), "both", "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert url in proc.stdout
    assert canary not in combined


def test_readme_tool_allowlists_cover_all_worker_tools() -> None:
    """README Pi/Hermes allowlists and safe pattern name all eight worker tools."""
    text = README.read_text()
    for tool in WORKER_TOOLS:
        assert tool in text, f"README missing worker tool: {tool}"
    assert "worker_decide" in text
    assert "worker_resume" in text


def test_readme_openhands_example_uses_placeholder_not_expanded_token() -> None:
    """OpenHands CLI example uses a placeholder, never a shell-expanded token."""
    text = README.read_text()
    openhands_idx = text.index("### OpenHands")
    section = text[openhands_idx : openhands_idx + 1200]
    assert "<paste-token-here>" in section
    assert '"Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN"' not in section
