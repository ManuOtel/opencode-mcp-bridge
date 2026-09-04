"""Environment-based configuration for the bridge.

Reads settings from the environment (or a .env file when present).
Required secrets raise RuntimeError with no secret values in the message.

Usage:
    from opencode_mcp_bridge.config import load_settings
    settings = load_settings()
"""

from __future__ import annotations

import hmac
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


def _as_bool(value: str | None, default: bool = False) -> bool:
    """Parse an opt-in boolean env var.

    Args:
        value: Raw env value.
        default: Returned when value is None or empty.

    Returns:
        True for 1/true/yes/on (case-insensitive), False otherwise.
    """
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Bridge settings. All secrets come from the environment."""

    opencode_base_url: str
    opencode_username: str
    opencode_password: str
    mcp_bearer_token: str
    mcp_bearer_token_secondary: str | None
    mcp_host: str
    mcp_port: int
    default_directory: str
    allowed_directories: tuple[str, ...]
    default_provider_id: str
    default_model_id: str
    exec_timeout_s: int
    exec_max_output_chars: int
    task_state_path: str
    enable_exec_run: bool


def _normalize_dir(raw: str | None, fallback: str) -> str:
    """Normalize a directory to an absolute expanded path.

    Args:
        raw: Raw env value (may be None or blank).
        fallback: Used when raw is None or blank.

    Returns:
        Absolute path with ~ expanded (symlinks not resolved here;
        authorization resolves canonically per request).
    """
    candidate = (raw or "").strip() or fallback
    expanded = os.path.expanduser(candidate.strip())
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(os.path.join(os.getcwd(), expanded))
    else:
        expanded = os.path.abspath(expanded)
    return expanded


def _realpath_str(path: str) -> str:
    """Return the canonical real path for authorization comparisons.

    Uses os.path.realpath so symlinks, dot segments, and traversal
    resolve before any allowlist check.

    Args:
        path: Directory path (absolute or relative, ~ allowed).

    Returns:
        Canonical absolute path string.
    """
    expanded = os.path.expanduser(path.strip())
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    return os.path.realpath(expanded)


def _is_within_root(candidate_real: str, root_real: str) -> bool:
    """Check whether a canonical path equals or sits below a root.

    String prefix checks are not used, so sibling-prefix tricks
    (/root-evil vs /root) do not pass.

    Args:
        candidate_real: Canonical candidate path.
        root_real: Canonical root path.

    Returns:
        True when equal or below the root.
    """
    try:
        return Path(candidate_real).is_relative_to(Path(root_real))
    except (ValueError, OSError):
        return False


def is_path_allowed(candidate: str, allowed_roots: tuple[str, ...] | list[str]) -> bool:
    """Check a candidate directory against allowed roots canonically.

    Args:
        candidate: Requested directory path.
        allowed_roots: Configured root directories.

    Returns:
        True when the canonical candidate equals or sits below any root.
    """
    candidate_real = _realpath_str(candidate)
    for root in allowed_roots:
        if _is_within_root(candidate_real, _realpath_str(root)):
            return True
    return False


def _parse_allowed_directories(raw: str | None, normalized_default: str) -> tuple[str, ...]:
    """Parse ALLOWED_DIRECTORIES, defaulting to the default directory.

    Args:
        raw: Raw env value, or None when the variable is unset.
        normalized_default: Normalized default directory.

    Returns:
        Tuple of allowed root directories (absolute, expanded).

    Raises:
        RuntimeError: If explicitly configured roots are empty, or the
            normalized default is not within them (fail closed).
    """
    if raw is None:
        return (normalized_default,)
    if not raw.strip():
        raise RuntimeError(
            "Invalid ALLOWED_DIRECTORIES: must list at least one directory, "
            "or unset it to default to DEFAULT_DIRECTORY"
        )
    roots: list[str] = []
    for entry in raw.split(","):
        cleaned = entry.strip()
        if not cleaned:
            continue
        roots.append(_normalize_dir(cleaned, normalized_default))
    if not roots:
        raise RuntimeError(
            "Invalid ALLOWED_DIRECTORIES: must list at least one directory, "
            "or unset it to default to DEFAULT_DIRECTORY"
        )
    default_real = _realpath_str(normalized_default)
    allowed = False
    for root in roots:
        if _is_within_root(default_real, _realpath_str(root)):
            allowed = True
            break
    if not allowed:
        raise RuntimeError(
            "Invalid configuration: DEFAULT_DIRECTORY is not within ALLOWED_DIRECTORIES"
        )
    return tuple(roots)


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


def _optional_secondary_token(name: str, primary: str) -> str | None:
    """Return an optional rotation token or raise fail-closed on misuse.

    Unset means single-token mode (fully backward compatible). When set,
    the value must be non-blank and must differ from the primary token;
    an identical or blank value is a misconfiguration, never silently
    accepted or ignored.

    Args:
        name: Environment variable name for the secondary token.
        primary: Already-validated primary token value.

    Returns:
        Stripped secondary token, or None when the variable is unset.

    Raises:
        RuntimeError: If the variable is set-but-blank or duplicates
            the primary. Messages carry names only, never token values.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        raise RuntimeError(f"Invalid {name}: set but empty; unset it or set a token")
    if hmac.compare_digest(cleaned.encode(), primary.encode()):
        raise RuntimeError(f"Invalid {name}: must differ from MCP_BEARER_TOKEN")
    return cleaned


def accepted_bearer_tokens(settings: Settings) -> tuple[str, ...]:
    """Return every accepted bearer token (primary first, then secondary).

    Args:
        settings: Loaded Settings.

    Returns:
        Tuple of accepted token values (1 or 2 entries).
    """
    if settings.mcp_bearer_token_secondary:
        return (settings.mcp_bearer_token, settings.mcp_bearer_token_secondary)
    return (settings.mcp_bearer_token,)


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
    normalized_default = _normalize_dir(
        os.environ.get("DEFAULT_DIRECTORY"), os.path.expanduser("~")
    )
    allowed = _parse_allowed_directories(os.environ.get("ALLOWED_DIRECTORIES"), normalized_default)
    primary_token = _required("MCP_BEARER_TOKEN")
    secondary_token = _optional_secondary_token("MCP_BEARER_TOKEN_SECONDARY", primary_token)
    return Settings(
        opencode_base_url=os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/"),
        opencode_username=os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
        opencode_password=_required("OPENCODE_SERVER_PASSWORD"),
        mcp_bearer_token=primary_token,
        mcp_bearer_token_secondary=secondary_token,
        mcp_host=os.environ.get("MCP_HOST", "127.0.0.1"),
        mcp_port=mcp_port,
        default_directory=normalized_default,
        allowed_directories=allowed,
        default_provider_id=os.environ.get("DEFAULT_PROVIDER_ID", "opencode"),
        default_model_id=os.environ.get("DEFAULT_MODEL_ID", "muse-spark-1.3-contributor-free"),
        exec_timeout_s=exec_timeout,
        exec_max_output_chars=exec_max_chars,
        task_state_path=os.environ.get(
            "TASK_STATE_PATH", "/var/lib/opencode-mcp-bridge/tasks.json"
        ),
        enable_exec_run=_as_bool(os.environ.get("ENABLE_EXEC_RUN"), default=False),
    )
