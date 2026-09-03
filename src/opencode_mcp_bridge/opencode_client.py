"""Async client for the opencode serve/web REST API.

Uses HTTP Basic auth (OPENCODE_SERVER_USERNAME/PASSWORD). Never exposes
provider API keys: only /provider is used for model listing, never
/config/providers (which contains secrets).

Opencode endpoint reference: https://opencode.ai/docs/server/
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx


class OpencodeError(RuntimeError):
    """Opencode API failure with HTTP status and a short body snippet."""

    def __init__(self, method: str, path: str, status: int, snippet: str) -> None:
        super().__init__(f"opencode {method} {path} failed: HTTP {status}: {snippet}")
        self.method = method
        self.path = path
        self.status = status
        self.snippet = snippet


def extract_text(parts: list[dict[str, Any]], max_chars: int = 20000) -> str:
    """Extract readable text from opencode message parts.

    Args:
        parts: Raw part dicts from the opencode API.
        max_chars: Truncation cap for the joined text.

    Returns:
        Joined text content, truncated with a marker when over the cap.
    """
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    text = "\n".join(chunks).strip()
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    return text


def simplify_message(item: dict[str, Any]) -> dict[str, Any]:
    """Reduce a {info, parts} message to role/text/time fields.

    Args:
        item: Raw message object with info and parts keys.

    Returns:
        Dict with id, role, text, and time fields.
    """
    info = item.get("info", {}) if isinstance(item, dict) else {}
    parts = item.get("parts", []) if isinstance(item, dict) else []
    return {
        "id": info.get("id"),
        "role": info.get("role"),
        "text": extract_text(parts) if isinstance(parts, list) else "",
        "time": info.get("time", {}),
    }


class OpencodeClient:
    """Thin async wrapper around the opencode REST API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        default_directory: str | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        """Create a client.

        Args:
            base_url: Opencode server URL, e.g. http://127.0.0.1:4096.
            username: Basic auth username.
            password: Basic auth password.
            default_directory: Directory used when callers omit it.
                Defaults to the runtime user's home directory.
            timeout_s: HTTP timeout; prompts can take minutes.
        """
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(username, password),
            timeout=httpx.Timeout(timeout_s),
        )
        self.default_directory = default_directory or os.path.expanduser("~")

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    def _dir(self, directory: str | None) -> str:
        """Resolve the effective opencode working directory."""
        return directory or self.default_directory

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Send one request and return decoded JSON.

        Args:
            method: HTTP method.
            path: API path starting with /.
            params: Query params.
            body: JSON body.

        Returns:
            Decoded JSON payload.

        Raises:
            OpencodeError: On non-2xx responses.
        """
        response = await self._client.request(method, path, params=params, json=body)
        if response.status_code >= 400:
            raise OpencodeError(method, path, response.status_code, response.text[:500])
        if response.status_code == 204:
            return True
        return response.json()

    async def health(self) -> dict[str, Any]:
        """Get server health and version.

        Returns:
            Dict like {healthy: True, version: str}.
        """
        return await self._request("GET", "/global/health")

    async def list_providers(self) -> dict[str, Any]:
        """List providers with model IDs and connected status, no secrets.

        Returns:
            Dict with providers [{providerID, name, modelIDs, connected}]
            and default model mapping.
        """
        data = await self._request("GET", "/provider")
        connected = set(data.get("connected", []) or [])
        providers = []
        for provider in data.get("all", []) or []:
            models = provider.get("models", {}) or {}
            providers.append(
                {
                    "providerID": provider.get("id"),
                    "name": provider.get("name"),
                    "modelIDs": sorted(models.keys()),
                    "connected": provider.get("id") in connected,
                }
            )
        providers.sort(key=lambda item: (not item["connected"], item["providerID"] or ""))
        return {"providers": providers, "default": data.get("default", {})}

    async def list_agents(self, directory: str | None = None) -> list[dict[str, Any]]:
        """List available agents.

        Args:
            directory: Opencode working directory.

        Returns:
            Agent list with name/mode/description fields when present.
        """
        data = await self._request("GET", "/agent", params={"directory": self._dir(directory)})
        agents = data if isinstance(data, list) else []
        return [
            {
                "name": agent.get("name"),
                "mode": agent.get("mode"),
                "description": (agent.get("description") or "")[:300],
            }
            for agent in agents
            if isinstance(agent, dict)
        ]

    async def create_session(
        self,
        title: str | None = None,
        directory: str | None = None,
        agent: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new opencode session.

        Args:
            title: Human-readable session title.
            directory: Working directory for the session (full access allowed).
            agent: Agent name, e.g. plan or build.
            provider_id: Provider for the session default model.
            model_id: Model for the session default model.

        Returns:
            The created session object.

        Raises:
            OpencodeError: If the API call fails.
            ValueError: If only one of provider_id/model_id is given.
        """
        if bool(provider_id) != bool(model_id):
            raise ValueError("provider_id and model_id must be given together or omitted")
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if agent:
            body["agent"] = agent
        if provider_id and model_id:
            body["model"] = {"id": model_id, "providerID": provider_id}
        return await self._request(
            "POST", "/session", params={"directory": self._dir(directory)}, body=body
        )

    async def send_message(
        self,
        session_id: str,
        prompt: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        agent: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """Send a prompt and wait for the assistant reply.

        Args:
            session_id: Session ID (ses_...).
            prompt: User message text.
            provider_id: Optional model override provider.
            model_id: Optional model override model.
            agent: Optional agent override.
            directory: Opencode working directory.

        Returns:
            Dict with sessionID, messageID, text, and raw model info.

        Raises:
            OpencodeError: If the API call fails.
            ValueError: If only one of provider_id/model_id is given.
        """
        if bool(provider_id) != bool(model_id):
            raise ValueError("provider_id and model_id must be given together or omitted")
        body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        if provider_id and model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        if agent:
            body["agent"] = agent
        path = f"/session/{quote(session_id, safe='')}/message"
        data = await self._request(
            "POST", path, params={"directory": self._dir(directory)}, body=body
        )
        info = data.get("info", {}) if isinstance(data, dict) else {}
        parts = data.get("parts", []) if isinstance(data, dict) else []
        return {
            "sessionID": session_id,
            "messageID": info.get("id"),
            "text": extract_text(parts if isinstance(parts, list) else []),
            "model": info.get("model"),
        }

    async def list_sessions(
        self, directory: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """List recent sessions.

        Args:
            directory: Filter directory.
            limit: Max sessions to return.

        Returns:
            Simplified session dicts.
        """
        data = await self._request(
            "GET",
            "/session",
            params={"directory": self._dir(directory), "limit": limit},
        )
        sessions = data if isinstance(data, list) else []
        return [self._simplify_session(s) for s in sessions if isinstance(s, dict)][:limit]

    async def get_session(self, session_id: str, directory: str | None = None) -> dict[str, Any]:
        """Get one session by ID.

        Args:
            session_id: Session ID.
            directory: Opencode working directory.

        Returns:
            Simplified session dict.
        """
        path = f"/session/{quote(session_id, safe='')}"
        data = await self._request("GET", path, params={"directory": self._dir(directory)})
        return self._simplify_session(data if isinstance(data, dict) else {})

    async def list_messages(
        self, session_id: str, directory: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List messages in a session.

        Args:
            session_id: Session ID.
            directory: Opencode working directory.
            limit: Max messages (most recent).

        Returns:
            Simplified {id, role, text, time} dicts.
        """
        path = f"/session/{quote(session_id, safe='')}/message"
        data = await self._request(
            "GET", path, params={"directory": self._dir(directory), "limit": limit}
        )
        items = data if isinstance(data, list) else []
        simplified = [simplify_message(m) for m in items if isinstance(m, dict)]
        return simplified[-limit:]

    async def abort_session(self, session_id: str, directory: str | None = None) -> bool:
        """Abort a running session.

        Args:
            session_id: Session ID.
            directory: Opencode working directory.

        Returns:
            True on success.
        """
        path = f"/session/{quote(session_id, safe='')}/abort"
        await self._request("POST", path, params={"directory": self._dir(directory)})
        return True

    async def delete_session(self, session_id: str, directory: str | None = None) -> bool:
        """Delete a session and all its data.

        Args:
            session_id: Session ID.
            directory: Opencode working directory.

        Returns:
            True on success.
        """
        path = f"/session/{quote(session_id, safe='')}"
        await self._request("DELETE", path, params={"directory": self._dir(directory)})
        return True

    async def get_diff(
        self,
        session_id: str,
        message_id: str | None = None,
        directory: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get file diffs produced by a session.

        Args:
            session_id: Session ID.
            message_id: Optional message to scope the diff.
            directory: Opencode working directory.

        Returns:
            Raw file diff list from opencode.
        """
        path = f"/session/{quote(session_id, safe='')}/diff"
        params: dict[str, Any] = {"directory": self._dir(directory)}
        if message_id:
            params["messageID"] = message_id
        data = await self._request("GET", path, params=params)
        return data if isinstance(data, list) else []

    @staticmethod
    def _simplify_session(session: dict[str, Any]) -> dict[str, Any]:
        """Reduce a session object to the fields MCP clients need.

        Args:
            session: Raw session dict.

        Returns:
            Dict with id, title, directory, agent, model, time, cost.
        """
        return {
            "id": session.get("id"),
            "title": session.get("title"),
            "directory": session.get("directory"),
            "agent": session.get("agent"),
            "model": session.get("model"),
            "time": session.get("time", {}),
            "cost": session.get("cost"),
        }
