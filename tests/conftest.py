"""A minimal JSON-RPC-over-stdio client for exercising the real server process.

Tests drive the server exactly as a host would: spawn it, write newline-framed
JSON to its stdin, read frames from its stdout. Every raw stdout line is kept
so the isolation tests can assert the channel stayed clean.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StdioClient:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_refund_server"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**__import__("os").environ, "MCP_LOG_LEVEL": "DEBUG"},
        )
        self._next_id = 1
        self._lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._arrived = threading.Condition()
        self.stdout_lines: list[str] = []
        self.stderr_chunks: list[str] = []

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            self.stdout_lines.append(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # Recorded verbatim; the purity test will flag it.
            if isinstance(msg, dict) and "id" in msg and msg["id"] is not None:
                with self._arrived:
                    self._responses[msg["id"]] = msg
                    self._arrived.notify_all()

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_chunks.append(line)

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_chunks)

    def send_raw(self, text: str) -> None:
        """Write a byte-exact frame, for malformed-input tests."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(text)
        self.proc.stdin.flush()

    def send(self, method: str, params=None, timeout: float = 10.0) -> dict:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1

        frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        # allow_nan=True so tests can put NaN/Infinity literals on the wire.
        self.send_raw(json.dumps(frame, allow_nan=True) + "\n")

        with self._arrived:
            ok = self._arrived.wait_for(lambda: request_id in self._responses, timeout=timeout)
            if not ok:
                raise TimeoutError(f"no response to {method} (id={request_id})")
            return self._responses.pop(request_id)

    def notify(self, method: str, params=None) -> None:
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self.send_raw(json.dumps(frame) + "\n")

    def initialize(self) -> dict:
        result = self.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def call_tool(self, name: str, arguments) -> dict:
        return self.send("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


@pytest.fixture(scope="session")
def client():
    c = StdioClient()
    c.initialize()
    yield c
    c.close()


def payload(result: dict) -> dict:
    """Tool results are JSON-serialised into a single text content block."""
    return json.loads(result["content"][0]["text"])
