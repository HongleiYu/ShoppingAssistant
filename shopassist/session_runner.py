"""Multi-turn runtime. Tool facts come from the session sandbox."""

from __future__ import annotations

import os
import time

from shopassist.communication import prepare_turn, task_from_decision
from shopassist.config import ConfigError
from shopassist.constraints import clone_memory, empty_memory
from shopassist.planner_policy import PlanRegistry, act, new_planner_state, resolve_arguments
from shopassist.reply_render import (
    render_clarification,
    render_direct,
    render_escalation,
    render_items,
    render_no_match,
    render_policy,
)
from shopassist.sandbox import SessionSandbox
from shopassist.versioning import BANNED_TRACE_KEYS


class SchemaViolation(RuntimeError):
    pass


def run_session(
    user_turns: list[str],
    *,
    seed: int = 0,
    faults: list[dict] | None = None,
    session_id: str = "session",
    mode: str = "mock",
    model_clients: dict | None = None,
    sandbox: SessionSandbox | None = None,
) -> dict:
    if mode != "mock":
        raise ConfigError(
            "configured_mode_not_executed_here",
            {"mode": mode, "mock_fallback": False, "hint": "Use a configured session object after readiness checks."},
        )
    own = sandbox is None
    box = sandbox or SessionSandbox.start(seed=seed, faults=faults or [])
    started = time.perf_counter()
    try:
        session = AgentSession(box, session_id=session_id, mode="mock")
        for text in user_turns:
            session.user_message(text)
        trace = session.trace()
        trace["latency_ms"] = int((time.perf_counter() - started) * 1000)
        trace["token_usage"] = None
        trace["token_usage_reason"] = "unavailable_for_mock"
        trace["evaluation_kind"] = "mock_demo"
        _reject_banned(trace)
        return trace
    finally:
        if own:
            box.close()


class AgentSession:
    def __init__(self, sandbox: SessionSandbox, session_id: str, mode: str = "mock", model_clients=None, plan_registry: PlanRegistry | None = None):
        if mode not in {"mock", "configured_endpoint"}:
            raise ConfigError("unknown_mode", {"mode": mode})
        self.sandbox = sandbox
        self.session_id = session_id
        self.mode = mode
        self.model_clients = model_clients
        self.plan_registry = plan_registry or PlanRegistry()
        self.memory = empty_memory()
        self.history: list[dict] = []
        self.task_counter = 0
        self.name_index = sandbox.read_name_index()
        if mode == "configured_endpoint" and not model_clients:
            raise ConfigError("configured_model_not_ready", {"missing": ["model_client"], "mock_fallback": False})

    def user_message(self, text: str) -> dict:
        if self.mode == "configured_endpoint":
            return self._model_message(text)
        memory_before = clone_memory(self.memory)
        features, decision, memory = prepare_turn(text, self.memory, self.name_index)
        self.memory = memory
        turn = {
            "memory_before": memory_before,
            "user_text": text,
            "route": decision["route"],
            "route_reason_code": decision["route_reason_code"],
            "planner_invoked": False,
            "task_id": None,
            "plan": [],
            "skills": [],
            "tools": [],
            "recovery": [],
            "claims": [],
            "limitations": [],
            "evidence": [],
            "reply_text": "",
            "escalated": False,
            "memory": clone_memory(self.memory),
            "schema_ok": True,
        }
        if decision["route"] == "direct_answer":
            rendered = render_direct(decision["intent"])
        elif decision["route"] == "clarification":
            rendered = render_clarification(decision)
        elif decision["route"] == "human_escalation":
            rendered = render_escalation("user_requested_person")
            turn["escalated"] = True
        elif decision["route"] == "simple_skill":
            rendered = self._simple_skill(decision, turn)
        elif decision["route"] == "complex_handoff":
            rendered = self._handoff(features, decision, text, turn)
        else:
            rendered = render_clarification(decision)
        turn["reply_text"] = rendered["text"]
        turn["claims"] = rendered["claims"]
        turn["limitations"] = rendered["limitations"]
        if rendered["route"] == "human_escalation":
            turn["route"] = "human_escalation"
            turn["escalated"] = True
        turn["memory"] = clone_memory(self.memory)
        self.history.append(turn)
        return turn

    def _simple_skill(self, decision: dict, turn: dict) -> dict:
        result = self.sandbox.invoke(decision["skill_id"], decision.get("skill_args") or {})
        self._record_skill(turn, result)
        if decision["skill_id"] == "policy_lookup":
            return render_policy(result)
        if decision["skill_id"] == "product_quote" and result["status"] == "stale":
            turn["recovery"].append({"strategy": "retry", "because_code": "stale"})
            args = dict(decision.get("skill_args") or {})
            args["fresh"] = True
            result = self.sandbox.invoke(decision["skill_id"], args)
            self._record_skill(turn, result)
        if result["status"] == "conflict":
            turn["recovery"].append({"strategy": "escalate_human", "because_code": "conflict"})
            turn["escalated"] = True
            return render_escalation("conflicting_evidence")
        if result["status"] == "ok" and result.get("items"):
            return render_items(result["items"])
        if result["status"] == "stale":
            return render_escalation("stale_unresolved")
        return render_no_match()

    def _handoff(self, features, decision, text, turn) -> dict:
        self.task_counter += 1
        task_id = f"task-{self.session_id}-{self.task_counter}"
        turn["task_id"] = task_id
        turn["planner_invoked"] = True
        self.memory["active_task_id"] = task_id
        task = task_from_decision(features, decision, self.memory, task_id, text)
        try:
            outcome, state, plans = _run_planner(task, self.sandbox, self.plan_registry)
        finally:
            self.memory["active_task_id"] = None
        turn["plan"] = state.get("plan") or []
        for obs in state.get("observations") or []:
            self._record_skill(turn, obs, already_normalized=True)
        for plan_step in plans:
            if plan_step.get("recovery"):
                turn["recovery"].append(plan_step["recovery"])
        summary_status = "finished" if outcome["action"] == "finish" else "failed"
        self.memory["task_summaries"] = list(self.memory.get("task_summaries") or []) + [
            {"task_id": task_id, "status": summary_status, "summary": summary_status}
        ]
        if outcome["action"] == "finish":
            result = outcome["result"]
            lead = ""
            if result.get("requested_name"):
                lead = f"{result['requested_name']} is out of stock."
            return render_items(result.get("items") or [], result.get("limitations") or [], lead=lead)
        failure = outcome.get("failure") or {}
        if failure.get("escalate"):
            turn["escalated"] = True
            turn["route"] = "human_escalation"
            return render_escalation(failure.get("reason_code") or "conflicting_evidence")
        rendered = render_no_match()
        extra = failure.get("limitations") or []
        # Keep structured limitation codes from the planner.
        codes = {item["code"] for item in rendered["limitations"]}
        for item in extra:
            if item.get("code") not in codes:
                rendered["limitations"].append(item)
        return rendered

    def _record_skill(self, turn: dict, result: dict, already_normalized: bool = False) -> None:
        skill_id = result.get("skill_id")
        turn["skills"].append(
            {
                "skill_id": skill_id or result.get("skill_id"),
                "version": result.get("version"),
                "status": result.get("status"),
            }
        )
        for tool in result.get("tool_trace") or []:
            turn["tools"].append(tool)
        for evidence in result.get("evidence") or []:
            turn["evidence"].append(evidence)

    def _model_message(self, text: str) -> dict:
        client = self.model_clients["ca"]
        raw = client.complete(
            [
                {"role": "system", "content": "Return only a JSON object for the communication contract."},
                {"role": "user", "content": text},
            ]
        )
        turn = {
            "user_text": text,
            "route": "direct_answer",
            "route_reason_code": "configured_model",
            "planner_invoked": False,
            "task_id": None,
            "plan": [],
            "skills": [],
            "tools": [],
            "recovery": [],
            "claims": [],
            "limitations": [],
            "reply_text": raw,
            "escalated": False,
            "memory": clone_memory(self.memory),
            "schema_ok": False,
            "evaluation_kind": "configured_model",
        }
        self.history.append(turn)
        return turn

    def trace(self) -> dict:
        tool_calls = sum(len(turn["tools"]) for turn in self.history)
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "evaluation_kind": "mock_demo" if self.mode == "mock" else "configured_model",
            "turns": self.history,
            "task_ids": [turn["task_id"] for turn in self.history if turn.get("task_id")],
            "active_task_id_after": self.memory.get("active_task_id"),
            "memory": clone_memory(self.memory),
            "tool_call_count": tool_calls,
            "loop_count": _loop_count(self.history),
            "mcp_events": list(self.sandbox.process.client.events) if self.sandbox.process else [],
            "credentials_used": False,
            "pid": self.sandbox.pid(),
            "host_pid": os.getpid(),
        }

    def reset(self) -> None:
        self.sandbox.reset()
        self.memory = empty_memory()
        self.history = []
        self.task_counter = 0
        self.name_index = self.sandbox.read_name_index()


def _run_planner(task, sandbox, registry: PlanRegistry):
    state = new_planner_state()
    public_steps = []
    outcome = None
    for _ in range(8):
        outcome = act(task, state, registry)
        public_steps.append(
            {
                "action": outcome["action"],
                "plan": outcome.get("plan"),
                "skill_call": outcome.get("skill_call"),
                "recovery": outcome.get("recovery"),
                "failure": outcome.get("failure"),
            }
        )
        if outcome["action"] in {"finish", "fail", "ask_user"}:
            return outcome, state, public_steps
        call = outcome["skill_call"]
        if call["skill_id"] == "budget_attribute_validate":
            arguments = resolve_arguments(
                {
                    "skill_id": call["skill_id"],
                    "depends_on": _depends(state, call["step_id"]),
                    "arguments": call["arguments"],
                },
                state,
                task,
            )
        else:
            arguments = dict(call["arguments"])
        result = sandbox.invoke(call["skill_id"], arguments)
        state["observations"].append(
            {
                "step_id": call["step_id"],
                "skill_id": result.get("skill_id", call["skill_id"]),
                "version": result.get("version"),
                "status": result.get("status"),
                "items": result.get("items") or [],
                "facts": result.get("facts") or [],
                "evidence": result.get("evidence") or [],
                "arguments": arguments,
                "tool_trace": result.get("tool_trace") or [],
                "untrusted_text": result.get("untrusted_text") or [],
                "handled": False,
            }
        )
    return outcome, state, public_steps


def _depends(state, step_id: str) -> list[str]:
    for step in state.get("plan") or []:
        if step["step_id"] == step_id:
            return list(step.get("depends_on") or [])
    return []


def _loop_count(history: list[dict]) -> int:
    counts: dict[str, int] = {}
    loops = 0
    for turn in history:
        for tool in turn.get("tools") or []:
            key = json_key(tool.get("tool"), tool.get("arguments"))
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == 3:
                loops += 1
    return loops


def json_key(tool, arguments) -> str:
    import json

    return tool + ":" + json.dumps(arguments or {}, sort_keys=True)


def _reject_banned(node) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in BANNED_TRACE_KEYS:
                raise SchemaViolation(key)
            _reject_banned(value)
    elif isinstance(node, list):
        for item in node:
            _reject_banned(item)
