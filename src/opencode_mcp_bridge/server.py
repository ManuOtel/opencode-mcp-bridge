"""FastMCP server bridging MCP clients to local opencode.

Transport: Streamable HTTP at POST /mcp (stateless, works with ChatGPT,
Claude Code, Codex, and other MCP-compatible harnesses).
Auth: static Bearer token on every /mcp request; Basic auth to opencode.
Health: GET /health is open (reverse-proxy healthchecks).

Run:
    python -m opencode_mcp_bridge.server
"""

from __future__ import annotations

import asyncio
import hmac
from contextlib import suppress
from typing import Any

import uvicorn
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from opencode_mcp_bridge.config import Settings, load_settings
from opencode_mcp_bridge.opencode_client import OpencodeClient

mcp = FastMCP(
    "opencode-bridge",
    instructions=(
        "Bridge to a self-hosted opencode instance. "
        "Call list_providers first to see models, then create_session, "
        "then send_message with the agent/providerID/modelID choices. "
        "Sessions accept any directory (full server access). "
        "exec_run runs raw shell commands on the server."
    ),
)

_settings: Settings | None = None
_client: OpencodeClient | None = None


def get_settings() -> Settings:
    """Load and cache settings.

    Returns:
        Cached Settings.

    Raises:
        RuntimeError: If required env vars are missing.
    """
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def get_client() -> OpencodeClient:
    """Create and cache the opencode client.

    Returns:
        Shared OpencodeClient instance.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = OpencodeClient(
            base_url=settings.opencode_base_url,
            username=settings.opencode_username,
            password=settings.opencode_password,
            default_directory=settings.default_directory,
            default_provider_id=settings.default_provider_id,
            default_model_id=settings.default_model_id,
        )
    return _client


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    """Open health endpoint for reverse-proxy checks.

    Args:
        request: Starlette request (unused).

    Returns:
        JSON with bridge status and opencode reachability.
    """
    try:
        info = await get_client().health()
        return JSONResponse({"ok": True, "opencode": info})
    except Exception as exc:  # noqa: BLE001 - health must return 503, never raise
        return JSONResponse({"ok": False, "error": str(exc)[:300]}, status_code=503)


@mcp.tool
async def list_providers() -> dict[str, Any]:
    """List opencode providers and models. Call this first for the model picker.

    Returns:
        Dict with providers [{providerID, name, modelIDs, connected}] and default map.
    """
    return await get_client().list_providers()


@mcp.tool
async def list_agents(directory: str | None = None) -> list[dict[str, Any]]:
    """List available opencode agents (e.g. plan, build).

    Args:
        directory: Working directory. Defaults to the server default.

    Returns:
        Agent list with name/mode/description.
    """
    return await get_client().list_agents(directory)


@mcp.tool
async def create_session(
    title: str | None = None,
    directory: str | None = None,
) -> dict[str, Any]:
    """Create a new opencode session.

    Args:
        title: Session title.
        directory: Working directory (any path allowed).

    Returns:
        Created session returned by opencode. Select agent, provider, and model on send_message.
    """
    session = await get_client().create_session(title, directory)
    return OpencodeClient._simplify_session(session)


@mcp.tool
async def send_message(
    sessionID: str,
    message: str | None = None,
    providerID: str | None = None,
    modelID: str | None = None,
    agent: str | None = None,
    directory: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Send a prompt to a session and wait for the assistant reply.

    Args:
        sessionID: Session ID from create_session.
        message: The message text for the agent.
        prompt: Backward-compatible alias for message. Supply exactly one.
        providerID: Optional model override provider.
        modelID: Optional model override model.
        agent: Optional agent override.
        directory: Working directory override.

    Returns:
        Dict with sessionID, messageID, text, and model info.
    """
    if (message is None) == (prompt is None):
        raise ValueError("Exactly one of message or prompt must be supplied")
    return await get_client().send_message(
        sessionID, message if message is not None else prompt, providerID, modelID, agent, directory
    )


@mcp.tool
async def list_sessions(directory: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """List recent opencode sessions.

    Args:
        directory: Filter directory.
        limit: Max sessions (1-100).

    Returns:
        Simplified session dicts.
    """
    return await get_client().list_sessions(directory, max(1, min(limit, 100)))


@mcp.tool
async def get_session(sessionID: str, directory: str | None = None) -> dict[str, Any]:
    """Get one session by ID.

    Args:
        sessionID: Session ID.
        directory: Working directory override.

    Returns:
        Simplified session dict.
    """
    return await get_client().get_session(sessionID, directory)


@mcp.tool
async def list_messages(
    sessionID: str, directory: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List messages in a session (user prompts and assistant replies).

    Args:
        sessionID: Session ID.
        directory: Working directory override.
        limit: Max messages (1-200).

    Returns:
        List of {id, role, text, time} dicts.
    """
    return await get_client().list_messages(sessionID, directory, max(1, min(limit, 200)))


@mcp.tool
async def abort_session(sessionID: str, directory: str | None = None) -> dict[str, Any]:
    """Abort a running session.

    Args:
        sessionID: Session ID.
        directory: Working directory override.

    Returns:
        Dict with sessionID and aborted=True.
    """
    await get_client().abort_session(sessionID, directory)
    return {"sessionID": sessionID, "aborted": True}


@mcp.tool
async def delete_session(sessionID: str, directory: str | None = None) -> dict[str, Any]:
    """Delete a session and all its data. Use to clean up test sessions.

    Args:
        sessionID: Session ID.
        directory: Working directory override.

    Returns:
        Dict with sessionID and deleted=True.
    """
    await get_client().delete_session(sessionID, directory)
    return {"sessionID": sessionID, "deleted": True}


@mcp.tool
async def get_diff(
    sessionID: str, messageID: str | None = None, directory: str | None = None
) -> list[dict[str, Any]]:
    """Get file diffs produced by a session.

    Args:
        sessionID: Session ID.
        messageID: Optional message to scope the diff.
        directory: Working directory override.

    Returns:
        File diff list from opencode.
    """
    return await get_client().get_diff(sessionID, messageID, directory)


WORKER_OUTPUT_DEFAULT_CHARS = 12000
WORKER_OUTPUT_MAX_CHARS = 50000
WORKER_CATALOG_DEFAULT_LIMIT = 20
WORKER_CATALOG_MAX_LIMIT = 100


def _map_worker_state(status: Any) -> str:
    """Map a raw opencode session status to a stable worker state.

    Busy means the worker is active; retry means a retry is scheduled so
    the worker is still active. Error-like types map to error, anything
    missing or unrecognized maps to unknown.

    Args:
        status: Raw status dict (e.g. {type: busy}) or type string.

    Returns:
        One of running, idle, error, unknown.
    """
    raw_type = status.get("type") if isinstance(status, dict) else status
    if not isinstance(raw_type, str):
        return "unknown"
    normalized = raw_type.strip().lower()
    if normalized == "busy":
        return "running"
    if normalized == "idle":
        return "idle"
    if normalized == "retry":
        return "running"
    if "error" in normalized or "fail" in normalized:
        return "error"
    return "unknown"


def _is_free_model(model_id: Any, name: Any, cost: Any) -> bool:
    """Check whether a model counts as free.

    Args:
        model_id: Model ID string.
        name: Human-readable model name.
        cost: Cost metadata dict with input/output numbers when present.

    Returns:
        True when the ID/name contains "free" or when cost metadata
        exists with both input and output at zero.
    """
    if "free" in f"{model_id or ''} {name or ''}".lower():
        return True
    if isinstance(cost, dict) and cost.get("input") is not None and cost.get("output") is not None:
        try:
            return float(cost["input"]) == 0 and float(cost["output"]) == 0
        except (TypeError, ValueError):
            return False
    return False


@mcp.tool
async def worker_run(
    message: str,
    directory: str | None = None,
    title: str | None = None,
    agent: str | None = None,
    providerID: str | None = None,
    modelID: str | None = None,
) -> dict[str, Any]:
    """Start a background worker: create a session and prompt it without waiting.

    Model overrides are validated before anything is created, so invalid
    input has no side effects. If the async prompt fails, the new session
    is deleted on a best-effort basis and the original error is re-raised.

    Pass the returned directory to worker_status when it differs from the
    configured default: status and messages are directory-scoped.

    Args:
        message: Task prompt for the worker.
        directory: Working directory (any path allowed).
        title: Session title.
        agent: Optional agent override.
        providerID: Optional model override provider.
        modelID: Optional model override model.

    Returns:
        Compact dict with taskID (= sessionID), sessionID, state,
        providerID, modelID, directory, and title.
    """
    client = get_client()
    resolved_provider, resolved_model = client.resolve_model(providerID, modelID)
    session = await client.create_session(title, directory)
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        raise ValueError("opencode session response contained no id")
    try:
        await client.prompt_async(session_id, message, providerID, modelID, agent, directory)
    except Exception:
        with suppress(Exception):
            await client.delete_session(session_id, directory)
        raise
    effective_dir = session.get("directory") if isinstance(session, dict) else None
    effective_title = session.get("title") if isinstance(session, dict) else None
    if directory is None:
        resolved_dir = effective_dir or client.default_directory
    else:
        resolved_dir = effective_dir or directory
    return {
        "taskID": session_id,
        "sessionID": session_id,
        "state": "running",
        "providerID": resolved_provider,
        "modelID": resolved_model,
        "directory": resolved_dir,
        "title": effective_title if effective_title is not None else title,
        "agent": agent,
    }


@mcp.tool
async def worker_status(
    taskID: str,
    directory: str | None = None,
    include_output: bool = True,
    max_output_chars: int = WORKER_OUTPUT_DEFAULT_CHARS,
) -> dict[str, Any]:
    """Poll a background worker for state and its latest assistant text.

    Pass the directory returned by worker_run when it differs from the
    configured default: status and messages are directory-scoped.

    Args:
        taskID: Task ID from worker_run (the session ID).
        directory: Working directory override.
        include_output: When false, skip fetching messages.
        max_output_chars: Output cap, clamped to a bounded range.

    Returns:
        Compact dict with taskID, sessionID, state
        (running/idle/error/unknown), raw status, latest output only,
        output_chars, total_chars, truncated_chars, and a truncated flag.
        Never dumps full history.
    """
    client = get_client()
    cap = max(1, min(max_output_chars, WORKER_OUTPUT_MAX_CHARS))
    statuses = await client.get_session_status(directory)
    raw = statuses.get(taskID) if isinstance(statuses, dict) else None
    status = raw.get("type") if isinstance(raw, dict) else raw
    state = _map_worker_state(raw)
    output: str | None = None
    output_chars = 0
    total_chars = 0
    truncated_chars = 0
    truncated = False
    if include_output:
        latest = await client.get_latest_assistant(taskID, directory, max_chars=cap + 1)
        if latest.get("has_error"):
            state = "error"
        total_chars = int(latest.get("total_chars", 0) or 0)
        text = latest.get("text", "") or ""
        if total_chars > cap:
            output = text[:cap]
            truncated = True
            truncated_chars = total_chars - cap
        else:
            output = text
        output_chars = len(output) if output is not None else 0
    return {
        "taskID": taskID,
        "sessionID": taskID,
        "state": state,
        "status": status,
        "output": output,
        "output_chars": output_chars,
        "total_chars": total_chars,
        "truncated_chars": truncated_chars,
        "truncated": truncated,
    }


@mcp.tool
async def worker_catalog(
    query: str | None = None,
    free_only: bool = True,
    connected_only: bool = True,
    limit: int = WORKER_CATALOG_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List worker models with free/connected filters.

    Args:
        query: Case-insensitive substring filter over provider and model
            IDs/names. Omit for no text filtering.
        free_only: Keep only free models.
        connected_only: Keep only connected providers.
        limit: Max entries (1-100).

    Returns:
        Compact dict with model entries, bridge defaults, and total count.
    """
    client = get_client()
    count = max(1, min(limit, WORKER_CATALOG_MAX_LIMIT))
    data = await client.get_providers_raw()
    connected = set(data.get("connected", []) or [])
    needle = query.lower() if query else None
    models: list[dict[str, Any]] = []
    for provider in data.get("all", []) or []:
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("id")
        provider_name = provider.get("name")
        is_connected = provider_id in connected
        if connected_only and not is_connected:
            continue
        entries = provider.get("models", {}) or {}
        if not isinstance(entries, dict):
            continue
        for model_key, spec in entries.items():
            detail = spec if isinstance(spec, dict) else {}
            model_id = detail.get("id") or model_key
            name = detail.get("name")
            cost = detail.get("cost")
            free = _is_free_model(model_id, name, cost)
            if free_only and not free:
                continue
            if (
                needle
                and needle
                not in f"{provider_id or ''} {provider_name or ''} "
                f"{model_id or ''} {name or ''}".lower()
            ):
                continue
            entry: dict[str, Any] = {
                "providerID": provider_id,
                "modelID": model_id,
                "name": name,
                "connected": is_connected,
                "free": free,
            }
            if (
                isinstance(cost, dict)
                and cost.get("input") is not None
                and cost.get("output") is not None
            ):
                entry["cost"] = {"input": cost["input"], "output": cost["output"]}
            models.append(entry)
    models.sort(
        key=lambda item: (
            not item["connected"],
            item["providerID"] or "",
            item["modelID"] or "",
        )
    )
    return {
        "models": models[:count],
        "default": {
            "providerID": client.default_provider_id,
            "modelID": client.default_model_id,
        },
        "total": len(models),
    }


@mcp.tool
async def exec_run(
    command: str, workdir: str | None = None, timeout_s: int | None = None
) -> dict[str, Any]:
    """Run a raw shell command on the server. Full access, no sandbox.

    Prefer opencode sessions for code changes (they track diffs). Use this
    for system ops: docker, systemctl, logs, networking, disk.

    Args:
        command: Shell command to run.
        workdir: Working directory. Defaults to the server default.
        timeout_s: Timeout in seconds. Defaults to server setting.

    Returns:
        Dict with exit_code, stdout, stderr (truncated), and workdir.
    """
    settings = get_settings()
    cwd = workdir or settings.default_directory
    timeout = timeout_s or settings.exec_timeout_s
    timeout = max(1, min(timeout, 600))
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": f"timed out after {timeout}s (process killed)",
                "workdir": cwd,
            }
        cap = settings.exec_max_output_chars
        return {
            "exit_code": process.returncode,
            "stdout": _truncate(stdout.decode(errors="replace"), cap),
            "stderr": _truncate(stderr.decode(errors="replace"), cap),
            "workdir": cwd,
        }
    except FileNotFoundError:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"workdir not found: {cwd}",
            "workdir": cwd,
        }
    except NotADirectoryError:
        return {"exit_code": 127, "stdout": "", "stderr": f"not a directory: {cwd}", "workdir": cwd}


def _truncate(text: str, cap: int) -> str:
    """Truncate text with a marker.

    Args:
        text: Text to cap.
        cap: Max chars.

    Returns:
        Capped text.
    """
    if len(text) > cap:
        return text[:cap] + f"\n...[truncated {len(text) - cap} chars]"
    return text


class BearerAuthMiddleware:
    """ASGI middleware requiring a static Bearer token, except /health."""

    def __init__(self, app: Any, token: str) -> None:
        """Create the middleware.

        Args:
            app: Downstream ASGI app.
            token: Expected Bearer token.
        """
        self.app = app
        self._expected = token.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Check auth for HTTP requests, pass through lifespan/websocket.

        Args:
            scope: ASGI scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
        """
        if scope.get("type") != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"")
        scheme, _, presented = auth.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(presented, self._expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app() -> Any:
    """Build the Starlette app: MCP at /mcp plus Bearer auth.

    Returns:
        ASGI app ready for uvicorn.
    """
    settings = get_settings()
    app = mcp.http_app(path="/mcp", stateless_http=True)
    return BearerAuthMiddleware(app, settings.mcp_bearer_token)


def main() -> None:
    """Run the bridge with uvicorn."""
    settings = get_settings()
    uvicorn.run(create_app(), host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
