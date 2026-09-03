"""FastMCP server bridging ChatGPT to local opencode.

Transport: Streamable HTTP at POST /mcp (stateless, ChatGPT-compatible).
Auth: static Bearer token on every /mcp request; Basic auth to opencode.
Health: GET /health is open (Traefik/Coolify healthchecks).

Run:
    python -m opencode_mcp_bridge.server
"""

from __future__ import annotations

import asyncio
import hmac
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
        "Call list_providers first to see models, then create_session "
        "with providerID/modelID, then send_message. "
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
        )
    return _client


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    """Open health endpoint for Traefik/Coolify checks.

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
    agent: str | None = None,
    providerID: str | None = None,
    modelID: str | None = None,
) -> dict[str, Any]:
    """Create a new opencode session.

    Args:
        title: Session title.
        directory: Working directory (any path allowed).
        agent: Agent name, e.g. plan or build.
        providerID: Provider for the session model. Omit to use opencode default.
        modelID: Model for the session model. Omit to use opencode default.

    Returns:
        Created session with id, title, directory, agent, model.
    """
    session = await get_client().create_session(title, directory, agent, providerID, modelID)
    return OpencodeClient._simplify_session(session)


@mcp.tool
async def send_message(
    sessionID: str,
    prompt: str,
    providerID: str | None = None,
    modelID: str | None = None,
    agent: str | None = None,
    directory: str | None = None,
) -> dict[str, Any]:
    """Send a prompt to a session and wait for the assistant reply.

    Args:
        sessionID: Session ID from create_session.
        prompt: The message text for the agent.
        providerID: Optional model override provider.
        modelID: Optional model override model.
        agent: Optional agent override.
        directory: Working directory override.

    Returns:
        Dict with sessionID, messageID, text, and model info.
    """
    return await get_client().send_message(sessionID, prompt, providerID, modelID, agent, directory)


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
