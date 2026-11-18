"""Multi-turn runtime. Tool facts come from the session sandbox."""

from __future__ import annotations

import os
import time

from shopassist.communication import prepare_turn, task_from_decision
from shopassist.config import ConfigError
from shopassist.loop_control import (
    Budget,
    classify_failure,
    partial_ids,
    stop_terminal,
    validate_finish,
)
from shopassist.model_runtime import (
    ModelContractError,
    apply_model_memory,
    ca_messages,
    pa_messages,
    parse_ca_decision,
    parse_json_object,
    parse_pa_decision,
)
from shopassist.constraints import clone_memory, empty_memory
from shopassist.context_engine import ContextEngine
from shopassist.planner_policy import PlanRegistry, act, new_planner_state, resolve_arguments
from shopassist.progress import CancelledTurn, milestone_for
from shopassist.reply_render import (
    render_clarification,
    render_direct,
    render_escalation,
    render_items,
    render_no_match,
    render_policy,
    render_stopped,
)
from shopassist.settings import load_settings
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
    settings: dict | None = None,
    clock=None,
    decider=None,
) -> dict:
    if mode not in {"mock", "configured_endpoint"}:
        raise ConfigError("unknown_mode", {"mode": mode, "mock_fallback": False})
    if mode == "configured_endpoint" and not model_clients:
        raise ConfigError(
            "configured_model_not_ready",
            {"missing": ["model_client"], "mock_fallback": False},
        )
    settings = settings or load_settings()
    own = sandbox is None
    box = sandbox or SessionSandbox.start(
        seed=seed,
        faults=faults or [],
        max_calls=settings["loop"]["max_tool_calls"],
        timeout=settings["mcp"]["tool_timeout_seconds"],
    )
    started = time.perf_counter()
    try:
        session = AgentSession(
            box,
            session_id=session_id,
            mode=mode,
            model_clients=model_clients,
            settings=settings,
            clock=clock,
            decider=decider,
        )
        for text in user_turns:
            session.user_message(text)
        trace = session.trace()
        trace["latency_ms"] = int((time.perf_counter() - started) * 1000)
        trace["token_usage"] = session.budget.tokens_measured or None
        trace["token_estimate"] = session.budget.tokens_estimated or None
        trace["token_budget"] = session.budget.token_budget
        trace["token_budget_enforced"] = session.budget.token_budget is not None
        if mode == "mock":
            trace["token_usage_reason"] = "unavailable_for_mock" if not session.budget.tokens_measured else "measured"
        else:
            usage = _usage_from_clients(model_clients)
            if usage and trace["token_usage"] is None:
                trace["token_usage"] = usage
            trace["token_usage_reason"] = None if trace["token_usage"] else "endpoint_did_not_report_usage"
            if trace["token_estimate"]:
                trace["token_usage_reason"] = trace["token_usage_reason"] or "estimated_when_usage_missing"
        trace["evaluation_kind"] = "mock_demo" if mode == "mock" else "configured_model"
        _reject_banned(trace)
        return trace
    finally:
        if own:
            box.close()


class AgentSession:
    def __init__(self, sandbox: SessionSandbox, session_id: str, mode: str = "mock", model_clients=None, plan_registry: PlanRegistry | None = None, context: ContextEngine | None = None, tenant_id: str = "local", user_id: str = "local-user", settings: dict | None = None, clock=None, decider=None):
        if mode not in {"mock", "configured_endpoint"}:
            raise ConfigError("unknown_mode", {"mode": mode})
        self.sandbox = sandbox
        self.session_id = session_id
        self.mode = mode
        self.model_clients = model_clients
        self.plan_registry = plan_registry or PlanRegistry()
        self.settings = settings or load_settings()
        self.budget = Budget(self.settings["loop"], clock=clock)
        self.sleeper = None
        self._rng = __import__("random").Random(self.settings["seed"])
        self.budget.recovery_settings = self.settings
        self.budget.rng = self._rng
        self.budget.sleeper = None
        self.decider = decider
        self.cancel_event = None
        self.generation = 0
        self.on_progress = None
        self._progress_generation = 0
        self.paused_task = None
        context_settings = self.settings["context"]
        self.context = context or ContextEngine(
            latest_messages=context_settings["latest_messages"],
            recent_days=context_settings["recent_memory_days"],
            summary_min_new_messages=context_settings["summary_min_new_messages"],
            summary_token_budget=context_settings["summary_token_budget"],
            sync_catchup_message_budget=context_settings["sync_catchup_messages"],
        )
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.memory = self.context.projected_memory(tenant_id, session_id)
        self.history: list[dict] = []
        self.task_counter = 0
        self.name_index = sandbox.read_name_index()
        self._credentials_used = False
        if model_clients:
            self._credentials_used = any(bool(getattr(client, "credential_present", False)) for client in model_clients.values())
        if mode == "configured_endpoint" and not model_clients:
            raise ConfigError("configured_model_not_ready", {"missing": ["model_client"], "mock_fallback": False})

    def user_message(self, text: str, on_progress=None) -> dict:
        self.on_progress = on_progress
        self._progress_generation = self.generation
        try:
            self._raise_if_cancelled()
            self.budget.begin_request()
            self.budget.sleeper = self.sleeper
            self.budget.cancel_event = self.cancel_event
            self.budget.recovery_settings = self.settings
            self.budget.rng = self._rng
            if self.mode == "configured_endpoint":
                return self._model_message(text)
            return self._mock_message(text)
        finally:
            self.on_progress = None

    def _mock_message(self, text: str) -> dict:
        memory_before = clone_memory(self.memory)
        features, decision, memory = prepare_turn(text, self.memory, self.name_index)
        self.memory = memory
        if self.paused_task and self._should_supersede(decision):
            self._cancel_paused()
        elif self.paused_task and self._can_resume(features):
            turn = self._blank_turn(text, memory_before, decision)
            rendered = self._resume(features, decision, text, turn)
            return self._commit(text, turn, rendered)
        turn = {
            "memory_before": memory_before,
            "user_text": text,
            "route": decision["route"],
            "route_reason_code": decision["route_reason_code"],
            "planner_invoked": False,
            "task_id": None,
            "task_created": False,
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
        return self._commit(text, turn, rendered)

    def _commit(self, text: str, turn: dict, rendered: dict) -> dict:
        self._raise_if_cancelled()
        turn["reply_text"] = rendered["text"]
        turn["claims"] = rendered["claims"]
        turn["limitations"] = rendered["limitations"]
        if rendered["route"] == "human_escalation":
            turn["route"] = "human_escalation"
            turn["escalated"] = True
        if "terminal" not in turn:
            turn["terminal"] = _terminal_for_route(turn["route"], turn.get("escalated"))
        from shopassist.evidence_gate import gate_turn

        gated = gate_turn(turn)
        turn["claims"] = gated["claims"]
        turn["limitations"] = gated["limitations"]
        turn["reply_text"] = gated["reply_text"]
        turn["evidence_records"] = gated["records"]
        turn["memory"] = clone_memory(self.memory)
        self._publish_context(text, turn)
        self.history.append(turn)
        return turn

    def _blank_turn(self, text, memory_before, decision) -> dict:
        return {
            "memory_before": memory_before,
            "user_text": text,
            "route": decision["route"],
            "route_reason_code": decision["route_reason_code"],
            "planner_invoked": False,
            "task_id": None,
            "task_created": False,
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

    def _should_supersede(self, decision: dict) -> bool:
        return decision["route"] in {"direct_answer", "human_escalation", "simple_skill"}

    def _can_resume(self, features: dict) -> bool:
        missing = set((self.paused_task or {}).get("missing") or [])
        if not missing:
            return False
        confirmed = {item["field"] for item in self.memory.get("confirmed_constraints") or []}
        if "color" in missing and features.get("colors"):
            confirmed.add("color")
        if "budget" in missing and features.get("budgets"):
            confirmed.add("budget")
        if "category" in missing and features.get("categories"):
            confirmed.add("category")
        if "product" in missing and features.get("product_ids"):
            confirmed.add("product")
        return missing.issubset(confirmed)

    def _cancel_paused(self) -> None:
        self.paused_task = None
        self.memory["active_task_id"] = None

    def _resume(self, features, decision, text, turn) -> dict:
        paused = self.paused_task
        self.paused_task = None
        task = task_from_decision(features, decision, self.memory, paused["task_id"], text)
        task["task_id"] = paused["task_id"]
        turn["task_id"] = paused["task_id"]
        turn["planner_invoked"] = True
        turn["resumed"] = True
        return self._handoff(features, decision, text, turn, task=task, resume_state=paused["state"])

    def _raise_if_cancelled(self) -> None:
        event = self.cancel_event
        if event is not None and event.is_set():
            self.memory["active_task_id"] = None
            raise CancelledTurn()

    def _emit(self, skill_id: str) -> None:
        if self.on_progress is None or self._progress_generation != self.generation:
            return
        event = self.cancel_event
        if event is not None and event.is_set():
            return
        milestone = milestone_for(skill_id)
        if milestone is not None:
            self.on_progress(milestone)

    def _publish_context(self, text: str, turn: dict) -> None:
        if self.context is None:
            return
        assembled = self.context.record_exchange(
            self.tenant_id,
            self.user_id,
            self.session_id,
            text,
            turn.get("reply_text") or "",
            self.memory,
        )
        turn["context_view"] = self.context.public_view(assembled)

    def _simple_skill(self, decision: dict, turn: dict) -> dict:
        from shopassist.recovery import choose

        args = dict(decision.get("skill_args") or {})
        attempt = 0
        fallback_count = 0
        result = None
        while True:
            result = self._invoke_limited(decision["skill_id"], args, turn)
            if result is None:
                return render_stopped((turn.get("terminal") or {}).get("reason_code") or "stopped")
            decision_recovery = choose(
                status=result.get("status"),
                error_code=(result.get("data") or {}).get("error_code") if isinstance(result.get("data"), dict) else None,
                skill_id=decision["skill_id"],
                attempt=attempt,
                fallback_count=fallback_count,
                limits=self._loop_limits(),
                idempotent=True,
                partial_streamed=self.budget.partial_streamed,
            )
            if decision_recovery["action"] == "retry":
                turn["recovery"].append({"strategy": "retry", "because_code": decision_recovery["reason"]})
                reason = self._wait_retry(attempt, None)
                if reason:
                    turn["terminal"] = stop_terminal(reason)
                    return render_stopped(reason)
                attempt += 1
                continue
            if decision_recovery["action"] == "refresh":
                turn["recovery"].append({"strategy": "retry", "because_code": "stale"})
                args["fresh"] = True
                attempt += 1
                continue
            if decision_recovery["action"] == "cached_fallback":
                turn["recovery"].append({"strategy": "fallback", "because_code": "error"})
                args["backend"] = "cached"
                fallback_count += 1
                continue
            break
        if decision["skill_id"] == "policy_lookup":
            if result.get("status") == "ok":
                turn["terminal"] = {"status": "COMPLETED", "reason_code": "validated_completion"}
            elif result.get("status") == "empty":
                turn["terminal"] = {"status": "COMPLETED_NO_MATCH", "reason_code": "no_match"}
            elif result.get("status") == "conflict":
                turn["terminal"] = {"status": "COMPLETED", "reason_code": "conflicting_evidence"}
            else:
                turn["terminal"] = {"status": "FAILED", "reason_code": result.get("status") or "tool_error"}
            return render_policy(result)
        if decision["skill_id"] == "shipment_lookup":
            return self._render_shipment(result, turn)
        if result["status"] == "conflict":
            turn["recovery"].append({"strategy": "escalate_human", "because_code": "conflict"})
            turn["escalated"] = True
            turn["terminal"] = {"status": "ESCALATED", "reason_code": "conflicting_evidence"}
            return render_escalation("conflicting_evidence")
        if result["status"] == "ok" and result.get("items"):
            turn["terminal"] = {"status": "COMPLETED", "reason_code": "validated_completion"}
            return render_items(result["items"])
        if result["status"] == "stale":
            turn["terminal"] = {"status": "FAILED", "reason_code": "stale_unresolved"}
            return render_escalation("stale_unresolved")
        if result["status"] in {"timeout", "error", "malformed"}:
            reason = {"timeout": "tool_timeout", "error": "tool_error", "malformed": "malformed_evidence"}[result["status"]]
            turn["terminal"] = {"status": "FAILED", "reason_code": reason}
            rendered = render_no_match()
            rendered["limitations"].append({"code": reason})
            return rendered
        if result.get("status") == "empty" and decision["skill_id"] == "shipment_lookup":
            return self._render_shipment(result, turn)
        turn["terminal"] = {"status": "COMPLETED_NO_MATCH", "reason_code": "no_match"}
        return render_no_match()

    def _render_shipment(self, result: dict, turn: dict) -> dict:
        status = result.get("status")
        if status == "ok" and result.get("facts"):
            turn["terminal"] = {"status": "COMPLETED", "reason_code": "validated_completion"}
            turn["facts"] = result["facts"]
            text_bits = []
            for fact in result["facts"]:
                if fact["field"] == "logistics_status":
                    text_bits.append(f"Tracking {fact.get('tracking_id')} is {fact['value']}.")
            rendered = {
                "text": " ".join(text_bits) or "Logistics status is unavailable for this request.",
                "claims": result["facts"],
                "limitations": [],
                "route": "simple_skill",
                "route_reason_code": "shipment_lookup",
            }
            return rendered
        if status == "empty":
            turn["terminal"] = {"status": "COMPLETED_NO_MATCH", "reason_code": "logistics_not_in_snapshot"}
            return {
                "text": "The logistics snapshot has no record for that id.",
                "claims": [],
                "limitations": [{"code": "logistics_not_in_snapshot"}],
                "route": "simple_skill",
                "route_reason_code": "shipment_lookup",
            }
        turn["terminal"] = {"status": "FAILED", "reason_code": "logistics_unavailable"}
        return {
            "text": "Logistics status is unavailable for this request.",
            "claims": [],
            "limitations": [{"code": "logistics_unavailable"}],
            "route": "simple_skill",
            "route_reason_code": "shipment_lookup",
        }

    def _complete_model(self, client, messages, timeout: float | None):
        from shopassist.config import ConfigError
        from shopassist.recovery import PermanentEndpointError, TransientEndpointError, choose

        attempt = 0
        while True:
            if timeout is None:
                return None
            try:
                return client.complete(messages, timeout=timeout)
            except TransientEndpointError as exc:
                decision = choose(
                    status=None,
                    http_status=exc.http_status,
                    attempt=attempt,
                    limits=self._loop_limits(),
                    idempotent=True,
                    partial_streamed=self.budget.partial_streamed,
                )
                if decision["action"] != "retry":
                    raise ConfigError("endpoint_transient_exhausted", {"http_status": exc.http_status, "mock_fallback": False})
                reason = self._wait_retry(attempt, exc.retry_after)
                if reason:
                    return None
                attempt += 1
                timeout = self.budget.call_timeout(self.settings["model"]["ca"]["timeout_seconds"])
            except PermanentEndpointError as exc:
                raise ConfigError("endpoint_permanent", {"http_status": exc.http_status, "mock_fallback": False})

    def _wait_retry(self, attempt: int, retry_after) -> str | None:
        from shopassist.recovery import delay_seconds, wait_bounded

        seconds = delay_seconds(attempt, retry_after, self.settings, self._rng)
        return wait_bounded(seconds, self.budget, self.cancel_event, self.sleeper)

    def _invoke_limited(self, skill_id: str, arguments: dict, turn: dict):
        self._raise_if_cancelled()
        spec = None
        try:
            spec = self.sandbox.registry.get(skill_id)
        except Exception:
            spec = None
        uses_tools = bool(spec and spec.allowed_tools)
        idempotent = True if spec is None else bool(spec.idempotent)
        blocked = self.budget.repeat_block(skill_id, arguments, idempotent=idempotent)
        if blocked:
            turn["terminal"] = stop_terminal(blocked)
            return None
        reason = self.budget.before_tool_call(uses_tools=uses_tools)
        if reason:
            turn["terminal"] = stop_terminal(reason)
            return None
        timeout = self.budget.call_timeout(self.settings["mcp"]["tool_timeout_seconds"])
        if timeout is None:
            turn["terminal"] = stop_terminal("wall_clock")
            return None
        self._emit(skill_id)
        try:
            result = self.sandbox.invoke(skill_id, arguments, timeout=timeout)
        except TimeoutError:
            result = {
                "skill_id": skill_id,
                "status": "timeout",
                "items": [],
                "facts": [],
                "evidence": [],
                "tool_trace": [{"tool": skill_id, "status": "timeout", "arguments": arguments}],
                "untrusted_text": [],
            }
        self.budget.note_tool_result(skill_id, arguments, result)
        self._record_skill(turn, result)
        self._raise_if_cancelled()
        return result

    def _handoff(self, features, decision, text, turn, task: dict | None = None, decide=None, resume_state=None) -> dict:
        if resume_state is None:
            self.task_counter += 1
            task_id = f"task-{self.session_id}-{self.task_counter}"
            turn["task_created"] = True
            self.budget.begin_task()
        else:
            task_id = task["task_id"]
            turn["task_created"] = False
        turn["task_id"] = task_id
        turn["planner_invoked"] = True
        self.memory["active_task_id"] = task_id
        if task is None:
            task = task_from_decision(features, decision, self.memory, task_id, text)
        else:
            task["task_id"] = task_id
        self.budget.charge_model_calls = self.mode == "configured_endpoint" and self.decider is None
        try:
            outcome, state, plans = _run_planner(
                task,
                self.sandbox,
                self.plan_registry,
                decide=self._decide if decide is None else decide,
                budget=self.budget,
                limits=self._loop_limits(),
                initial_state=resume_state,
                emit=self._emit,
                cancelled=self._raise_if_cancelled,
                tool_timeout=self.settings["mcp"]["tool_timeout_seconds"],
            )
        finally:
            self.memory["active_task_id"] = None
        terminal = outcome.get("terminal") or stop_terminal("step_budget")
        if terminal.get("reason_code") == "cancelled":
            raise CancelledTurn()
        turn["plan"] = state.get("plan") or []
        turn["terminal"] = terminal
        for obs in state.get("observations") or []:
            if obs.get("recorded"):
                continue
            self._record_skill(turn, obs, already_normalized=True)
            obs["recorded"] = True
        for plan_step in plans:
            if plan_step.get("recovery"):
                turn["recovery"].append(plan_step["recovery"])
        status = terminal.get("status")
        summary_status = "finished" if status in {"COMPLETED", "COMPLETED_NO_MATCH"} else "failed"
        if status != "WAITING_USER":
            self.memory["task_summaries"] = list(self.memory.get("task_summaries") or []) + [
                {"task_id": task_id, "status": summary_status, "summary": summary_status}
            ]
            self.paused_task = None
        else:
            self.paused_task = {
                "task_id": task_id,
                "state": state,
                "missing": list((outcome.get("ask") or {}).get("missing") or terminal.get("missing") or ["request"]),
            }
            turn["route"] = "clarification"
            return render_clarification(
                {"missing": self.paused_task["missing"], "route_reason_code": "ask_user"}
            )
        if status == "COMPLETED":
            result = outcome.get("result") or {}
            lead = ""
            if result.get("requested_name"):
                lead = f"{result['requested_name']} is out of stock."
            return render_items(result.get("items") or [], result.get("limitations") or [], lead=lead)
        if status == "COMPLETED_NO_MATCH":
            return render_no_match()
        if status == "ESCALATED":
            turn["escalated"] = True
            turn["route"] = "human_escalation"
            return render_escalation((outcome.get("failure") or {}).get("reason_code") or "conflicting_evidence")
        if status == "STOPPED":
            return render_stopped(terminal.get("reason_code") or "stopped")
        rendered = render_no_match()
        code = terminal.get("reason_code")
        if code and code != "no_match":
            rendered["limitations"].append({"code": code})
        return rendered

    def _loop_limits(self) -> dict:
        loop = self.settings["loop"]
        return {
            "max_retries_per_step": loop["max_retries_per_step"],
            "max_replans": loop["max_replans"],
            "max_fallbacks": loop["max_fallbacks"],
        }

    def _decide(self, task: dict, state: dict) -> dict:
        if self.decider is not None:
            return self.decider(task, state, self.plan_registry)
        if self.mode == "configured_endpoint":
            return self._pa_decide(task, state)
        return act(task, state, self.plan_registry)

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
        if "pa" not in (self.model_clients or {}) or "ca" not in (self.model_clients or {}):
            raise ConfigError("configured_model_not_ready", {"missing": ["ca", "pa"], "mock_fallback": False})
        prior = self.context.public_view(self.context.assemble(self.tenant_id, self.user_id, self.session_id))
        messages = ca_messages(text, self.memory, self.name_index, prior)
        timeout = self.budget.call_timeout(self.settings["model"]["ca"]["timeout_seconds"])
        if timeout is None:
            raise CancelledTurn()
        raw = self._complete_model(self.model_clients["ca"], messages, timeout)
        if raw is None:
            memory_before = clone_memory(self.memory)
            turn = self._blank_turn(text, memory_before, {"route": "direct_answer", "route_reason_code": "wall_clock"})
            turn["terminal"] = stop_terminal("wall_clock")
            turn["evaluation_kind"] = "configured_model"
            return self._commit(text, turn, render_stopped("wall_clock"))
        token_stop = self.budget.after_model_call(getattr(self.model_clients["ca"], "last_usage", None), len(raw))
        if token_stop:
            memory_before = clone_memory(self.memory)
            turn = self._blank_turn(text, memory_before, {"route": "direct_answer", "route_reason_code": token_stop})
            turn["terminal"] = stop_terminal(token_stop)
            turn["evaluation_kind"] = "configured_model"
            return self._commit(text, turn, render_stopped(token_stop))
        try:
            decision = parse_ca_decision(parse_json_object(raw))
        except ModelContractError:
            raise
        except ConfigError:
            raise
        memory_before = clone_memory(self.memory)
        self.memory = apply_model_memory(self.memory, decision)
        turn = {
            "memory_before": memory_before,
            "user_text": text,
            "route": decision["route"],
            "route_reason_code": decision["route_reason_code"],
            "planner_invoked": False,
            "task_id": None,
            "task_created": False,
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
            "evaluation_kind": "configured_model",
        }
        if self.paused_task and decision["route"] in {"direct_answer", "human_escalation", "simple_skill"}:
            self._cancel_paused()
        if decision["route"] == "direct_answer":
            rendered = {
                "text": decision["reply_text"],
                "claims": [],
                "limitations": [],
                "route": "direct_answer",
                "route_reason_code": decision["route_reason_code"],
            }
        elif decision["route"] == "clarification":
            rendered = {
                "text": decision["reply_text"],
                "claims": [],
                "limitations": [],
                "route": "clarification",
                "route_reason_code": decision["route_reason_code"],
            }
        elif decision["route"] == "human_escalation":
            rendered = render_escalation(decision["route_reason_code"])
            turn["escalated"] = True
        elif decision["route"] == "simple_skill":
            rendered = self._simple_skill(decision, turn)
        elif decision["route"] == "complex_handoff":
            if self.paused_task:
                rendered = self._resume_model(decision, text, turn)
            else:
                task = {
                    "goal_type": decision["goal_type"],
                    "confirmed_constraints": list(self.memory.get("confirmed_constraints") or []),
                    "assumptions": list(self.memory.get("assumptions") or []),
                    "missing": [],
                    "query": decision.get("query") or {},
                    "user_text": text,
                }
                rendered = self._handoff(None, decision, text, turn, task=task)
        else:
            raise ModelContractError("model_route_rejected", {"route": decision["route"]})
        return self._commit(text, turn, rendered)

    def _resume_model(self, decision, text, turn) -> dict:
        paused = self.paused_task
        self.paused_task = None
        task = {
            "goal_type": decision.get("goal_type"),
            "confirmed_constraints": list(self.memory.get("confirmed_constraints") or []),
            "assumptions": list(self.memory.get("assumptions") or []),
            "missing": [],
            "query": decision.get("query") or {},
            "user_text": text,
            "task_id": paused["task_id"],
        }
        turn["task_id"] = paused["task_id"]
        turn["planner_invoked"] = True
        turn["resumed"] = True
        return self._handoff(None, decision, text, turn, task=task, resume_state=paused["state"])

    def _pa_decide(self, task: dict, state: dict) -> dict:
        self._raise_if_cancelled()
        messages = pa_messages(task, state)
        timeout = self.budget.call_timeout(self.settings["model"]["pa"]["timeout_seconds"])
        if timeout is None:
            return _stopped_outcome("wall_clock")
        raw = self._complete_model(self.model_clients["pa"], messages, timeout)
        if raw is None:
            return _stopped_outcome("wall_clock")
        outcome = parse_pa_decision(parse_json_object(raw))
        if outcome.get("plan"):
            state["plan"] = outcome["plan"]
        reason = self.budget.after_model_call(getattr(self.model_clients["pa"], "last_usage", None), len(raw))
        if reason:
            return _stopped_outcome(reason)
        return outcome

    def trace(self) -> dict:
        tool_calls = sum(len(turn["tools"]) for turn in self.history)
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "evaluation_kind": "mock_demo" if self.mode == "mock" else "configured_model",
            "turns": self.history,
            "task_ids": [turn["task_id"] for turn in self.history if turn.get("task_created")],
            "active_task_id_after": self.memory.get("active_task_id"),
            "memory": clone_memory(self.memory),
            "tool_call_count": tool_calls,
            "loop_count": _loop_count(self.history),
            "mcp_events": list(self.sandbox.process.client.events) if self.sandbox.process else [],
            "credentials_used": bool(getattr(self, "_credentials_used", False)),
            "model_call_count": _model_calls(self.model_clients),
            "pid": self.sandbox.pid(),
            "host_pid": os.getpid(),
        }

    def reset(self) -> None:
        self.sandbox.reset()
        if self.context is not None:
            self.context.reset_session(self.tenant_id, self.session_id)
        self.memory = empty_memory()
        self.history = []
        self.task_counter = 0
        self.paused_task = None
        self.budget = Budget(self.settings["loop"], clock=self.budget.clock)
        self.name_index = self.sandbox.read_name_index()


def _run_planner(task, sandbox, registry: PlanRegistry, decide, budget: Budget, limits: dict, initial_state=None, emit=None, cancelled=None, tool_timeout=2.0):
    state = initial_state or new_planner_state()
    state["limits"] = limits
    public_steps = []
    outcome = None
    while True:
        if cancelled:
            try:
                cancelled()
            except CancelledTurn:
                return _stopped_outcome("cancelled"), state, public_steps
        reason = budget.before_plan_step(will_call_model=bool(getattr(budget, "charge_model_calls", False)))
        if reason:
            stopped = _stopped_outcome(reason, partial_ids(state))
            return stopped, state, public_steps
        budget.note_plan_step()
        outcome = decide(task, state)
        public_steps.append(
            {
                "action": outcome.get("action"),
                "skill_call": _public_call(outcome.get("skill_call")),
                "recovery": outcome.get("recovery"),
                "failure": outcome.get("failure"),
            }
        )
        action = outcome.get("action")
        if action == "finish":
            verdict = validate_finish(task, state, outcome)
            outcome["terminal"] = verdict["terminal"]
            outcome["result"] = verdict["result"]
            outcome["failure"] = verdict["failure"]
            return outcome, state, public_steps
        if action == "ask_user":
            missing = list((outcome.get("ask") or {}).get("missing") or ["request"])
            outcome["terminal"] = {"status": "WAITING_USER", "reason_code": "ask_user", "missing": missing}
            return outcome, state, public_steps
        if action == "fail":
            outcome["terminal"] = classify_failure(outcome, state)
            return outcome, state, public_steps
        call = outcome.get("skill_call") or {}
        if not call.get("skill_id"):
            outcome["terminal"] = {"status": "FAILED", "reason_code": "missing_skill_call"}
            return outcome, state, public_steps
        if call["skill_id"] == "budget_attribute_validate":
            arguments = resolve_arguments(
                {
                    "skill_id": call["skill_id"],
                    "depends_on": _depends(state, call["step_id"]),
                    "arguments": call.get("arguments") or {},
                },
                state,
                task,
            )
        else:
            arguments = dict(call.get("arguments") or {})
        spec = _spec(sandbox, call["skill_id"])
        idempotent = True if spec is None else bool(getattr(spec, "idempotent", True))
        uses_tools = bool(spec and getattr(spec, "allowed_tools", ()))
        blocked = budget.repeat_block(call["skill_id"], arguments, idempotent=idempotent)
        if blocked:
            return _stopped_outcome(blocked, partial_ids(state)), state, public_steps
        tool_reason = budget.before_tool_call(uses_tools=uses_tools)
        if tool_reason:
            return _stopped_outcome(tool_reason, partial_ids(state)), state, public_steps
        timeout = budget.call_timeout(tool_timeout)
        if timeout is None:
            return _stopped_outcome("wall_clock", partial_ids(state)), state, public_steps
        if action == "retry":
            from shopassist.recovery import delay_seconds, wait_bounded

            settings = getattr(budget, "recovery_settings", None)
            if settings is not None:
                seconds = delay_seconds(0, None, settings, getattr(budget, "rng", None) or __import__("random").Random(0))
                waited = wait_bounded(seconds, budget, getattr(budget, "cancel_event", None), getattr(budget, "sleeper", None))
                if waited:
                    return _stopped_outcome(waited, partial_ids(state)), state, public_steps
        if emit:
            emit(call["skill_id"])
        try:
            result = sandbox.invoke(call["skill_id"], arguments, timeout=timeout)
        except TimeoutError:
            result = {"skill_id": call["skill_id"], "status": "timeout", "items": [], "facts": [], "evidence": [], "tool_trace": [], "untrusted_text": []}
        budget.note_tool_result(call["skill_id"], arguments, result)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        state["observations"].append(
            {
                "step_id": call.get("step_id"),
                "skill_id": result.get("skill_id", call["skill_id"]),
                "version": result.get("version"),
                "status": result.get("status"),
                "error_code": data.get("error_code"),
                "items": result.get("items") or [],
                "facts": result.get("facts") or [],
                "evidence": result.get("evidence") or [],
                "arguments": arguments,
                "tool_trace": result.get("tool_trace") or [],
                "untrusted_text": result.get("untrusted_text") or [],
                "handled": False,
                "recorded": False,
            }
        )


def _stopped_outcome(reason: str, partial=None) -> dict:
    return {
        "action": "stop",
        "result": None,
        "failure": {"reason_code": reason, "escalate": False, "limitations": [{"code": reason}]},
        "ask": None,
        "skill_call": None,
        "recovery": None,
        "terminal": stop_terminal(reason, partial_ids=partial),
    }


def _public_call(call):
    if not isinstance(call, dict):
        return None
    return {"step_id": call.get("step_id"), "skill_id": call.get("skill_id")}


def _spec(sandbox, skill_id: str):
    registry = getattr(sandbox, "registry", None)
    if registry is None:
        return None
    try:
        return registry.get(skill_id)
    except Exception:
        return None


def _terminal_for_route(route: str, escalated: bool) -> dict:
    if escalated or route == "human_escalation":
        return {"status": "ESCALATED", "reason_code": "user_requested_person"}
    if route == "clarification":
        return {"status": "WAITING_USER", "reason_code": "missing_information"}
    if route == "direct_answer":
        return {"status": "COMPLETED", "reason_code": "direct_answer"}
    return {"status": "COMPLETED", "reason_code": "validated_completion"}


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


def _usage_from_clients(model_clients: dict | None):
    if not model_clients:
        return None
    found = []
    for client in model_clients.values():
        usage = getattr(client, "last_usage", None)
        if usage:
            found.append(usage)
    return found or None


def _model_calls(model_clients: dict | None) -> int:
    if not model_clients:
        return 0
    return sum(int(getattr(client, "completed_calls", 0) or 0) for client in model_clients.values())


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
