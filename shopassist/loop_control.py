"""Bounded task execution. Completion is decided here, not by a model flag."""

from __future__ import annotations

import json
import time

COMPLETED = "COMPLETED"
COMPLETED_NO_MATCH = "COMPLETED_NO_MATCH"
WAITING_USER = "WAITING_USER"
FAILED = "FAILED"
STOPPED = "STOPPED"
ESCALATED = "ESCALATED"

_FAULTS = {"timeout", "error", "malformed"}
_CLEAN = {"ok", "empty"}


class Clock:
    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    def __init__(self, now: float = 0.0):
        self._now = float(now)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class Budget:
    def __init__(self, loop: dict, clock=None):
        self.max_plan_steps = int(loop["max_plan_steps"])
        self.max_tool_calls = int(loop["max_tool_calls"])
        self.max_model_calls = int(loop["max_model_calls"])
        self.max_retries_per_step = int(loop["max_retries_per_step"])
        self.repeat_limit = int(loop["repeat_limit"])
        self.wall_clock_seconds = float(loop["wall_clock_seconds"])
        self.token_budget = None if loop.get("token_budget") is None else int(loop["token_budget"])
        self.missing_usage_tokens = int(loop["missing_usage_tokens"])
        self.chars_per_token = int(loop["chars_per_token"])
        self.clock = clock or Clock()
        self.started = self.clock.now()
        self.deadline = self.started + self.wall_clock_seconds
        self.plan_steps = 0
        self.tool_calls = 0
        self.model_calls = 0
        self.tokens_measured = 0
        self.tokens_estimated = 0
        self._signatures: dict[str, tuple[int, str]] = {}
        self.charge_model_calls = False

    def limits(self) -> dict:
        return {
            "max_retries_per_step": self.max_retries_per_step,
            "max_replans": None,
            "max_fallbacks": None,
        }

    def before_plan_step(self, *, will_call_model: bool) -> str | None:
        reason = self._hard_stop()
        if reason:
            return reason
        if self.plan_steps >= self.max_plan_steps:
            return "step_budget"
        if will_call_model and self.model_calls >= self.max_model_calls:
            return "model_budget"
        if not will_call_model and not self.charge_model_calls:
            return self._reserve_estimate()
        return None

    def after_model_call(self, usage, text_len: int) -> str | None:
        self.model_calls += 1
        measured = None
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            measured = int(usage["total_tokens"])
        if measured is not None:
            self.tokens_measured += measured
        elif self.token_budget is not None:
            self.tokens_estimated += self._estimate(text_len)
        return self._hard_stop()

    def before_tool_call(self, *, uses_tools: bool) -> str | None:
        reason = self._hard_stop()
        if reason:
            return reason
        if uses_tools and self.tool_calls >= self.max_tool_calls:
            return "tool_budget"
        return None

    def call_timeout(self, configured: float) -> float | None:
        remaining = self.deadline - self.clock.now()
        if remaining <= 0:
            return None
        return min(float(configured), remaining)

    def repeat_block(self, skill_id: str, arguments: dict, *, idempotent: bool) -> str | None:
        signature = _signature(skill_id, arguments)
        previous = self._signatures.get(signature)
        if previous is None:
            return None
        if not idempotent:
            return "side_effect_not_retried"
        count, status = previous
        if status in _CLEAN:
            return "no_progress"
        if count > self.repeat_limit:
            return "no_progress"
        return None

    def note_tool_result(self, skill_id: str, arguments: dict, result: dict) -> None:
        status = str(result.get("status") or "error")
        signature = _signature(skill_id, arguments)
        count = self._signatures.get(signature, (0, ""))[0] + 1
        self._signatures[signature] = (count, status)
        self.tool_calls += len(result.get("tool_trace") or [])

    def begin_task(self) -> None:
        self.plan_steps = 0

    def _reserve_estimate(self) -> str | None:
        if self.token_budget is None:
            return None
        if self.tokens_used() + self.missing_usage_tokens > self.token_budget:
            return "token_budget"
        self.tokens_estimated += self.missing_usage_tokens
        return None

    def _estimate(self, text_len: int) -> int:
        rough = int(text_len) // self.chars_per_token
        return max(self.missing_usage_tokens, rough)

    def _hard_stop(self) -> str | None:
        if self.clock.now() >= self.deadline:
            return "wall_clock"
        if self.token_budget is not None and self.tokens_used() > self.token_budget:
            return "token_budget"
        return None

    def tokens_used(self) -> int:
        return self.tokens_measured + self.tokens_estimated


def stop_terminal(reason: str, *, partial_ids: list[str] | None = None) -> dict:
    return {
        "status": STOPPED,
        "reason_code": reason,
        "partial_product_ids": list(partial_ids or []),
    }


def validate_finish(task: dict, state: dict, proposal: dict) -> dict:
    """Accept a finish only when current evidence satisfies the task."""
    items, requested = grounded_items(task, state)
    proposed = _proposed_items(proposal)
    if proposed is not None:
        grounded_ids = {item.get("product_id") for item in items}
        if any(item.get("product_id") not in grounded_ids for item in proposed):
            return _verdict(FAILED, "ungrounded_finish", failure="ungrounded_finish")
        items = proposed
    violation = _constraint_violation(task, items)
    if violation:
        return _verdict(FAILED, "constraint_violation", failure="constraint_violation")
    if not items:
        blocked = _blocking_reason(state)
        if blocked == "budget_exhausted":
            return _verdict(STOPPED, "budget_exhausted")
        if blocked == "conflicting_evidence":
            return _verdict(ESCALATED, "conflicting_evidence", failure="conflicting_evidence", escalate=True)
        if blocked:
            return _verdict(FAILED, blocked, failure=blocked)
        return _verdict(COMPLETED_NO_MATCH, "no_match", items=[], limitations=["no_match"])
    limitations = []
    requested_name = None
    if task.get("goal_type") == "oos":
        limitations.append("requested_out_of_stock")
        requested_name = None if requested is None else requested.get("name")
    return _verdict(COMPLETED, "validated_completion", items=items, limitations=limitations, requested_name=requested_name)


def classify_failure(outcome: dict, state: dict) -> dict:
    failure = outcome.get("failure") or {}
    reason = str(failure.get("reason_code") or "tool_failure")
    blocked = _blocking_reason(state)
    if blocked == "budget_exhausted":
        return stop_terminal("budget_exhausted")
    if failure.get("escalate") or reason == "conflicting_evidence" or blocked == "conflicting_evidence":
        return {
            "status": ESCALATED,
            "reason_code": "conflicting_evidence",
            "partial_product_ids": [],
        }
    if reason == "no_match" and not blocked:
        return {"status": COMPLETED_NO_MATCH, "reason_code": "no_match", "partial_product_ids": []}
    if blocked:
        reason = blocked
    return {"status": FAILED, "reason_code": reason, "partial_product_ids": partial_ids(state)}


def grounded_items(task: dict, state: dict) -> tuple[list[dict], dict | None]:
    requested = None
    items: list[dict] = []
    for obs in state.get("observations") or []:
        if obs.get("skill_id") == "product_quote" and obs.get("items"):
            requested = obs["items"][0]
        if obs.get("status") != "ok":
            continue
        if obs.get("skill_id") == "product_quote" and task.get("goal_type") == "oos":
            continue
        items = [dict(item) for item in obs.get("items") or [] if isinstance(item, dict)]
    if task.get("goal_type") == "oos":
        requested_id = None if requested is None else requested.get("product_id")
        items = [item for item in items if item.get("product_id") != requested_id]
    return items, requested


def partial_ids(state: dict) -> list[str]:
    found = []
    for obs in state.get("observations") or []:
        if obs.get("status") != "ok":
            continue
        for item in obs.get("items") or []:
            product_id = item.get("product_id")
            if product_id and product_id not in found:
                found.append(product_id)
    return found


def _proposed_items(proposal: dict):
    result = proposal.get("result")
    if not isinstance(result, dict) or "items" not in result:
        return None
    items = result.get("items") or []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _constraint_violation(task: dict, items: list[dict]) -> bool:
    query = task.get("query") or {}
    max_price = query.get("max_price")
    for item in items:
        if max_price is not None and item.get("price") is not None:
            try:
                if int(item["price"]) >= int(max_price):
                    return True
            except (TypeError, ValueError):
                return True
        for field in ("category", "color", "size"):
            expected = query.get(field)
            actual = item.get(field)
            if expected and actual and actual != expected:
                return True
    return False


def _blocking_reason(state: dict) -> str | None:
    saw_clean = False
    blocked = None
    for obs in state.get("observations") or []:
        error_code = obs.get("error_code")
        if error_code == "budget_exhausted":
            blocked = "budget_exhausted"
        elif obs.get("status") == "conflict":
            blocked = "conflicting_evidence"
        elif obs.get("status") in _FAULTS:
            blocked = {
                "timeout": "tool_timeout",
                "error": "tool_error",
                "malformed": "malformed_evidence",
            }[obs["status"]]
        elif obs.get("status") in _CLEAN:
            saw_clean = True
            blocked = None
    if saw_clean:
        return None
    return blocked


def _verdict(status: str, reason: str, *, items=None, limitations=None, failure=None, escalate=False, requested_name=None) -> dict:
    terminal = {"status": status, "reason_code": reason, "partial_product_ids": []}
    result = None
    if status in {COMPLETED, COMPLETED_NO_MATCH}:
        result = {
            "items": list(items or []),
            "limitations": [{"code": code} for code in (limitations or [])],
            "summary": "finished" if status == COMPLETED else "no_match",
            "requested_name": requested_name,
        }
    failure_body = None
    if failure:
        failure_body = {"reason_code": failure, "escalate": escalate, "limitations": [{"code": failure}]}
    return {"ok": status == COMPLETED, "terminal": terminal, "result": result, "failure": failure_body}


def _signature(skill_id: str, arguments: dict) -> str:
    return skill_id + ":" + json.dumps(arguments or {}, sort_keys=True, default=str)
