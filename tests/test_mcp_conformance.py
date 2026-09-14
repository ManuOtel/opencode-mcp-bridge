"""Deterministic MCP conformance smoke layer for the bridge. No network.

Covers the worker-first contract without real tokens, paid models,
remote OpenCode sessions, or deployments:
- initialize handshake + protocol version negotiation (repo pins 2025-06-18);
- tools/list exact safe worker surface (5 tools) and full catalog (16 tools);
- truthful input/output schema and MCP annotation exposure on the wire;
- worker_catalog free-first recommendations (paid fallback is rank 2 only);
- worker_run requestID deduplication and conflicting reuse;
- status/verify/cleanup error and scope behavior with fakes;
- bounded output and malformed-input handling.

Transport tests run in-process via Starlette TestClient (lifespan-managed,
no sockets). Tool tests use a minimal fake client (no httpx, no subprocess
except local `git` for one read-only verify bundle).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencode_mcp_bridge import server
from opencode_mcp_bridge.opencode_client import OpencodeError

REPO_PROTOCOL_VERSION = "2025-06-18"
FAKE_TOKEN = "conformance-fake-token-001"
WRONG_TOKEN = "conformance-wrong-token-999"

EXPECTED_WORKER_TOOLS = [
    "worker_catalog",
    "worker_cleanup",
    "worker_run",
    "worker_status",
    "worker_verify",
]

EXPECTED_FULL_COUNT = 16

EXPECTED_READ_ONLY = {
    "worker_catalog": True,
    "worker_cleanup": False,
    "worker_run": False,
    "worker_status": True,
    "worker_verify": True,
}

EXPECTED_DESTRUCTIVE = {
    "worker_catalog": False,
    "worker_cleanup": True,
    "worker_run": False,
    "worker_status": False,
    "worker_verify": False,
}

EXPECTED_OPEN_WORLD = {
    "worker_catalog": False,
    "worker_cleanup": False,
    "worker_run": True,
    "worker_status": False,
    "worker_verify": False,
}

EXPECTED_REQUIRED_PARAMS = {
    "worker_catalog": set(),
    "worker_cleanup": {"taskID"},
    "worker_run": {"message"},
    "worker_status": {"taskID"},
    "worker_verify": {"taskID"},
}


def _make_client(monkeypatch: pytest.MonkeyPatch):
    """Build a lifespan-managed test client with a fake bearer token."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MCP_BEARER_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv("MCP_BEARER_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("ENABLE_EXEC_RUN", raising=False)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_client", None)
    return TestClient(server.create_app())


def _headers(token: str | None = FAKE_TOKEN) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _init_body(version: str = REPO_PROTOCOL_VERSION) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "conformance", "version": "0"},
        },
    }


def _sse_payload(response: Any) -> dict[str, Any]:
    """Return the first SSE data payload; fail loudly when absent."""
    assert response.status_code == 200, response.text[:500]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no SSE data payload in: {response.text[:500]}")


def _sse_tools(response: Any) -> list[dict[str, Any]]:
    payload = _sse_payload(response)
    tools = payload.get("result", {}).get("tools", [])
    assert isinstance(tools, list) and tools, f"no tools in: {response.text[:500]}"
    return tools


class _FakeConformanceClient:
    """Minimal fake: counting sessions, canned status/output, catalog payload."""

    default_directory = "/home/tester"
    default_provider_id = "opencode"
    default_model_id = "muse-spark-1.3-contributor-free"

    def __init__(self) -> None:
        self.created: list[tuple[Any, Any]] = []
        self.prompted: list[tuple[Any, ...]] = []
        self.aborted: list[tuple[Any, Any]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.delete_error: Exception | None = None
        self.status_map: dict[str, Any] = {}
        self.latest: dict[str, Any] = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }
        self.providers_raw: dict[str, Any] = {}
        self._next = 0

    def resolve_model(self, provider_id: Any, model_id: Any) -> tuple[str, str]:
        if bool(provider_id) != bool(model_id):
            raise ValueError("provider_id and model_id must be given together or omitted")
        return (
            provider_id or self.default_provider_id,
            model_id or self.default_model_id,
        )

    async def create_session(self, title: Any, directory: Any) -> dict[str, Any]:
        self.created.append((title, directory))
        self._next += 1
        return {
            "id": f"ses_{self._next}",
            "title": title,
            "directory": directory or self.default_directory,
        }

    async def prompt_async(self, *args: Any, **kwargs: Any) -> bool:
        self.prompted.append(args)
        return True

    async def abort_session(self, session_id: str, directory: Any = None) -> bool:
        self.aborted.append((session_id, directory))
        return True

    async def delete_session(self, session_id: str, directory: Any = None) -> bool:
        self.deleted.append((session_id, directory))
        if self.delete_error is not None:
            raise self.delete_error
        return True

    async def get_session(self, session_id: str, directory: Any = None) -> dict[str, Any]:
        return {"id": session_id, "title": None, "directory": directory}

    async def get_session_status(self, directory: Any = None) -> dict[str, Any]:
        return self.status_map

    async def get_latest_assistant(
        self, session_id: str, directory: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.latest

    async def get_providers_raw(self) -> dict[str, Any]:
        return self.providers_raw


def _patch(monkeypatch: pytest.MonkeyPatch) -> _FakeConformanceClient:
    fake = _FakeConformanceClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def _catalog_payload() -> dict[str, Any]:
    return {
        "connected": ["opencode", "opencode-go"],
        "default": {},
        "all": [
            {
                "id": "opencode",
                "name": "Opencode",
                "models": {
                    "muse-spark-1.3-contributor-free": {
                        "id": "muse-spark-1.3-contributor-free",
                        "name": "Muse Spark Free",
                        "cost": {"input": 0, "output": 0},
                    },
                    "paid-1": {
                        "id": "paid-1",
                        "name": "Paid One",
                        "cost": {"input": 3, "output": 15},
                    },
                    "gpt-5": {
                        "id": "gpt-5",
                        "name": "GPT-5",
                        "cost": {"input": 0, "output": 0},
                    },
                },
            },
            {
                "id": "opencode-go",
                "name": "OpenCode Go",
                "models": {
                    "muse-spark-1.3-contributor": {
                        "id": "muse-spark-1.3-contributor",
                        "name": "Muse Spark 1.3 Contributor",
                    },
                },
            },
        ],
    }


def test_initialize_echoes_repo_protocol_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both endpoints echo the repo protocol version with per-endpoint names."""
    expected_names = {"/mcp": "opencode-bridge", "/worker-mcp": "opencode-bridge-worker"}
    with _make_client(monkeypatch) as client:
        for path, expected_name in expected_names.items():
            payload = _sse_payload(client.post(path, json=_init_body(), headers=_headers()))
            result = payload["result"]
            assert result["protocolVersion"] == REPO_PROTOCOL_VERSION
            assert "tools" in result["capabilities"]
            assert result["serverInfo"]["name"] == expected_name
            assert FAKE_TOKEN not in json.dumps(payload)


def test_initialize_negotiates_unknown_version_without_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown client version still handshakes on a server version, no secret."""
    with _make_client(monkeypatch) as client:
        payload = _sse_payload(
            client.post("/worker-mcp", json=_init_body("1999-01-01"), headers=_headers())
        )
        assert payload.get("error") is None
        negotiated = payload["result"]["protocolVersion"]
        assert isinstance(negotiated, str) and negotiated
        assert negotiated != "1999-01-01"
        assert FAKE_TOKEN not in json.dumps(payload)
        assert WRONG_TOKEN not in json.dumps(payload)


def test_worker_tools_list_exact_safe_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safe endpoint exposes exactly the five worker tools, never exec_run."""
    with _make_client(monkeypatch) as client:
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        tools = _sse_tools(client.post("/worker-mcp", json=body, headers=_headers()))
    names = sorted(tool["name"] for tool in tools)
    assert names == EXPECTED_WORKER_TOOLS
    assert "exec_run" not in names


def test_full_tools_list_exact_sixteen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy endpoint keeps the full 16-tool catalog including exec_run."""
    with _make_client(monkeypatch) as client:
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        tools = _sse_tools(client.post("/mcp", json=body, headers=_headers()))
    names = sorted(tool["name"] for tool in tools)
    assert names == sorted(server.ALL_TOOL_NAMES)
    assert len(names) == EXPECTED_FULL_COUNT
    assert set(EXPECTED_WORKER_TOOLS) <= set(names)
    assert "exec_run" in names


def test_tools_list_schema_and_annotations_truthful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wire schemas/annotations exist and match the in-process tool definitions."""
    live = {tool.name: tool for tool in asyncio.run(server.worker_mcp.list_tools())}
    with _make_client(monkeypatch) as client:
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        tools = _sse_tools(client.post("/worker-mcp", json=body, headers=_headers()))
    assert len(tools) == len(EXPECTED_WORKER_TOOLS)
    for entry in tools:
        name = entry["name"]
        assert name in EXPECTED_WORKER_TOOLS
        assert isinstance(entry.get("description"), str) and entry["description"].strip()
        input_schema = entry.get("inputSchema")
        assert isinstance(input_schema, dict) and input_schema.get("type") == "object"
        assert isinstance(input_schema.get("properties"), dict)
        required = set(input_schema.get("required") or [])
        assert required == EXPECTED_REQUIRED_PARAMS[name]
        output_schema = entry.get("outputSchema")
        assert isinstance(output_schema, dict) and output_schema.get("type") == "object"
        annotations = entry.get("annotations")
        assert isinstance(annotations, dict)
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(annotations.get(hint), bool), (name, hint)
        assert annotations["readOnlyHint"] is EXPECTED_READ_ONLY[name]
        assert annotations["destructiveHint"] is EXPECTED_DESTRUCTIVE[name]
        assert annotations["openWorldHint"] is EXPECTED_OPEN_WORLD[name]
        # The wire payload must not drift from the implementation definition.
        assert annotations["readOnlyHint"] is bool(live[name].annotations.read_only_hint)
        assert annotations["destructiveHint"] is bool(live[name].annotations.destructive_hint)
        assert annotations["openWorldHint"] is bool(live[name].annotations.open_world_hint)


def test_worker_catalog_free_first_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free default ranks first; paid fallback is explicit-request rank 2 only."""
    fake = _patch(monkeypatch)
    fake.providers_raw = _catalog_payload()
    result = asyncio.run(server.worker_catalog())
    # Zero-cost without a "free" marker never counts as free.
    assert [m["modelID"] for m in result["models"]] == ["muse-spark-1.3-contributor-free"]
    assert result["total"] == 1
    assert result["default"] == {
        "providerID": "opencode",
        "modelID": "muse-spark-1.3-contributor-free",
    }
    recs = result["recommendations"]
    assert [(r["rank"], r["providerID"], r["modelID"]) for r in recs] == [
        (1, "opencode", "muse-spark-1.3-contributor-free"),
        (2, "opencode-go", "muse-spark-1.3-contributor"),
    ]
    assert recs[0]["free"] is True
    assert recs[0]["requires_explicit_request"] is False
    assert recs[1]["free"] is False
    assert recs[1]["requires_explicit_request"] is True
    assert recs[1]["reason"] == "paid-fallback-use-only-when-explicitly-requested"
    # Filters hide paid models from `models` but never from `recommendations`.
    queried = asyncio.run(server.worker_catalog(query="zzz-no-match"))
    assert queried["models"] == [] and queried["total"] == 0
    assert [r["modelID"] for r in queried["recommendations"]] == [
        "muse-spark-1.3-contributor-free",
        "muse-spark-1.3-contributor",
    ]


def test_worker_run_dedup_and_conflicting_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same requestID + same inputs deduplicates; conflicts fail without effects."""
    fake = _patch(monkeypatch)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await server.worker_run("do X", directory="/tmp/w", requestID="req-1")
        second = await server.worker_run("do X", directory="/tmp/w", requestID="req-1")
        return first, second

    first, second = asyncio.run(run())
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["taskID"] == first["taskID"]
    assert len(fake.created) == 1
    created_before = list(fake.created)
    with pytest.raises(ValueError, match="different inputs"):
        asyncio.run(server.worker_run("do Y", directory="/tmp/w", requestID="req-1"))
    assert fake.created == created_before
    with pytest.raises(ValueError, match="requestID"):
        asyncio.run(server.worker_run("hi", requestID="  "))
    with pytest.raises(ValueError, match="requestID"):
        asyncio.run(server.worker_run("hi", requestID="x" * 200))
    assert fake.created == created_before


def test_status_verify_cleanup_error_and_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error states, input validation, directory scope, and record isolation."""
    fake = _patch(monkeypatch)
    created = asyncio.run(server.worker_run("hi", directory="/tmp/w"))
    task_id = created["taskID"]
    other = asyncio.run(server.worker_run("other", directory="/tmp/w"))
    other_id = other["taskID"]

    async def probe() -> tuple[dict[str, Any], dict[str, Any]]:
        fake.status_map = {task_id: {"type": "idle"}}
        fake.latest = {
            "messageID": "m1",
            "text": "boom",
            "total_chars": 4,
            "has_error": True,
        }
        error_result = await server.worker_status(task_id)
        fake.latest = {
            "messageID": None,
            "text": "",
            "total_chars": 0,
            "has_error": False,
        }
        unknown_result = await server.worker_status("ses_missing")
        return error_result, unknown_result

    error_result, unknown_result = asyncio.run(probe())
    assert error_result["state"] == "error"
    assert unknown_result["state"] == "unknown"
    assert unknown_result["status"] is None

    with pytest.raises(ValueError, match="taskID"):
        asyncio.run(server.worker_verify("  "))
    with pytest.raises(ValueError, match="taskID"):
        asyncio.run(server.worker_cleanup("  ", action="delete"))
    with pytest.raises(ValueError, match="action"):
        asyncio.run(server.worker_cleanup(task_id, action="restart"))
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.worker_status(task_id, directory="/nope/outside-root"))
    with pytest.raises(ValueError, match="not within allowed"):
        asyncio.run(server.worker_verify(task_id, directory="/nope/outside-root"))

    # Non-git directories verify cleanly with ok=False; only this task is touched.
    plain = tmp_path / "plain"
    plain.mkdir()
    verified = asyncio.run(server.worker_verify(task_id, directory=str(plain)))
    assert verified["verification"]["ok"] is False
    assert verified["verification"]["directory"] == str(plain)

    fake.delete_error = OpencodeError("DELETE", "/session/x", 404, "gone")
    removed = asyncio.run(server.worker_cleanup(task_id, "/tmp/w", action="delete"))
    assert removed == {
        "taskID": task_id,
        "sessionID": task_id,
        "action": "delete",
        "aborted": True,
        "deleted": True,
        "directory": "/tmp/w",
        "cleanup_warning": "session already gone; record removed",
    }
    stored = server._load_task_state()
    assert task_id not in stored
    assert other_id in stored
    assert {session for session, _ in fake.deleted} == {task_id}


def test_bounded_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output caps clamp deterministically with exact truncation counts."""
    fake = _patch(monkeypatch)
    fake.status_map = {"ses_1": {"type": "busy"}}
    fake.latest = {
        "messageID": "m1",
        "text": "x" * 100,
        "total_chars": 100,
        "has_error": False,
    }

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        small = await server.worker_status("ses_1", max_output_chars=10)
        floored = await server.worker_status("ses_1", max_output_chars=0)
        return small, floored

    small, floored = asyncio.run(run())
    assert small["output"] == "x" * 10
    assert (small["output_chars"], small["total_chars"], small["truncated_chars"]) == (10, 100, 90)
    assert small["truncated"] is True
    assert floored["output"] == "x"
    assert floored["output_chars"] == 1

    big = "y" * (server.WORKER_OUTPUT_MAX_CHARS + 25)
    fake.latest = {
        "messageID": "m1",
        "text": big,
        "total_chars": len(big),
        "has_error": False,
    }
    clamped = asyncio.run(server.worker_status("ses_1", max_output_chars=10**9))
    assert len(clamped["output"]) == server.WORKER_OUTPUT_MAX_CHARS
    assert clamped["truncated_chars"] == 25
    assert clamped["truncated"] is True


def test_malformed_input_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad tool calls fail closed as errors without crashing or leaking secrets."""
    _patch(monkeypatch)
    with _make_client(monkeypatch) as client:
        unknown_body = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
        unknown = _sse_payload(client.post("/mcp", json=unknown_body, headers=_headers()))
        assert unknown["result"]["isError"] is True
        assert "Unknown tool" in unknown["result"]["content"][0]["text"]

        missing_arg_body = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "worker_status", "arguments": {}},
        }
        missing = _sse_payload(
            client.post("/worker-mcp", json=missing_arg_body, headers=_headers())
        )
        assert missing["result"]["isError"] is True

        bad_method_body = {"jsonrpc": "2.0", "id": 5, "method": "nope/method", "params": {}}
        bad = _sse_payload(client.post("/worker-mcp", json=bad_method_body, headers=_headers()))
        assert bad.get("error") is not None or bad.get("result", {}).get("isError") is True

        raw = json.dumps(_init_body()).encode()
        oversized_headers = dict(_headers())
        oversized_headers["Content-Length"] = str(10**9)
        response = client.post("/worker-mcp", content=raw, headers=oversized_headers)
        assert response.status_code in (401, 408, 413, 422)
        assert FAKE_TOKEN not in response.text

    with pytest.raises(ValueError, match="together"):
        asyncio.run(server.worker_run("hi", providerID="only-provider"))
