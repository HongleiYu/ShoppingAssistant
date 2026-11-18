"""MCP client over framed stdio streams."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from shopassist.mcp.contracts import TOOL_SPECS
from shopassist.mcp.framing import read_message, write_message
from shopassist.redact import redact_text
from shopassist.versioning import MCP_PROTOCOL_VERSION, PACKAGE_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]


class MCPError(RuntimeError):
    pass


def clean_child_env() -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.environ.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    return env


class MCPClient:
    def __init__(self, to_server_fd: int, from_server_fd: int, timeout: float = 2.0):
        self.to_server_fd = to_server_fd
        self.from_server_fd = from_server_fd
        self.timeout = timeout
        self._next_id = 1
        self.discovered_tools: dict[str, dict] = {}
        self.events: list[dict] = []
        self.evidence_counter = 0

    def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        write_message(
            self.to_server_fd,
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params},
        )
        message = read_message(self.from_server_fd, timeout=self.timeout if timeout is None else timeout)
        if message.get("id") != msg_id:
            raise MCPError("mcp_response_id_mismatch")
        if "error" in message:
            raise MCPError("mcp_rpc_error:" + str(message["error"].get("message", "error")))
        return message.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        write_message(self.to_server_fd, {"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "shopassist", "version": PACKAGE_VERSION},
            },
            timeout=5,
        )
        if result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise MCPError("mcp_protocol_mismatch")
        self.notify("notifications/initialized", {})
        listed = self.request("tools/list", {})
        self.discovered_tools = {tool["name"]: tool for tool in listed.get("tools", [])}
        self.events.append({"method": "tools/list", "tools": sorted(self.discovered_tools)})
        resources = self.request("resources/list", {})
        self.events.append(
            {"method": "resources/list", "uris": [item.get("uri") for item in resources.get("resources", [])]}
        )
        return result

    def read_resource(self, uri: str) -> dict:
        result = self.request("resources/read", {"uri": uri})
        self.events.append({"method": "resources/read", "uri": uri})
        contents = result.get("contents") or []
        if not contents:
            raise MCPError("mcp_empty_resource")
        import json

        return json.loads(contents[0]["text"])

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in TOOL_SPECS and name not in self.discovered_tools:
            raise MCPError("tool_not_allowlisted")
        if name not in self.discovered_tools:
            raise MCPError("mcp_tool_not_advertised")
        try:
            result = self.request("tools/call", {"name": name, "arguments": arguments})
        except TimeoutError as exc:
            self.events.append({"method": "tools/call", "name": name, "status": "timeout"})
            raise TimeoutError("mcp_read_timeout") from exc
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            structured = {"status": "malformed"}
        spec = TOOL_SPECS.get(name)
        if spec:
            problems = spec["validate_output"](structured)
            if problems:
                structured = {
                    "status": "malformed",
                    "error_code": "client_rejected_output",
                    "validation_errors": problems,
                }
        self.evidence_counter += 1
        evidence = {
            "evidence_id": f"ev-{self.evidence_counter}",
            "tool_name": name,
            "status": structured.get("status"),
            "snapshot_id": structured.get("snapshot_id"),
            "kb_version": structured.get("kb_version"),
            "payload": structured,
            "untrusted_text": _untrusted_strings(structured),
        }
        self.events.append(
            {
                "method": "tools/call",
                "name": name,
                "arguments": arguments,
                "status": structured.get("status"),
                "evidence_id": evidence["evidence_id"],
            }
        )
        return {"data": structured, "evidence": evidence, "is_error": bool(result.get("isError"))}


def _untrusted_strings(node, out=None) -> list[str]:
    if out is None:
        out = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"body", "text", "untrusted_text"} and isinstance(value, str):
                out.append(value)
            else:
                _untrusted_strings(value, out)
    elif isinstance(node, list):
        for item in node:
            _untrusted_strings(item, out)
    return out


class MCPProcess:
    """A tool server in a separate process with a minimal environment."""

    def __init__(self, catalog_path: Path, kb_path: Path, profile_path: Path, workspace: Path, timeout: float = 2.0):
        self.workspace = workspace
        command = [
            sys.executable,
            "-m",
            "shopassist.mcp.server",
            "--catalog",
            str(catalog_path),
            "--kb",
            str(kb_path),
            "--profile",
            str(profile_path),
            "--workspace",
            str(workspace),
        ]
        self.proc = subprocess.Popen(  # noqa: S603 — fixed interpreter and module, no shell
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_child_env(),
            cwd=str(REPO_ROOT),
            start_new_session=True,
            bufsize=0,
        )
        self.stderr_chunks: list[bytes] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        assert self.proc.stdin and self.proc.stdout
        self.client = MCPClient(self.proc.stdin.fileno(), self.proc.stdout.fileno(), timeout=timeout)

    def _drain_stderr(self) -> None:
        assert self.proc.stderr
        try:
            while True:
                chunk = self.proc.stderr.read(1024)
                if not chunk:
                    break
                self.stderr_chunks.append(chunk)
                if sum(len(part) for part in self.stderr_chunks) > 65536:
                    self.stderr_chunks = self.stderr_chunks[-16:]
        except Exception:
            return

    def stderr_text(self) -> str:
        raw = b"".join(self.stderr_chunks).decode("utf-8", errors="replace")
        cleaned, _ = redact_text(raw)
        return cleaned[-2000:]

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=2)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe:
                try:
                    pipe.close()
                except Exception:
                    pass
