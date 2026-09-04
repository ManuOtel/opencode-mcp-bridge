"""Onboarding tests: install helper safety/usage and client-setup docs. No network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "install-client.sh"
DOCS = REPO / "docs" / "client-setup.md"
README = REPO / "README.md"
CODEX_BUNDLE = REPO / ".mcp.json"

MAINTAINER_URL = "https://opencode-mcp.manuotel.com/worker-mcp"
PLACEHOLDER_HOST = "YOUR-BRIDGE-HOST"
BASH = shutil.which("bash") or "bash"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k != "OPENCODE_MCP_BEARER_TOKEN"}
    if env:
        base.update(env)
    return subprocess.run(
        [BASH, str(HELPER), *args], capture_output=True, text=True, env=base, check=False
    )


def _env_without_commands(tmp_path: Path, token: str) -> dict[str, str]:
    """Env with a canary token and a PATH containing no CLI binaries."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    return {"OPENCODE_MCP_BEARER_TOKEN": token, "PATH": str(empty_bin)}


def test_helper_syntax_ok() -> None:
    """The helper must parse cleanly under bash -n."""
    proc = subprocess.run(["bash", "-n", str(HELPER)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_helper_shellcheck_if_available() -> None:
    """Run shellcheck when installed; skip otherwise."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    proc = subprocess.run(
        ["shellcheck", "-S", "warning", str(HELPER)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_helper_has_no_eval_and_quotes_args() -> None:
    """No eval; server name and URL expansions stay quoted."""
    text = HELPER.read_text()
    assert "eval" not in text
    assert '"$NAME"' in text
    assert '"$MCP_URL"' in text


def test_helper_usage_text() -> None:
    """Usage covers modes, --name, required token env, required URL env."""
    proc = _run("--help", env={"OPENCODE_MCP_BEARER_TOKEN": "x"})
    assert proc.returncode == 0
    out = proc.stdout
    assert "codex" in out and "claude" in out and "both" in out
    assert "--name" in out
    assert "OPENCODE_MCP_BEARER_TOKEN" in out
    assert "OPENCODE_MCP_URL" in out
    assert "required" in out
    assert MAINTAINER_URL not in out, "usage must not default to the maintainer server"


def test_helper_requires_url_without_fallback() -> None:
    """Missing URL fails fast with a clear error and no silent fallback."""
    proc = _run("codex", env={"OPENCODE_MCP_BEARER_TOKEN": "canary-token-url001"})
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "OPENCODE_MCP_URL is required" in combined
    assert "canary-token-url001" not in combined
    assert MAINTAINER_URL not in combined


def test_helper_uses_explicit_url(tmp_path: Path) -> None:
    """A stub codex CLI receives exactly the URL from OPENCODE_MCP_URL."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    seen = tmp_path / "codex-args.txt"
    stub = bin_dir / "codex"
    stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {seen}\n")
    stub.chmod(0o755)
    url = "https://bridge.example.com/worker-mcp"
    proc = _run(
        "codex",
        env={
            "OPENCODE_MCP_BEARER_TOKEN": "canary-token-url002",
            "OPENCODE_MCP_URL": url,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert url in seen.read_text()
    assert "canary-token-url002" not in proc.stdout + proc.stderr


def test_codex_bundle_has_placeholder_not_maintainer_url() -> None:
    """Bundled Codex .mcp.json uses a visible placeholder, never the maintainer server."""
    import json

    raw = CODEX_BUNDLE.read_text()
    assert MAINTAINER_URL not in raw
    assert PLACEHOLDER_HOST in raw
    config = json.loads(raw)
    server = config["mcpServers"]["opencode"]
    assert server["bearer_token_env_var"] == "OPENCODE_MCP_BEARER_TOKEN"
    assert "worker_run" in server["tools"]


def test_helper_rejects_bad_mode() -> None:
    """Unknown mode fails with a clear error."""
    proc = _run("bogus", env={"OPENCODE_MCP_BEARER_TOKEN": "x"})
    assert proc.returncode != 0
    assert "codex, claude, both" in proc.stderr


def test_helper_requires_token_without_echoing() -> None:
    """Missing token fails fast and names the required variable."""
    proc = _run("codex", env={})
    assert proc.returncode != 0
    assert "OPENCODE_MCP_BEARER_TOKEN is required" in proc.stderr


def test_helper_never_echoes_token(tmp_path: Path) -> None:
    """A failing run with a canary token must not print the token value."""
    canary = "canary-token-abc123"
    proc = _run("claude", env=_env_without_commands(tmp_path, canary))
    assert proc.returncode != 0
    assert canary not in proc.stdout + proc.stderr


def test_helper_missing_command_fails_clearly(tmp_path: Path) -> None:
    """Absent codex/claude CLIs fail clearly without leaking the token."""
    canary = "canary-token-xyz789"
    env = _env_without_commands(tmp_path, canary)
    env["OPENCODE_MCP_URL"] = "https://bridge.example.com/worker-mcp"
    proc = _run("claude", env=env)
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert canary not in combined
    assert "not found: claude" in combined


def test_helper_warns_about_claude_token_persistence() -> None:
    """Helper source warns that Claude header config may persist the token."""
    text = HELPER.read_text()
    assert "may persist the token locally" in text


def test_docs_commands_and_urls() -> None:
    """Docs carry the exact copy/paste commands and both endpoint paths."""
    text = DOCS.read_text()
    assert "codex mcp add" in text
    assert "--url" in text
    assert "--bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN" in text
    assert "claude mcp add --transport http" in text
    assert '--header "Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN"' in text
    assert "$OPENCODE_MCP_URL" in text
    assert 'export OPENCODE_MCP_URL="https://<your-domain>/worker-mcp"' in text
    assert "/worker-mcp" in text
    assert "/mcp" in text
    assert "OPENCODE_MCP_BEARER_TOKEN" in text


def test_docs_distinguish_own_bridge_from_demo() -> None:
    """Docs explain own-bridge-first; the maintainer URL is opt-in only."""
    text = DOCS.read_text()
    flat = " ".join(text.split())
    assert "your own" in flat
    assert "opt-in only" in flat or "opt in explicitly" in flat
    assert "no fallback server" in flat or "no default server" in flat
    # The maintainer URL may appear solely as an explicit opt-in example.
    assert MAINTAINER_URL in text
    demo_idx = text.index("maintainer demo")
    assert text.index(MAINTAINER_URL) > demo_idx


def test_docs_explain_plugin_skills() -> None:
    """Docs make the opinionated plugin/skills behavior explicit."""
    text = DOCS.read_text()
    assert "opencode-worker" in text
    for skill in (
        "delegate-to-opencode",
        "verify-opencode-work",
        "recover-opencode-task",
        "opencode-git-workflow",
    ):
        assert skill in text
    assert "AGENTS.md" in text


def test_readme_quick_connect() -> None:
    """README links the setup doc and shows the one-command helper."""
    text = README.read_text()
    assert "Quick connect" in text
    assert "docs/client-setup.md" in text
    assert "scripts/install-client.sh" in text
    assert "OPENCODE_MCP_URL" in text
    assert MAINTAINER_URL not in text, "README generic path must not point at the maintainer server"
    # Opinionated skills/AGENTS.md references stay intact.
    assert "AGENTS.md" in text
    assert "delegate-to-opencode" in text
