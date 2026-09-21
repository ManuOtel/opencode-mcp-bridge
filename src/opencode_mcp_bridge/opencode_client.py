"""Async client for the opencode serve/web REST API.

Uses HTTP Basic auth (OPENCODE_SERVER_USERNAME/PASSWORD). Never exposes
provider API keys: only /provider is used for model listing, never
/config/providers (which contains secrets).

Opencode endpoint reference: https://opencode.ai/docs/server/
V2 HTTP API reference: https://opencode.ai/v2/docs/api

Route negotiation: the legacy contract is the default and stays unchanged
when GET /global/health succeeds. Only when legacy health is absent does
the cached capability probe select V2, in which case data-plane calls are
routed through the verified V2 contracts listed in docs/compatibility.md.
Anything without a verified V2 mapping fails closed with OpencodeError.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

LEGACY_HEALTH_PATH = "/global/health"
V2_INFO_PATH = "/api/info"
PROBE_TIMEOUT_S = 5.0


class OpencodeError(RuntimeError):
    """Opencode API failure with HTTP status and a short body snippet."""

    def __init__(self, method: str, path: str, status: int, snippet: str) -> None:
        super().__init__(f"opencode {method} {path} failed: HTTP {status}: {snippet}")
        self.method = method
        self.path = path
        self.status = status
        self.snippet = snippet


def _unwrap_envelope(payload: Any) -> Any:
    """Unwrap a V2 {data: ...} envelope, passing other payloads through."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _legacy_to_v2_path(legacy_path: str) -> str:
    """Map the legacy health path to its V2 equivalent.

    Only /global/health has a pure path-to-path V2 equivalent
    (/api/info). The V2 data plane needs per-method request/response
    adaptation (envelope unwrapping, body reshaping, multi-call model
    selection), so lifecycle mappings live in the OpencodeClient._v2_*
    helpers instead of this path mapper.
    """
    if legacy_path == LEGACY_HEALTH_PATH:
        return V2_INFO_PATH
    raise OpencodeError(
        "GET",
        legacy_path,
        0,
        "V2 lifecycle adapter is not verified yet: no verified V2 path",
    )


def _require_v2_data(payload: Any, method: str, path: str) -> Any:
    """Unwrap a V2 {data: ...} envelope, failing closed when absent.

    Args:
        payload: Decoded JSON response body.
        method: HTTP method used, for error reporting.
        path: V2 API path used, for error reporting.

    Returns:
        The unwrapped data payload.

    Raises:
        OpencodeError: When the payload has no data envelope.
    """
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    raise OpencodeError(method, path, 200, "unsupported V2 envelope payload")


def extract_v2_text(content: Any, max_chars: int = 20000) -> str:
    """Extract readable text from V2 projected assistant content.

    V2 assistant messages carry a content array whose text items look
    like {type: "text", text: "..."}. Reasoning and tool items are
    skipped, matching the legacy parts extractor.

    Args:
        content: Raw content array from a V2 assistant message.
        max_chars: Truncation cap for the joined text.

    Returns:
        Joined text content, truncated with a marker when over the cap.
    """
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    text = "\n".join(chunks).strip()
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    return text


def _simplify_v2_message(item: dict[str, Any]) -> dict[str, Any]:
    """Reduce a V2 projected message to role/text/time fields.

    V2 messages are flat: {id, type, time, ...} where type is the role
    (user, assistant, system, ...). Assistants carry text in content[];
    user/system/synthetic messages carry a text field.

    Args:
        item: Raw V2 message object.

    Returns:
        Dict with id, role, text, and time fields.
    """
    msg_type = item.get("type")
    if msg_type == "assistant":
        text = extract_v2_text(item.get("content"))
    else:
        raw_text = item.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
    time = item.get("time")
    return {
        "id": item.get("id"),
        "role": msg_type,
        "text": text,
        "time": time if isinstance(time, dict) else {},
    }


def _v2_model_cost(model: dict[str, Any]) -> dict[str, Any] | None:
    """Extract {input, output} cost from a V2 Model.Info, if numeric.

    V2 cost is a list of per-tier entries; the first entry with numeric
    input/output wins. Provider settings, headers, and body are never
    read here, so no credentials can leak through this helper.

    Args:
        model: Raw V2 Model.Info dict.

    Returns:
        Cost dict or None when no usable entry exists.
    """
    costs = model.get("cost")
    if not isinstance(costs, list):
        return None
    for entry in costs:
        if not isinstance(entry, dict):
            continue
        price_in = entry.get("input")
        price_out = entry.get("output")
        if isinstance(price_in, (int, float)) and isinstance(price_out, (int, float)):
            return {"input": price_in, "output": price_out}
    return None


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
        default_provider_id: str = "opencode",
        default_model_id: str = "muse-spark-1.3-contributor-free",
        timeout_s: float = 600.0,
    ) -> None:
        """Create a client.

        Args:
            base_url: Opencode server URL, e.g. http://127.0.0.1:4096.
            username: Basic auth username.
            password: Basic auth password.
            default_directory: Directory used when callers omit it.
                Defaults to the runtime user's home directory.
            default_provider_id: Provider used when send_message omits a model.
            default_model_id: Model used when send_message omits a model.
            timeout_s: HTTP timeout; prompts can take minutes.
        """
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(username, password),
            timeout=httpx.Timeout(timeout_s),
        )
        self.default_directory = default_directory or os.path.expanduser("~")
        self.default_provider_id = default_provider_id
        self.default_model_id = default_model_id
        # Runtime route negotiation. Default stays on the proven legacy
        # contract; V2 is a compatibility fallback selected only by
        # probe_capability(). No credentials, prompts, paths, or provider
        # secrets are ever logged here.
        self._api_family: str = "legacy"
        self._server_version: str | None = None
        self._capability: dict[str, Any] | None = None
        self._health_cache: dict[str, Any] | bool | None = None

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    def _dir(self, directory: str | None) -> str:
        """Resolve the effective opencode working directory."""
        return directory or self.default_directory

    def resolve_model(self, provider_id: str | None, model_id: str | None) -> tuple[str, str]:
        """Validate a model override pair and apply configured defaults.

        Args:
            provider_id: Optional model override provider.
            model_id: Optional model override model.

        Returns:
            Tuple of (provider_id, model_id) with defaults resolved.

        Raises:
            ValueError: If only one of provider_id/model_id is given.
        """
        if bool(provider_id) != bool(model_id):
            raise ValueError("provider_id and model_id must be given together or omitted")
        return (
            provider_id or self.default_provider_id,
            model_id or self.default_model_id,
        )

    def _message_body(
        self,
        message: str,
        provider_id: str | None,
        model_id: str | None,
        agent: str | None,
    ) -> tuple[dict[str, Any], str, str]:
        """Build a message/prompt_async body, applying configured model defaults.

        Args:
            message: User message text.
            provider_id: Optional model override provider.
            model_id: Optional model override model.
            agent: Optional agent override.

        Returns:
            Tuple of (body, provider_id, model_id) with defaults resolved.

        Raises:
            ValueError: If only one of provider_id/model_id is given.
        """
        resolved_provider, resolved_model = self.resolve_model(provider_id, model_id)
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": message}],
            "model": {"providerID": resolved_provider, "modelID": resolved_model},
        }
        if agent:
            body["agent"] = agent
        return body, resolved_provider, resolved_model

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

    @property
    def api_family(self) -> str:
        """Selected route family: 'legacy' (preferred) or 'v2' (fallback)."""
        return self._api_family

    @property
    def server_version(self) -> str | None:
        """Server version recorded by the last capability probe, if any."""
        return self._server_version

    @property
    def capability(self) -> dict[str, Any] | None:
        """Cached capability record, or None before the first probe."""
        return self._capability

    async def _probe_once(self, method: str, path: str, timeout_s: float) -> Any:
        """Send one bounded probe request and return decoded JSON."""
        try:
            response = await self._client.request(method, path, timeout=httpx.Timeout(timeout_s))
        except Exception as exc:
            raise OpencodeError(method, path, 0, type(exc).__name__[:100]) from exc
        if response.status_code >= 400:
            raise OpencodeError(method, path, response.status_code, response.text[:200])
        if response.status_code == 204:
            return True
        try:
            return response.json()
        except ValueError as exc:
            raise OpencodeError(method, path, response.status_code, "invalid JSON") from exc

    async def probe_capability(
        self, force_refresh: bool = False, timeout_s: float = PROBE_TIMEOUT_S
    ) -> dict[str, Any]:
        """Detect the server route family once and cache the result.

        Legacy is preferred: when GET /global/health succeeds the client
        stays on the proven /session + prompt_async contract and never
        switches merely because /api/info also exists. Only when legacy
        health is absent does the probe try GET /api/info as a V2
        compatibility fallback. The V2 info response is a direct
        ServerInfo object ({version, pid, urls, paths}): it carries no
        {data: ...} envelope and no healthy field.

        Args:
            force_refresh: Re-probe even when a cached record exists.
            timeout_s: Per-request bound for each of at most two probes.

        Returns:
            Dict with family, version, legacy_available, v2_available.

        Raises:
            OpencodeError: When neither family offers a usable health path.
        """
        if self._capability is not None and not force_refresh:
            return self._capability
        try:
            legacy = await self._probe_once("GET", LEGACY_HEALTH_PATH, timeout_s)
        except OpencodeError:
            legacy = None
        if legacy is not None:
            version = legacy.get("version") if isinstance(legacy, dict) else None
            self._api_family = "legacy"
            self._server_version = version if isinstance(version, str) else None
            self._health_cache = dict(legacy) if isinstance(legacy, dict) else legacy
            self._capability = {
                "family": "legacy",
                "version": self._server_version,
                "legacy_available": True,
                "v2_available": False,
            }
            return self._capability
        try:
            v2_raw = await self._probe_once("GET", V2_INFO_PATH, timeout_s)
        except OpencodeError as v2_error:
            raise OpencodeError(
                "GET",
                LEGACY_HEALTH_PATH,
                v2_error.status,
                "opencode capability probe failed: no usable legacy or V2 health path",
            ) from v2_error
        if not isinstance(v2_raw, dict):
            raise OpencodeError("GET", V2_INFO_PATH, 200, "unsupported V2 capability payload")
        v2_data = v2_raw
        version = v2_data.get("version")
        self._api_family = "v2"
        self._server_version = version if isinstance(version, str) else None
        self._health_cache = dict(v2_data)
        self._capability = {
            "family": "v2",
            "version": self._server_version,
            "legacy_available": False,
            "v2_available": True,
        }
        return self._capability

    async def _ensure_capability(self) -> dict[str, Any]:
        """Probe once on first normal use; later calls reuse the cache."""
        if self._capability is None:
            return await self.probe_capability()
        return self._capability

    async def _v2_provider_snapshot(self, directory: str | None = None) -> dict[str, Any]:
        """Fetch V2 provider/model/default and synthesize the legacy shape.

        V2 splits the legacy /provider payload across three verified
        routes: GET /api/provider (Provider.Info list, no models), GET
        /api/model (Model.Info list with providerID/modelID/cost), and
        GET /api/model/default (default Model.Info or null). Each takes
        the documented deepObject location[directory] query parameter
        (httpx encoding: params {"location[directory]": directory}), so
        the resolved directory is sent instead of dropped. This helper
        merges them into {all, connected, default} so list_providers and
        get_providers_raw keep their existing return shapes.

        Only id/name/modelIDs/cost/activation are read: provider
        settings, headers, and body (which may hold secrets) are never
        extracted. A provider counts as connected unless its activation
        is "disabled".

        Args:
            directory: Opencode working directory; defaults to the
                configured default when omitted.

        Returns:
            Dict with all/connected/default keys, like legacy /provider.

        Raises:
            OpencodeError: On transport failure or unexpected envelopes.
        """
        location_params = {"location[directory]": self._dir(directory)}
        providers_raw = await self._request("GET", "/api/provider", params=location_params)
        models_raw = await self._request("GET", "/api/model", params=location_params)
        default_raw = await self._request("GET", "/api/model/default", params=location_params)
        providers = _require_v2_data(providers_raw, "GET", "/api/provider")
        models = _require_v2_data(models_raw, "GET", "/api/model")
        default_info = _require_v2_data(default_raw, "GET", "/api/model/default")
        if not isinstance(providers, list):
            raise OpencodeError("GET", "/api/provider", 200, "unsupported V2 provider list")
        if not isinstance(models, list):
            raise OpencodeError("GET", "/api/model", 200, "unsupported V2 model list")
        by_provider: dict[str, list[dict[str, Any]]] = {}
        for model in models:
            if not isinstance(model, dict):
                continue
            provider_id = model.get("providerID")
            model_id = model.get("modelID")
            if not isinstance(provider_id, str) or not isinstance(model_id, str):
                continue
            by_provider.setdefault(provider_id, []).append(model)
        providers_out: list[dict[str, Any]] = []
        connected: list[str] = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            if not isinstance(provider_id, str):
                continue
            if provider.get("activation", "enabled") != "disabled":
                connected.append(provider_id)
            entries: dict[str, Any] = {}
            for model in by_provider.get(provider_id, []):
                model_id = model.get("modelID")
                entry: dict[str, Any] = {
                    "id": model_id,
                    "name": model.get("name"),
                }
                cost = _v2_model_cost(model)
                if cost is not None:
                    entry["cost"] = cost
                entries[model_id] = entry
            providers_out.append(
                {"id": provider_id, "name": provider.get("name"), "models": entries}
            )
        default: dict[str, Any] = {}
        if isinstance(default_info, dict):
            default_provider = default_info.get("providerID")
            default_model = default_info.get("modelID")
            if isinstance(default_provider, str) and isinstance(default_model, str):
                default = {"providerID": default_provider, "modelID": default_model}
        return {"all": providers_out, "connected": connected, "default": default}

    @staticmethod
    def _summarize_providers(data: dict[str, Any]) -> dict[str, Any]:
        """Reduce a legacy-shape provider payload to the public summary.

        Args:
            data: Dict with all/connected/default keys.

        Returns:
            Dict with providers [{providerID, name, modelIDs, connected}]
            and default model mapping.
        """
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

    async def _v2_switch_model_agent(
        self,
        session_id: str,
        provider_id: str,
        model_id: str,
        agent: str | None,
    ) -> None:
        """Apply V2 model/agent selection before prompting.

        The V2 prompt body carries only text: model and agent overrides
        must go through POST /api/session/{sessionID}/model
        ({model: {providerID, id}}) and POST
        /api/session/{sessionID}/agent ({agent}) first.

        Args:
            session_id: Session ID (ses_...).
            provider_id: Resolved provider ID (bridge defaults applied).
            model_id: Resolved model ID (bridge defaults applied).
            agent: Optional agent override; skipped when None.

        Raises:
            OpencodeError: If either switch call fails.
        """
        quoted = quote(session_id, safe="")
        await self._request(
            "POST",
            f"/api/session/{quoted}/model",
            body={"model": {"providerID": provider_id, "id": model_id}},
        )
        if agent:
            await self._request("POST", f"/api/session/{quoted}/agent", body={"agent": agent})

    async def health(self, force_refresh: bool = False) -> dict[str, Any]:
        """Get server health and version.

        Auto-probes on first use, then returns the cached verified probe
        payload without duplicate health traffic. Only an explicit
        force_refresh re-probes.

        Args:
            force_refresh: Re-probe even when a cached payload exists.

        Returns:
            Cached probe payload: the legacy health dict
            ({healthy, version}) or the direct V2 ServerInfo dict
            ({version, pid, urls, paths}, no healthy field).
        """
        if force_refresh:
            await self.probe_capability(force_refresh=True)
        else:
            await self._ensure_capability()
        cached = self._health_cache
        if isinstance(cached, dict):
            return dict(cached)
        return cached  # type: ignore[return-value]

    async def list_providers(self, directory: str | None = None) -> dict[str, Any]:
        """List providers with model IDs and connected status, no secrets.

        In V2 mode the snapshot is synthesized from GET /api/provider,
        GET /api/model, and GET /api/model/default, each with the
        documented location[directory] query parameter; provider
        settings, headers, and body are never extracted. Legacy mode
        still calls GET /provider with no query parameters.

        Args:
            directory: Opencode working directory (V2 only; legacy
                ignores it). Defaults to the configured default.

        Returns:
            Dict with providers [{providerID, name, modelIDs, connected}]
            and default model mapping.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            return self._summarize_providers(await self._v2_provider_snapshot(directory))
        data = await self._request("GET", "/provider")
        return self._summarize_providers(data)

    async def list_agents(self, directory: str | None = None) -> list[dict[str, Any]]:
        """List available agents.

        In V2 mode GET /api/agent is used with the documented
        deepObject location[directory] query parameter (httpx encoding:
        params {"location[directory]": directory}) and its
        {data: [...]} envelope is unwrapped.

        Args:
            directory: Opencode working directory; defaults to the
                configured default when omitted.

        Returns:
            Agent list with name/mode/description fields when present.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            raw = await self._request(
                "GET",
                "/api/agent",
                params={"location[directory]": self._dir(directory)},
            )
            data = _require_v2_data(raw, "GET", "/api/agent")
            agents = data if isinstance(data, list) else []
        else:
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
    ) -> dict[str, Any]:
        """Create a new opencode session.

        In V2 mode POST /api/session is used with
        {title, location: {directory}}; the {data: Session.Info}
        envelope is unwrapped.

        Args:
            title: Human-readable session title.
            directory: Working directory for the session (full access allowed).
        Returns:
            The created session object.

        Raises:
            OpencodeError: If the API call fails.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            body: dict[str, Any] = {"location": {"directory": self._dir(directory)}}
            if title:
                body["title"] = title
            raw = await self._request("POST", "/api/session", body=body)
            data = _require_v2_data(raw, "POST", "/api/session")
            if not isinstance(data, dict):
                raise OpencodeError("POST", "/api/session", 200, "unsupported V2 session")
            return data
        body = {}
        if title:
            body["title"] = title
        return await self._request(
            "POST", "/session", params={"directory": self._dir(directory)}, body=body
        )

    async def send_message(
        self,
        session_id: str,
        message: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        agent: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """Send a prompt and wait for the assistant reply.

        Args:
            session_id: Session ID (ses_...).
            message: User message text.
            provider_id: Optional model override provider.
            model_id: Optional model override model.
            agent: Optional agent override.
            directory: Opencode working directory.

        Returns:
            Dict with sessionID, messageID, text, and raw model info.

        Raises:
            OpencodeError: If the API call fails, or in V2 mode where the
                prompt API is async-only and has no verified sync-reply
                mapping (use prompt_async plus message polling instead).
            ValueError: If only one of provider_id/model_id is given.
        """
        body, _, _ = self._message_body(message, provider_id, model_id, agent)
        await self._ensure_capability()
        path = f"/session/{quote(session_id, safe='')}/message"
        if self._api_family == "v2":
            raise OpencodeError(
                "POST",
                path,
                0,
                "V2 has no verified sync-reply mapping: POST "
                "/api/session/{sessionID}/prompt is async-only, so "
                "send_message cannot wait for a reply without guessing; "
                "use prompt_async plus message polling instead",
            )
        data = await self._request(
            "POST", path, params={"directory": self._dir(directory)}, body=body
        )
        info = data.get("info", {}) if isinstance(data, dict) else {}
        parts = data.get("parts", []) if isinstance(data, dict) else []
        error = info.get("error") if isinstance(info, dict) else None
        if error:
            if isinstance(error, dict):
                error_data = error.get("data")
                if isinstance(error_data, dict):
                    snippet = error_data.get("message")
                elif isinstance(error_data, str):
                    snippet = error_data
                else:
                    snippet = error.get("message") or error.get("name")
                snippet = snippet or str(error)
            else:
                snippet = str(error)
            raise OpencodeError("POST", path, 0, str(snippet)[:500])
        text = extract_text(parts if isinstance(parts, list) else [])
        if not text:
            raise OpencodeError("POST", path, 200, "response contained no usable text")
        model = info.get("model") if isinstance(info, dict) else None
        if model is None and isinstance(info, dict):
            provider_id_value = info.get("providerID")
            model_id_value = info.get("modelID")
            if provider_id_value is not None and model_id_value is not None:
                model = {"providerID": provider_id_value, "modelID": model_id_value}
        return {
            "sessionID": session_id,
            "messageID": info.get("id") if isinstance(info, dict) else None,
            "text": text,
            "model": model,
        }

    async def get_session_status(self, directory: str | None = None) -> dict[str, Any]:
        """Get live status for all sessions.

        In V2 mode GET /api/session/active is used. It only reports
        sessions with a foreground drain owned by this process, each as
        {type: "running"}; those entries are normalized to {type: "busy"}
        so the existing worker-state mapping keeps reporting "running".
        Sessions absent from the result have no entry (no idle listing
        exists in the documented V2 contract).

        Args:
            directory: Opencode working directory (legacy only; V2 active
                takes no directory parameter).

        Returns:
            Map of session ID to raw status dict, e.g. {type: idle|busy|retry}.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            raw = await self._request("GET", "/api/session/active")
            data = _require_v2_data(raw, "GET", "/api/session/active")
            if not isinstance(data, dict):
                raise OpencodeError(
                    "GET", "/api/session/active", 200, "unsupported V2 active sessions"
                )
            return {key: {"type": "busy"} for key in data}
        data = await self._request(
            "GET", "/session/status", params={"directory": self._dir(directory)}
        )
        return data if isinstance(data, dict) else {}

    async def get_providers_raw(self, directory: str | None = None) -> dict[str, Any]:
        """Get the raw /provider payload with per-model cost metadata.

        In V2 mode the payload is synthesized from GET /api/provider,
        GET /api/model, and GET /api/model/default (each with the
        documented location[directory] query parameter) into the same
        all/connected/default shape. Legacy mode still calls
        GET /provider with no query parameters.

        Args:
            directory: Opencode working directory (V2 only; legacy
                ignores it). Defaults to the configured default.

        Returns:
            Raw dict with all/connected/default keys. Never exposes secrets:
            /config/providers is never called.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            return await self._v2_provider_snapshot(directory)
        data = await self._request("GET", "/provider")
        return data if isinstance(data, dict) else {}

    async def _v2_latest_assistant(
        self,
        session_id: str,
        limit: int = 20,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Get the latest V2 assistant message text and error flag.

        Reads GET /api/session/{sessionID}/message (documented limit
        parameter only; no directory parameter exists) and scans the
        projected messages newest-first for type "assistant". Text comes
        from the message content[] text items; has_error is set when the
        message carries an error or finished with "error".

        Args:
            session_id: Session ID.
            limit: How many recent messages to scan.
            max_chars: Cap for the returned text. None means no cap.
                total_chars always reflects the full untruncated text.

        Returns:
            Dict with messageID, text, total_chars, and has_error flag.
        """
        quoted = quote(session_id, safe="")
        raw = await self._request(
            "GET", f"/api/session/{quoted}/message", params={"limit": str(limit)}
        )
        data = _require_v2_data(raw, "GET", f"/api/session/{quoted}/message")
        items = data if isinstance(data, list) else []
        for item in reversed(items):
            if not isinstance(item, dict) or item.get("type") != "assistant":
                continue
            full_text = extract_v2_text(item.get("content"))
            if max_chars is not None and len(full_text) > max_chars:
                text = full_text[:max_chars]
            else:
                text = full_text
            has_error = bool(item.get("error")) or item.get("finish") == "error"
            return {
                "messageID": item.get("id"),
                "text": text,
                "total_chars": len(full_text),
                "has_error": has_error,
            }
        return {"messageID": None, "text": "", "total_chars": 0, "has_error": False}

    async def get_latest_assistant(
        self,
        session_id: str,
        directory: str | None = None,
        limit: int = 20,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Get the latest assistant message text and error flag.

        Args:
            session_id: Session ID.
            directory: Opencode working directory.
            limit: How many recent messages to scan.
            max_chars: Cap for the returned text. None means no cap.
                total_chars always reflects the full untruncated text.

        Returns:
            Dict with messageID, text, total_chars, and has_error flag.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            return await self._v2_latest_assistant(session_id, limit, max_chars)
        path = f"/session/{quote(session_id, safe='')}/message"
        data = await self._request(
            "GET", path, params={"directory": self._dir(directory), "limit": limit}
        )
        items = data if isinstance(data, list) else []
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            info = item.get("info", {})
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            parts = item.get("parts", [])
            chunks = [
                part["text"]
                for part in parts
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ]
            full_text = "\n".join(chunks).strip()
            if max_chars is not None and len(full_text) > max_chars:
                text = full_text[:max_chars]
            else:
                text = full_text
            return {
                "messageID": info.get("id"),
                "text": text,
                "total_chars": len(full_text),
                "has_error": bool(info.get("error")),
            }
        return {"messageID": None, "text": "", "total_chars": 0, "has_error": False}

    async def prompt_async(
        self,
        session_id: str,
        message: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        agent: str | None = None,
        directory: str | None = None,
    ) -> bool:
        """Submit a prompt without waiting for the assistant reply.

        Args:
            session_id: Session ID (ses_...).
            message: User message text.
            provider_id: Optional model override provider.
            model_id: Optional model override model.
            agent: Optional agent override.
            directory: Opencode working directory.

        Returns:
            True on 204 acceptance (legacy) or prompt admission (V2).

        Raises:
            OpencodeError: If the API call fails.
            ValueError: If only one of provider_id/model_id is given.
        """
        body, resolved_provider, resolved_model = self._message_body(
            message, provider_id, model_id, agent
        )
        await self._ensure_capability()
        if self._api_family == "v2":
            # The V2 prompt body carries text only: apply the resolved
            # model (bridge defaults included, preserving free-first
            # selection) and the agent override first, then admit input
            # via the verified async prompt route.
            await self._v2_switch_model_agent(session_id, resolved_provider, resolved_model, agent)
            quoted = quote(session_id, safe="")
            await self._request("POST", f"/api/session/{quoted}/prompt", body={"text": message})
            return True
        path = f"/session/{quote(session_id, safe='')}/prompt_async"
        await self._request("POST", path, params={"directory": self._dir(directory)}, body=body)
        return True

    async def list_sessions(
        self, directory: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """List recent sessions.

        In V2 mode GET /api/session is used with the documented
        directory/limit parameters and its {data: [...]} envelope is
        unwrapped (pagination cursors are not followed).

        Args:
            directory: Filter directory.
            limit: Max sessions to return.

        Returns:
            Simplified session dicts.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            raw = await self._request(
                "GET",
                "/api/session",
                params={"directory": self._dir(directory), "limit": str(limit)},
            )
            data = _require_v2_data(raw, "GET", "/api/session")
            sessions = data if isinstance(data, list) else []
            return [self._simplify_session(s) for s in sessions if isinstance(s, dict)][:limit]
        data = await self._request(
            "GET",
            "/session",
            params={"directory": self._dir(directory), "limit": limit},
        )
        sessions = data if isinstance(data, list) else []
        return [self._simplify_session(s) for s in sessions if isinstance(s, dict)][:limit]

    async def get_session(self, session_id: str, directory: str | None = None) -> dict[str, Any]:
        """Get one session by ID.

        In V2 mode GET /api/session/{sessionID} is used (it takes no
        directory parameter) and its {data: Session.Info} envelope is
        unwrapped.

        Args:
            session_id: Session ID.
            directory: Opencode working directory (legacy only).

        Returns:
            Simplified session dict.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            quoted = quote(session_id, safe="")
            raw = await self._request("GET", f"/api/session/{quoted}")
            data = _require_v2_data(raw, "GET", f"/api/session/{quoted}")
            return self._simplify_session(data if isinstance(data, dict) else {})
        path = f"/session/{quote(session_id, safe='')}"
        data = await self._request("GET", path, params={"directory": self._dir(directory)})
        return self._simplify_session(data if isinstance(data, dict) else {})

    async def list_messages(
        self, session_id: str, directory: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List messages in a session.

        In V2 mode GET /api/session/{sessionID}/message is used with the
        documented limit parameter only (no directory parameter exists)
        and its {data: [...]} envelope of projected messages is mapped to
        the existing {id, role, text, time} shape.

        Args:
            session_id: Session ID.
            directory: Opencode working directory (legacy only).
            limit: Max messages (most recent).

        Returns:
            Simplified {id, role, text, time} dicts.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            quoted = quote(session_id, safe="")
            raw = await self._request(
                "GET", f"/api/session/{quoted}/message", params={"limit": str(limit)}
            )
            data = _require_v2_data(raw, "GET", f"/api/session/{quoted}/message")
            items = data if isinstance(data, list) else []
            simplified = [_simplify_v2_message(m) for m in items if isinstance(m, dict)]
            return simplified[-limit:]
        path = f"/session/{quote(session_id, safe='')}/message"
        data = await self._request(
            "GET", path, params={"directory": self._dir(directory), "limit": limit}
        )
        items = data if isinstance(data, list) else []
        simplified = [simplify_message(m) for m in items if isinstance(m, dict)]
        return simplified[-limit:]

    async def abort_session(self, session_id: str, directory: str | None = None) -> bool:
        """Abort a running session.

        In V2 mode POST /api/session/{sessionID}/interrupt is used; the
        documented direct SessionInterruptResponse ({interrupted: bool},
        no data envelope) is validated and True is returned on success.

        Args:
            session_id: Session ID.
            directory: Opencode working directory (legacy only).

        Returns:
            True on success.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            quoted = quote(session_id, safe="")
            data = await self._request("POST", f"/api/session/{quoted}/interrupt")
            if not isinstance(data, dict) or not isinstance(data.get("interrupted"), bool):
                raise OpencodeError(
                    "POST",
                    f"/api/session/{quoted}/interrupt",
                    200,
                    "unsupported V2 interrupt response",
                )
            return True
        path = f"/session/{quote(session_id, safe='')}/abort"
        await self._request("POST", path, params={"directory": self._dir(directory)})
        return True

    async def delete_session(self, session_id: str, directory: str | None = None) -> bool:
        """Delete a session and all its data.

        In V2 mode DELETE /api/session/{sessionID} is used (204).

        Args:
            session_id: Session ID.
            directory: Opencode working directory (legacy only).

        Returns:
            True on success.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            quoted = quote(session_id, safe="")
            await self._request("DELETE", f"/api/session/{quoted}")
            return True
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

        In V2 mode GET /api/session/{sessionID}/diff is used with the
        documented from parameter for the message scope (no directory
        parameter exists) and its {data: [...]} envelope is unwrapped.

        Args:
            session_id: Session ID.
            message_id: Optional message to scope the diff.
            directory: Opencode working directory (legacy only).

        Returns:
            Raw file diff list from opencode.
        """
        await self._ensure_capability()
        if self._api_family == "v2":
            quoted = quote(session_id, safe="")
            params: dict[str, Any] = {}
            if message_id:
                params["from"] = message_id
            raw = await self._request("GET", f"/api/session/{quoted}/diff", params=params)
            data = _require_v2_data(raw, "GET", f"/api/session/{quoted}/diff")
            return data if isinstance(data, list) else []
        path = f"/session/{quote(session_id, safe='')}/diff"
        params = {"directory": self._dir(directory)}
        if message_id:
            params["messageID"] = message_id
        data = await self._request("GET", path, params=params)
        return data if isinstance(data, list) else []

    @staticmethod
    def _simplify_session(session: dict[str, Any]) -> dict[str, Any]:
        """Reduce a session object to the fields MCP clients need.

        V2 Session.Info carries the working directory inside
        location.directory instead of a top-level directory key; that
        fallback is applied here without touching the legacy shape.

        Args:
            session: Raw session dict.

        Returns:
            Dict with id, title, directory, agent, model, time, cost.
        """
        directory = session.get("directory")
        if directory is None:
            location = session.get("location")
            if isinstance(location, dict):
                directory = location.get("directory")
        return {
            "id": session.get("id"),
            "title": session.get("title"),
            "directory": directory,
            "agent": session.get("agent"),
            "model": session.get("model"),
            "time": session.get("time", {}),
            "cost": session.get("cost"),
        }
