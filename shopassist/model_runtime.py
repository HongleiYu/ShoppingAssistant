"""Parse configured-model JSON into runtime decisions.

The model chooses the route or the next planner action. Prices and policy
facts still come from skills and the tool server. Invalid model output is an
error, not a reason to switch to the mock policy.
"""

from __future__ import annotations

import json

from shopassist.config import ConfigError
from shopassist.versioning import BANNED_TRACE_KEYS

CA_ROUTES = {"direct_answer", "clarification", "simple_skill", "complex_handoff", "human_escalation"}
PA_ACTIONS = {"plan", "tool_call", "retry", "fallback", "replan", "finish", "fail", "ask_user"}
SKILLS = {
    "policy_lookup",
    "product_quote",
    "product_search",
    "product_compare",
    "budget_attribute_validate",
    "shipment_lookup",
}
GOALS = {"search", "recommend", "compare", "oos"}

CA_INSTRUCTIONS = (
    "You are the communication agent. Return one JSON object and no hidden reasoning. "
    "Allowed routes are direct_answer, clarification, simple_skill, complex_handoff, and human_escalation. "
    "For simple_skill set skill_id and skill_args. For complex_handoff set goal_type and query. "
    "Do not invent prices, stock, or policy numbers. The runtime will call tools."
)

PA_INSTRUCTIONS = (
    "You are the planner agent. Return one JSON object and no hidden reasoning. "
    "Choose the next action, plan, and skill_call. Use only the skills product_search, "
    "product_quote, product_compare, and budget_attribute_validate. "
    "Do not invent catalog facts."
)


class ModelContractError(ConfigError):
    def __init__(self, code: str, details: dict | None = None):
        payload = dict(details or {})
        payload["mock_fallback"] = False
        super().__init__(code, payload)


def parse_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ModelContractError("model_output_not_json")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelContractError("model_output_not_json") from exc
    if not isinstance(payload, dict):
        raise ModelContractError("model_output_not_object")
    banned = sorted(set(payload) & set(BANNED_TRACE_KEYS))
    if banned:
        raise ModelContractError("model_output_rejected", {"fields": banned})
    return payload


def ca_messages(text: str, memory: dict, name_index: list[dict], context_view: dict | None = None) -> list[dict]:
    names = [{"product_id": item.get("product_id"), "name": item.get("name")} for item in name_index]
    payload = {"latest_user_turn": text, "memory": memory, "name_index": names}
    if context_view:
        payload["context"] = {
            "authoritative_confirmed": (context_view.get("structured_state") or {}).get("confirmed") or {},
            "background_summary_not_authoritative": context_view.get("summary") or {},
            "coverage": context_view.get("coverage") or {},
            "profile": context_view.get("profile") or {},
            "recent_memories": context_view.get("recent_memories") or [],
        }
    return [
        {"role": "system", "content": CA_INSTRUCTIONS},
        {"role": "user", "content": json.dumps(payload, sort_keys=True)},
    ]


def parse_ca_decision(payload: dict) -> dict:
    route = payload.get("route")
    if route not in CA_ROUTES:
        raise ModelContractError("model_route_rejected", {"route": str(route)})
    decision = {
        "route": route,
        "route_reason_code": str(payload.get("route_reason_code") or "configured_model"),
        "intent": "configured_model",
        "must_not_invoke_planner": bool(payload.get("must_not_invoke_planner", route != "complex_handoff")),
        "skill_id": payload.get("skill_id"),
        "skill_args": payload.get("skill_args") or {},
        "goal_type": payload.get("goal_type"),
        "query": payload.get("query") or {},
        "reply_text": payload.get("reply_text") or "",
        "confirmed_constraints": payload.get("confirmed_constraints") or [],
        "assumptions": payload.get("assumptions") or [],
        "missing": payload.get("missing") or [],
        "contradicted": payload.get("contradicted") or [],
    }
    if not isinstance(decision["skill_args"], dict) or not isinstance(decision["query"], dict):
        raise ModelContractError("model_arguments_rejected")
    if route == "simple_skill" and decision["skill_id"] not in SKILLS:
        raise ModelContractError("model_skill_rejected", {"skill_id": str(decision["skill_id"])})
    if route == "complex_handoff":
        if decision["must_not_invoke_planner"]:
            raise ModelContractError("model_contract_conflict")
        if decision["goal_type"] not in GOALS:
            raise ModelContractError("model_goal_rejected", {"goal_type": str(decision["goal_type"])})
    if route in {"direct_answer", "clarification"} and not str(decision["reply_text"]).strip():
        raise ModelContractError("model_reply_missing")
    return decision


def pa_messages(task: dict, state: dict) -> list[dict]:
    observations = []
    for obs in state.get("observations") or []:
        observations.append(
            {
                "step_id": obs.get("step_id"),
                "skill_id": obs.get("skill_id"),
                "status": obs.get("status"),
                "item_ids": [
                    item.get("product_id")
                    for item in obs.get("items") or []
                    if isinstance(item, dict) and item.get("product_id")
                ],
            }
        )
    public_task = {
        "task_id": task.get("task_id"),
        "goal_type": task.get("goal_type"),
        "query": task.get("query") or {},
        "confirmed_constraints": task.get("confirmed_constraints") or [],
        "user_text": task.get("user_text") or "",
    }
    return [
        {"role": "system", "content": PA_INSTRUCTIONS},
        {"role": "user", "content": json.dumps({"task": public_task, "observations": observations, "plan": state.get("plan") or []}, sort_keys=True)},
    ]


def parse_pa_decision(payload: dict) -> dict:
    action = payload.get("action")
    if action not in PA_ACTIONS:
        raise ModelContractError("model_action_rejected", {"action": str(action)})
    skill_call = payload.get("skill_call")
    if skill_call is not None:
        if not isinstance(skill_call, dict) or skill_call.get("skill_id") not in SKILLS:
            raise ModelContractError("model_skill_rejected")
    if action not in {"finish", "fail", "ask_user"} and not skill_call:
        raise ModelContractError("model_missing_skill_call")
    plan = payload.get("plan")
    if plan is not None and not isinstance(plan, list):
        raise ModelContractError("model_plan_rejected")
    return {
        "action": action,
        "plan": plan,
        "skill_call": skill_call,
        "verification": payload.get("verification"),
        "result": payload.get("result"),
        "ask": payload.get("ask"),
        "failure": payload.get("failure"),
        "recovery": payload.get("recovery"),
    }


def apply_model_memory(memory: dict, decision: dict) -> dict:
    from shopassist.constraints import clone_memory

    updated = clone_memory(memory)
    if decision.get("confirmed_constraints"):
        updated["confirmed_constraints"] = [dict(item) for item in decision["confirmed_constraints"] if isinstance(item, dict)]
    if decision.get("assumptions"):
        updated["assumptions"] = [dict(item) for item in decision["assumptions"] if isinstance(item, dict)]
    return updated


def grounded_finish(state: dict) -> dict:
    items: list[dict] = []
    for obs in state.get("observations") or []:
        if obs.get("status") == "ok":
            items = [dict(item) for item in obs.get("items") or [] if isinstance(item, dict)]
    limitations = [] if items else [{"code": "no_match"}]
    return {"items": items, "limitations": limitations, "summary": "finished", "requested_name": None}
