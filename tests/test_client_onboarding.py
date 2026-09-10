"""Onboarding tests: install helper safety/usage and client-setup docs. No network."""

from __future__ import annotations

import os
import shutil
import stat
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


def test_helper_is_executable() -> None:
    """The helper must keep its user execute bit for ./scripts/install-client.sh."""
    assert HELPER.stat().st_mode & stat.S_IXUSR, "scripts/install-client.sh must be 100755"


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
    assert "must start with http" in out
    assert "must end with" in out
    assert "${OPENCODE_MCP_BEARER_TOKEN}" in out
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


def test_helper_stores_claude_reference_not_token_value() -> None:
    """Helper passes a Bearer ${VAR} reference so the token value is never stored."""
    text = HELPER.read_text()
    assert "${OPENCODE_MCP_BEARER_TOKEN}" in text
    assert "--header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}'" in text
    assert '"Authorization: Bearer $OPENCODE_MCP_BEARER_TOKEN"' not in text
    # All MCP options (--header) must precede the server name and URL.
    assert (
        "claude mcp add --transport http "
        "--header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}'" in text
    )
    assert '"$NAME" "$MCP_URL" --header' not in text


def test_helper_rejects_malformed_url(tmp_path: Path) -> None:
    """URL without https scheme or without /mcp suffix fails clearly, no token leak."""
    canary = "canary-token-urlshape001"
    for bad_url, fragment in (
        ("bridge.example.com/worker-mcp", "must start with http"),
        ("https://bridge.example.com/tools", "must end with /worker-mcp or /mcp"),
    ):
        proc = _run(
            "codex",
            env={
                "OPENCODE_MCP_BEARER_TOKEN": canary,
                "OPENCODE_MCP_URL": bad_url,
                "PATH": f"{tmp_path}:/usr/bin:/bin",
            },
        )
        assert proc.returncode != 0
        combined = proc.stdout + proc.stderr
        assert fragment in combined
        assert canary not in combined


def test_helper_claude_passes_reference_not_value(tmp_path: Path) -> None:
    """A stub claude CLI receives the literal ${VAR} reference, never the token."""
    bin_dir = tmp_path / "bin-claude"
    bin_dir.mkdir(exist_ok=True)
    seen = tmp_path / "claude-args.txt"
    stub = bin_dir / "claude"
    stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {seen}\n")
    stub.chmod(0o755)
    canary = "canary-token-claude-ref001"
    url = "https://bridge.example.com/worker-mcp"
    proc = _run(
        "claude",
        env={
            "OPENCODE_MCP_BEARER_TOKEN": canary,
            "OPENCODE_MCP_URL": url,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
    )
    assert proc.returncode == 0, proc.stderr
    recorded = seen.read_text()
    assert "${OPENCODE_MCP_BEARER_TOKEN}" in recorded
    assert canary not in recorded
    assert canary not in proc.stdout + proc.stderr
    # --header must precede the server name and URL per Claude Code docs.
    lines = recorded.splitlines()
    assert "--header" in lines
    assert "Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}" in lines
    assert url in lines
    assert lines.index("--header") < lines.index("opencode")
    assert lines.index("--header") < lines.index(url)


def test_docs_commands_and_urls() -> None:
    """Docs carry the exact copy/paste commands and both endpoint paths."""
    text = DOCS.read_text()
    assert "codex mcp add" in text
    assert "--url" in text
    assert "--bearer-token-env-var OPENCODE_MCP_BEARER_TOKEN" in text
    assert "claude mcp add --transport http" in text
    assert "--header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}'" in text
    assert (
        "claude mcp add --transport http "
        "--header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}' "
        'opencode "$OPENCODE_MCP_URL"' in text
    )
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
    assert "may require its own token" in flat or "may require its token" in flat
    # The maintainer URL may appear solely as an explicit opt-in example.
    assert MAINTAINER_URL in text
    demo_idx = text.index("maintainer demo")
    assert text.index(MAINTAINER_URL) > demo_idx


def test_docs_explain_bridge_coordinates_not_replaces() -> None:
    """Docs state the bridge coordinates workers and needs its own OpenCode server."""
    text = DOCS.read_text()
    flat = " ".join(text.split()).lower()
    assert "does not replace opencode" in flat
    assert "opencode serve" in flat or "opencode web" in flat
    assert "no local stdio" in flat or "remote" in flat
    assert "streamable http" in flat


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
    flat = " ".join(text.split()).lower()
    assert "Quick connect" in text
    assert "docs/client-setup.md" in text
    assert "scripts/install-client.sh" in text
    assert "OPENCODE_MCP_URL" in text
    assert "does not replace opencode" in flat
    assert "no local stdio" in flat or "remote http only" in flat
    assert "may require its own token" in flat or "may require its token" in flat
    # Optional community demo is advertised, with a self-host/demo distinction.
    assert MAINTAINER_URL in text
    assert MAINTAINER_URL.startswith("https://")
    assert MAINTAINER_URL.endswith("/worker-mcp")
    assert "self-host for production" in flat
    assert "not for production" in flat
    assert "operated by manuotel" in flat
    assert "requires its own token" in flat or "never supplies a token" in flat
    # Generic quick-connect stays on the user's own bridge, never the demo.
    assert "your own bridge" in flat
    assert "opt-in only" in flat
    assert "never point" in flat
    quick_idx = text.index("Quick connect")
    assert MAINTAINER_URL not in text[quick_idx : quick_idx + 600]
    # Opinionated skills/AGENTS.md references stay intact.
    assert "AGENTS.md" in text
    assert "delegate-to-opencode" in text


def test_readme_claude_uses_env_var_reference() -> None:
    """README Claude examples use ${VAR} references, never a hardcoded token."""
    text = README.read_text()
    assert "--header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}'" in text
    assert '"Authorization: Bearer <token>"' not in text
    # All MCP options (--header) must precede the server name and URL.
    assert (
        "claude mcp add --transport http "
        "--header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}' "
        'opencode "$OPENCODE_MCP_URL"' in text
    )
    assert (
        "claude mcp add --transport http "
        "--header 'Authorization: Bearer ${OPENCODE_MCP_BEARER_TOKEN}' "
        'opencode-bridge "$OPENCODE_MCP_URL"' in text
    )


def _stub_clients(tmp_path: Path, marker_dir: Path) -> Path:
    """Create stub codex/claude CLIs that record invocation via marker files."""
    bin_dir = tmp_path / "bin-stubs"
    bin_dir.mkdir(exist_ok=True)
    for name in ("codex", "claude"):
        marker = marker_dir / f"{name}.invoked"
        stub = bin_dir / name
        stub.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
        stub.chmod(0o755)
    return bin_dir


def _dry_run_env(bin_dir: Path, token: str, url: str) -> dict[str, str]:
    return {
        "OPENCODE_MCP_BEARER_TOKEN": token,
        "OPENCODE_MCP_URL": url,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }


def test_helper_usage_mentions_dry_run() -> None:
    """Usage documents --dry-run as a non-mutating validation mode."""
    proc = _run("--help", env={"OPENCODE_MCP_BEARER_TOKEN": "x"})
    assert proc.returncode == 0
    assert "--dry-run" in proc.stdout


def test_helper_dry_run_codex_does_not_invoke_client(tmp_path: Path) -> None:
    """Dry-run for codex validates inputs without calling any client CLI."""
    marker_dir = tmp_path / "markers-codex"
    marker_dir.mkdir(exist_ok=True)
    bin_dir = _stub_clients(tmp_path, marker_dir)
    canary = "canary-token-dryrun-codex001"
    url = "https://bridge.example.com/worker-mcp"
    proc = _run("codex", "--dry-run", env=_dry_run_env(bin_dir, canary, url))
    assert proc.returncode == 0, proc.stderr
    assert not (marker_dir / "codex.invoked").exists()
    assert not (marker_dir / "claude.invoked").exists()
    combined = proc.stdout + proc.stderr
    assert "mode='codex'" in proc.stdout
    assert "opencode" in proc.stdout
    assert url in proc.stdout
    assert "OPENCODE_MCP_BEARER_TOKEN" in combined
    assert "not invoked" in combined
    assert canary not in combined


def test_helper_dry_run_claude_does_not_invoke_client(tmp_path: Path) -> None:
    """Dry-run for claude validates inputs without calling any client CLI."""
    marker_dir = tmp_path / "markers-claude"
    marker_dir.mkdir(exist_ok=True)
    bin_dir = _stub_clients(tmp_path, marker_dir)
    canary = "canary-token-dryrun-claude001"
    url = "https://bridge.example.com/mcp"
    proc = _run("claude", "--name", "custom", "--dry-run", env=_dry_run_env(bin_dir, canary, url))
    assert proc.returncode == 0, proc.stderr
    assert not (marker_dir / "codex.invoked").exists()
    assert not (marker_dir / "claude.invoked").exists()
    combined = proc.stdout + proc.stderr
    assert "mode='claude'" in proc.stdout
    assert "custom" in proc.stdout
    assert url in proc.stdout
    assert canary not in combined


def test_helper_dry_run_both_does_not_invoke_either(tmp_path: Path) -> None:
    """Dry-run for both modes invokes neither codex nor claude."""
    marker_dir = tmp_path / "markers-both"
    marker_dir.mkdir(exist_ok=True)
    bin_dir = _stub_clients(tmp_path, marker_dir)
    canary = "canary-token-dryrun-both001"
    url = "https://bridge.example.com/worker-mcp"
    proc = _run("both", "--dry-run", env=_dry_run_env(bin_dir, canary, url))
    assert proc.returncode == 0, proc.stderr
    assert not (marker_dir / "codex.invoked").exists()
    assert not (marker_dir / "claude.invoked").exists()
    assert "mode='both'" in proc.stdout
    assert url in proc.stdout
    assert canary not in proc.stdout + proc.stderr


def test_helper_dry_run_needs_no_client_binaries(tmp_path: Path) -> None:
    """Dry-run succeeds with an empty PATH (no codex/claude required)."""
    canary = "canary-token-dryrun-nobin001"
    url = "https://bridge.example.com/worker-mcp"
    env = _env_without_commands(tmp_path, canary)
    env["OPENCODE_MCP_URL"] = url
    proc = _run("both", "--dry-run", env=env)
    assert proc.returncode == 0, proc.stderr
    assert url in proc.stdout
    assert canary not in proc.stdout + proc.stderr


def test_helper_dry_run_validates_required_inputs(tmp_path: Path) -> None:
    """Dry-run still enforces token, URL, and URL shape checks."""
    url = "https://bridge.example.com/worker-mcp"
    proc = _run("codex", "--dry-run", env={"OPENCODE_MCP_URL": url})
    assert proc.returncode != 0
    assert "OPENCODE_MCP_BEARER_TOKEN is required" in proc.stderr

    canary = "canary-token-dryrun-req002"
    proc = _run("codex", "--dry-run", env={"OPENCODE_MCP_BEARER_TOKEN": canary})
    assert proc.returncode != 0
    assert "OPENCODE_MCP_URL is required" in proc.stdout + proc.stderr
    assert canary not in proc.stdout + proc.stderr

    for bad_url, fragment in (
        ("bridge.example.com/worker-mcp", "must start with http"),
        ("https://bridge.example.com/tools", "must end with /worker-mcp or /mcp"),
    ):
        proc = _run(
            "claude",
            "--dry-run",
            env={"OPENCODE_MCP_BEARER_TOKEN": canary, "OPENCODE_MCP_URL": bad_url},
        )
        assert proc.returncode != 0
        combined = proc.stdout + proc.stderr
        assert fragment in combined
        assert canary not in combined


def test_docs_dry_run_example() -> None:
    """Onboarding doc shows one truthful dry-run validation example."""
    text = DOCS.read_text()
    assert "./scripts/install-client.sh both --dry-run" in text
