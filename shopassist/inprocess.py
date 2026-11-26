"""In-process tool gateway for the static demo.

The local server still starts an MCP child process. This gateway calls the
same tool server and skill handlers without a process, so a browser Python
runtime can execute a turn. It is not a second catalog implementation.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import mkdtemp

from shopassist.catalog import default_catalog
from shopassist.knowledge import default_kb
from shopassist.mcp.client import _untrusted_strings
from shopassist.mcp.contracts import TOOL_SPECS
from shopassist.mcp.server import ToolServer
from shopassist.sandbox import SandboxError
from shopassist.skills.registry import SkillExecutor, build_default_registry


class InProcessGateway:
    def __init__(self, server: ToolServer):
        self.server = server
        self.discovered_tools = set(TOOL_SPECS)
        self.evidence_counter = 0
        self.events: list[dict] = []

    def call(self, tool: str, arguments: dict, timeout: float | None = None) -> dict:
        del timeout  # the caller already bounded the turn; there is no socket to time out
        if tool not in self.discovered_tools:
            raise SandboxError("tool_not_allowlisted:" + tool)
        data = self.server.call_tool(tool, arguments)
        spec = TOOL_SPECS.get(tool)
        if spec:
            problems = spec["validate_output"](data)
            if problems:
                data = {
                    "status": "malformed",
                    "error_code": "client_rejected_output",
                    "validation_errors": problems,
                }
        self.evidence_counter += 1
        evidence = {
            "evidence_id": f"ev-{self.evidence_counter}",
            "tool_name": tool,
            "status": data.get("status"),
            "snapshot_id": data.get("snapshot_id"),
            "kb_version": data.get("kb_version"),
            "payload": data,
            "untrusted_text": _untrusted_strings(data),
        }
        self.events.append(
            {
                "method": "tools/call",
                "name": tool,
                "arguments": arguments,
                "status": data.get("status"),
                "evidence_id": evidence["evidence_id"],
                "transport": "in_process",
            }
        )
        return {
            "data": data,
            "evidence": evidence,
            "is_error": data.get("status") in {"error", "timeout", "malformed"},
        }


class InProcessSandbox:
    """Same invoke surface as SessionSandbox, without an MCP process."""

    def __init__(self, *, seed: int = 0, faults: list[dict] | None = None, max_calls: int = 30, timeout: float = 2.0, index_dir: str | None = None, knowledge: dict | None = None):
        self.seed = seed
        self.faults = list(faults or [])
        self.max_calls = max_calls
        self.timeout = timeout
        self.index_dir = index_dir
        self.knowledge = knowledge
        self.process = None
        self.registry = build_default_registry()
        self.root: Path | None = None
        self.server: ToolServer | None = None
        self.gateway: InProcessGateway | None = None
        self.executor: SkillExecutor | None = None
        self._boot()

    def _boot(self) -> None:
        self.root = Path(mkdtemp(prefix="shopassist-inprocess-"))
        workspace = self.root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        profile = {"seed": self.seed, "faults": self.faults, "max_calls": self.max_calls}
        if self.index_dir:
            profile["kb_index"] = str(self.index_dir)
        if self.knowledge:
            profile["kb_encoder"] = {
                "encoder": self.knowledge.get("encoder"),
                "embedding_model": self.knowledge.get("embedding_model") or "",
                "embedding_dim": self.knowledge.get("embedding_dim"),
                "encoder_version": self.knowledge.get("encoder_version"),
                "top_k": self.knowledge.get("top_k"),
                "max_hits": self.knowledge.get("max_hits"),
                "dedup_cosine": self.knowledge.get("dedup_cosine"),
                "score_threshold": self.knowledge.get("score_threshold"),
            }
        self.server = ToolServer(default_catalog(), default_kb(), profile, workspace)
        self.gateway = InProcessGateway(self.server)
        self.executor = SkillExecutor(self.registry, self.gateway)

    def invoke(self, skill_id: str, arguments: dict, timeout: float | None = None) -> dict:
        if self.executor is None:
            raise SandboxError("sandbox_closed")
        return self.executor.invoke(skill_id, arguments, timeout=timeout)

    def read_name_index(self) -> list[dict]:
        if self.server is None:
            raise SandboxError("sandbox_closed")
        payload = self.server.read_resource("catalog://name-index")
        return list(payload.get("items") or [])

    def pid(self) -> int | None:
        return None

    def reset(self) -> None:
        self.close()
        self._boot()

    def close(self) -> None:
        root = self.root
        self.root = None
        self.server = None
        self.gateway = None
        self.executor = None
        if root is not None and root.exists():
            shutil.rmtree(root, ignore_errors=True)
