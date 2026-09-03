"""Environment-based configuration for the bridge.

Reads settings from the environment (or a .env file when present).
Required secrets raise RuntimeError with no secret values in the message.

Usage:
    from opencode_mcp_bridge.config import load_settings
    settings = load_settings()
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(dotenv_path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ without overwriting."""
    path = dotenv_path or Path.cwd() / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    """Bridge settings. All secrets come from the environment."""

    opencode_base_url: str
    opencode_username: str
    opencode_password: str
    mcp_bearer_token: str
    mcp_host: str
    mcp_port: int
    default_directory: str
    default_provider_id: str
    default_model_id: str
    exec_timeout_s: int
    exec_max_output_chars: int
    tool_profile: str


TOOL_PROFILE_ENV_VAR = "OPENCODE_MCP_TOOL_PROFILE"
VALID_TOOL_PROFILES = ("full", "worker")


def resolve_tool_profile(raw: str | None = None) -> str:
    """Validate the MCP tool profile name.

    Args:
        raw: Raw profile value, or None to read from the environment.

    Returns:
        "full" or "worker".

    Raises:
        RuntimeError: If the value is not a supported profile.
    """
    value = raw if raw is not None else os.environ.get(TOOL_PROFILE_ENV_VAR, "full")
    normalized = (value or "").strip().lower()
    if normalized not in VALID_TOOL_PROFILES:
        raise RuntimeError(
            f"Invalid {TOOL_PROFILE_ENV_VAR}: {value!r}. Expected one of: full, worker"
        )
    return normalized


def _required(name: str) -> str:
    """Return a required env var or raise without leaking its value.

    Args:
        name: Environment variable name.

    Returns:
        The variable value.

    Raises:
        RuntimeError: If the variable is missing or empty.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_settings(dotenv_path: Path | None = None) -> Settings:
    """Load settings from environment, optionally reading a .env file first.

    Args:
        dotenv_path: Explicit path to a .env file. Defaults to ./.env when present.

    Returns:
        Populated Settings.

    Raises:
        RuntimeError: If a required variable is missing or a numeric value is invalid.
    """
    _load_dotenv(dotenv_path)
    try:
        mcp_port = int(os.environ.get("MCP_PORT", "8087"))
        exec_timeout = int(os.environ.get("EXEC_TIMEOUT_S", "120"))
        exec_max_chars = int(os.environ.get("EXEC_MAX_OUTPUT_CHARS", "20000"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid numeric setting: {exc}") from exc
    return Settings(
        opencode_base_url=os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/"),
        opencode_username=os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
        opencode_password=_required("OPENCODE_SERVER_PASSWORD"),
        mcp_bearer_token=_required("MCP_BEARER_TOKEN"),
        mcp_host=os.environ.get("MCP_HOST", "127.0.0.1"),
        mcp_port=mcp_port,
        default_directory=os.environ.get("DEFAULT_DIRECTORY", os.path.expanduser("~")),
        default_provider_id=os.environ.get("DEFAULT_PROVIDER_ID", "opencode"),
        default_model_id=os.environ.get("DEFAULT_MODEL_ID", "muse-spark-1.3-contributor-free"),
        exec_timeout_s=exec_timeout,
        exec_max_output_chars=exec_max_chars,
        tool_profile=resolve_tool_profile(),
    )
