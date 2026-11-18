"""Per-session tool sandbox.

Threat model, stated plainly:
- This is a deterministic tool simulator with process isolation for catalog and
  knowledge state. It is not a secure sandbox for arbitrary code.
- The tool server is a separate OS process with a minimal environment. It has
  no shell tool, no purchase tool, and no network client.
- This demo does not apply container, cgroup, or memory-cap boundaries.
- Bounds that do exist: framed message size, per-session tool-call budget,
  client read timeout, and an allowlisted tool map.
- Session writes stay in a temp workspace that reset() clears.
- Untrusted document text is returned as data and is not executed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from tempfile import mkdtemp

from shopassist.catalog import export_catalog
from shopassist.knowledge import export_kb
from shopassist.mcp.client import MCPError, MCPProcess
from shopassist.mcp.contracts import TOOL_SPECS
from shopassist.skills.registry import SkillExecutor, build_default_registry


class SandboxError(RuntimeError):
    pass


class ToolGateway:
    def __init__(self, client, allowlist: set[str]):
        self.client = client
        self.allowlist = allowlist

    def call(self, tool: str, arguments: dict) -> dict:
        if tool not in self.allowlist:
            raise SandboxError("tool_not_allowlisted:" + tool)
        if tool not in self.client.discovered_tools:
            raise SandboxError("mcp_tool_not_advertised:" + tool)
        return self.client.call_tool(tool, arguments)


class SessionSandbox:
    def __init__(self, root: Path, seed: int, faults: list[dict], max_calls: int = 30, timeout: float = 2.0):
        self.root = root
        self.seed = seed
        self.faults = faults
        self.max_calls = max_calls
        self.timeout = timeout
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.process: MCPProcess | None = None
        self.executor: SkillExecutor | None = None
        self.registry = build_default_registry()
        self._start()

    @classmethod
    def start(cls, *, seed: int = 0, faults: list[dict] | None = None, max_calls: int = 30, timeout: float = 2.0, root: Path | None = None):
        base = Path(root) if root else Path(mkdtemp(prefix="shopassist-session-"))
        return cls(base, seed, list(faults or []), max_calls=max_calls, timeout=timeout)

    def _write_inputs(self) -> tuple[Path, Path, Path]:
        catalog_path = self.root / "catalog.json"
        kb_path = self.root / "kb.json"
        profile_path = self.root / "profile.json"
        export_catalog(catalog_path)
        export_kb(kb_path)
        profile_path.write_text(
            json.dumps({"seed": self.seed, "faults": self.faults, "max_calls": self.max_calls}, sort_keys=True),
            encoding="utf-8",
        )
        return catalog_path, kb_path, profile_path

    def _start(self) -> None:
        catalog_path, kb_path, profile_path = self._write_inputs()
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            self.process = MCPProcess(catalog_path, kb_path, profile_path, self.workspace, timeout=self.timeout)
            self.process.client.initialize()
        except Exception as exc:
            stderr = self.process.stderr_text() if self.process else ""
            self.close()
            raise SandboxError("mcp_server_failed:" + type(exc).__name__ + ":" + stderr[-500:]) from exc
        missing = sorted(set(TOOL_SPECS) - set(self.process.client.discovered_tools))
        if missing:
            self.close()
            raise SandboxError("mcp_tool_not_advertised:" + ",".join(missing))
        gateway = ToolGateway(self.process.client, set(TOOL_SPECS))
        self.executor = SkillExecutor(self.registry, gateway)

    def invoke(self, skill_id: str, arguments: dict) -> dict:
        if self.executor is None:
            raise SandboxError("sandbox_closed")
        return self.executor.invoke(skill_id, arguments)

    def read_name_index(self) -> list[dict]:
        if self.process is None:
            raise SandboxError("sandbox_closed")
        payload = self.process.client.read_resource("catalog://name-index")
        return list(payload.get("items") or [])

    def pid(self) -> int | None:
        if self.process and self.process.proc:
            return self.process.proc.pid
        return None

    def reset(self) -> None:
        self.close_process()
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._start()

    def close_process(self) -> None:
        if self.process is not None:
            self.process.close()
            self.process = None
        self.executor = None

    def close(self) -> None:
        self.close_process()
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    def replay_rows(self) -> list[dict]:
        path = self.workspace / "replay.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
