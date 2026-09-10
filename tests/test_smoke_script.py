"""Local tests for the POSIX transport smoke script."""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke.sh"


class ChunkedOversizeHandler(http.server.BaseHTTPRequestHandler):
    """Serve a local chunked response large enough to exercise the cap."""

    protocol_version = "HTTP/1.1"
    total_sent = 0
    total_bytes = 4 * 1024 * 1024

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep subprocess test output deterministic."""

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/worker-mcp":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != "Bearer smoke-test-token":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        remaining = self.total_bytes
        chunk = b"x" * 65536
        try:
            while remaining:
                data = chunk[: min(remaining, len(chunk))]
                self.wfile.write(f"{len(data):x}\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                type(self).total_sent += len(data)
                remaining -= len(data)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.mark.skipif(
    any(shutil.which(command) is None for command in ("curl", "jq", "mkfifo")),
    reason="curl, jq, and mkfifo are required",
)
def test_smoke_script_stops_reading_oversized_chunked_response() -> None:
    """An oversized chunked body is rejected without downloading it all."""
    ChunkedOversizeHandler.total_sent = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChunkedOversizeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        result = subprocess.run(
            ["sh", str(SCRIPT)],
            env={
                **os.environ,
                "MCP_URL": f"{base_url}/worker-mcp",
                "OPENCODE_MCP_BEARER_TOKEN": "smoke-test-token",
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode != 0
    assert "response_bytes=512001" in result.stdout
    assert "tools/list response too large" in result.stderr
    assert "smoke-test-token" not in result.stdout + result.stderr
    assert ChunkedOversizeHandler.total_sent < ChunkedOversizeHandler.total_bytes
