"""Skill registry and executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class SkillContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    version: str
    description: str
    allowed_tools: tuple[str, ...]
    handler: Callable
    input_fields: tuple[str, ...]
    output_fields: tuple[str, ...] = ("status",)
    idempotent: bool = True


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, SkillSpec] = {}

    def register(self, spec: SkillSpec) -> None:
        if not spec.skill_id or spec.version == "":
            raise SkillContractError("skill_identity_required")
        self._skills[spec.skill_id] = spec

    def get(self, skill_id: str) -> SkillSpec:
        if skill_id not in self._skills:
            raise SkillContractError("unknown_skill:" + skill_id)
        return self._skills[skill_id]

    def ids(self) -> list[str]:
        return sorted(self._skills)

    def versions(self) -> dict[str, str]:
        return {key: spec.version for key, spec in sorted(self._skills.items())}


class SkillExecutor:
    def __init__(self, registry: SkillRegistry, gateway):
        self.registry = registry
        self.gateway = gateway

    def invoke(self, skill_id: str, arguments: dict, timeout: float | None = None) -> dict:
        spec = self.registry.get(skill_id)
        if not isinstance(arguments, dict):
            raise SkillContractError("arguments_not_object")
        trace = []

        def call(tool: str, tool_args: dict):
            if tool not in spec.allowed_tools:
                raise SkillContractError("tool_not_in_skill_contract:" + tool)
            result = self.gateway.call(tool, tool_args, timeout=timeout)
            trace.append(
                {
                    "tool": tool,
                    "arguments": tool_args,
                    "status": result["evidence"]["status"],
                    "evidence_id": result["evidence"]["evidence_id"],
                }
            )
            return result

        output = spec.handler(arguments, call)
        if not isinstance(output, dict) or "status" not in output:
            raise SkillContractError("skill_output_missing_status")
        output = dict(output)
        output["skill_id"] = spec.skill_id
        output["version"] = spec.version
        output["tool_trace"] = trace
        output.setdefault("untrusted_text", [])
        output.setdefault("facts", [])
        output.setdefault("items", [])
        output.setdefault("evidence", [])
        return output


def build_default_registry() -> SkillRegistry:
    from shopassist.skills import builtin

    registry = SkillRegistry()
    for spec in builtin.SPECS:
        registry.register(spec)
    return registry
