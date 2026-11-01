"""Shared helpers: load scripts/server.py as a module and drive the stdio loop."""

from __future__ import annotations

import atexit
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

# Keep the quota counter of McpServer() out of the real secrets/ folder during tests.
_TEST_DIR = tempfile.mkdtemp(prefix="yt-mcp-tests-")
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)
os.environ["YOUTUBE_QUOTA_FILE"] = str(Path(_TEST_DIR) / "quota_usage.json")

SERVER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "server.py"

_spec = importlib.util.spec_from_file_location("youtube_studio_server", SERVER_PATH)
server = importlib.util.module_from_spec(_spec)
sys.modules["youtube_studio_server"] = server
_spec.loader.exec_module(server)


def run_stdio(raw_input: bytes) -> bytes:
    """Feed raw bytes to McpServer.serve() and return everything it wrote."""
    server.McpServer._framing = "headers"
    out = io.BytesIO()
    srv = server.McpServer()
    with mock.patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(raw_input))), \
            mock.patch.object(sys, "stdout", types.SimpleNamespace(buffer=out)):
        srv.serve()
    return out.getvalue()


def ndjson(*messages: dict) -> bytes:
    return b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)


def framed(*messages: dict) -> bytes:
    parts = []
    for m in messages:
        body = json.dumps(m).encode("utf-8")
        parts.append(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body)
    return b"".join(parts)


def parse_ndjson(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def parse_framed(raw: bytes) -> list[dict]:
    messages = []
    while raw:
        head, _, rest = raw.partition(b"\r\n\r\n")
        length = int(head.decode("utf-8").split(":", 1)[1])
        messages.append(json.loads(rest[:length].decode("utf-8")))
        raw = rest[length:]
    return messages


def make_client() -> "server.YouTubeClient":
    """A YouTubeClient whose OAuth token is always valid ("test-token")."""
    auth = server.AuthConfig(Path("client_secret.json"), Path("token.json"))
    client = server.YouTubeClient(auth)
    client._access_token = lambda: "test-token"  # type: ignore[method-assign]
    return client


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
