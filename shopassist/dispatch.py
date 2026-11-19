"""Route table. Orchestration calls this; it does not hard-code one scenario."""

from __future__ import annotations

from shopassist.constraints import constraints_by_field

POLICY_INTENTS = {
    "policy_returns",
    "policy_stale",
    "policy_warranty",
    "policy_shipping",
    "policy_care",
    "policy_conflict",
    "policy_missing",
    "policy_legacy",
}
DIRECT_INTENTS = {"thanks", "greeting", "ack", "irrelevant", "capabilities"}
HANDOFF_INTENTS = {
    "search",
    "search_size",
    "recommend",
    "compare",
    "compare_warranty",
    "oos",
    "change_budget",
    "add_color",
    "help_find",
}


def _value(memory: dict, features: dict, field: str):
    current = constraints_by_field(memory.get("confirmed_constraints") or [])
    if field == "budget" and features.get("budgets") and features["intent"] != "contradict_budget":
        return features["budgets"][-1]
    if field == "category" and features.get("categories"):
        return features["categories"][0]
    if field == "color" and features.get("colors"):
        return features["colors"][0]
    if field == "size" and features.get("sizes"):
        return features["sizes"][0]
    if field in current:
        return current[field]["value"]
    return None


def decide_route(features: dict, memory: dict) -> dict:
    intent = features["intent"]
    if intent == "knowledge_policy":
        return _decision(
            "simple_skill",
            "knowledge_retrieve",
            intent,
            True,
            skill_id="knowledge_retrieve",
            skill_args={"query": features.get("kb_query") or "", "corpus": "policy"},
        )
    if intent == "knowledge_product":
        args = {"query": features.get("kb_query") or "", "corpus": "product"}
        budget = _value(memory, features, "budget")
        if budget is not None:
            args["max_price"] = budget
        return _decision(
            "simple_skill",
            "knowledge_retrieve",
            intent,
            True,
            skill_id="knowledge_retrieve",
            skill_args=args,
        )
    if intent == "ask_human":
        return _decision("human_escalation", "user_requested_person", intent, True)
    if intent in DIRECT_INTENTS:
        return _decision("direct_answer", intent, intent, True)
    if features.get("contradictions"):
        return _decision("clarification", "contradiction", intent, True, missing=["budget"], contradicted=["budget"])
    if intent in POLICY_INTENTS:
        return _decision(
            "simple_skill",
            "policy_lookup",
            intent,
            True,
            skill_id="policy_lookup",
            skill_args={"topic": features.get("topic") or "", "include_stale": bool(features.get("include_stale"))},
        )
    if intent == "shipment":
        if not features.get("tracking_id"):
            return _decision("clarification", "missing_information", intent, True, missing=["tracking"])
        return _decision(
            "simple_skill",
            "shipment_lookup",
            intent,
            True,
            skill_id="shipment_lookup",
            skill_args={"tracking_id": features["tracking_id"]},
        )
    if intent == "quote":
        product_ids = list(features.get("product_ids") or [])
        if not product_ids and features.get("use_current_product"):
            current = memory.get("consulted_product_id")
            if current:
                product_ids = [current]
        if len(product_ids) != 1:
            return _decision("clarification", "missing_information", intent, True, missing=["product"])
        return _decision(
            "simple_skill",
            "single_product_quote",
            intent,
            True,
            skill_id="product_quote",
            skill_args={"product_id": product_ids[0], "fresh": False},
        )
    if intent in HANDOFF_INTENTS:
        missing = _missing_slots(intent, features, memory)
        if missing:
            return _decision("clarification", "missing_information", intent, True, missing=missing)
        goal = {
            "search": "search",
            "search_size": "search",
            "help_find": "search",
            "change_budget": "search",
            "add_color": "search",
            "recommend": "recommend",
            "compare": "compare",
            "compare_warranty": "compare",
            "oos": "oos",
        }[intent]
        return _decision("complex_handoff", "needs_plan", intent, False, goal_type=goal)
    return _decision("clarification", "unrecognized", intent, True, missing=["request"])


def _missing_slots(intent: str, features: dict, memory: dict) -> list[str]:
    missing = []
    if intent in {"compare", "compare_warranty"}:
        if len(features.get("product_ids") or []) < 2:
            missing.append("product")
        return missing
    if intent == "oos":
        if len(features.get("product_ids") or []) < 1:
            missing.append("product")
        if _value(memory, features, "category") is None:
            missing.append("category")
        return missing
    if intent == "help_find":
        if _value(memory, features, "category") is None:
            missing.append("category")
        if _value(memory, features, "budget") is None:
            missing.append("budget")
        return missing
    if _value(memory, features, "category") is None:
        missing.append("category")
    if _value(memory, features, "budget") is None:
        missing.append("budget")
    if intent == "search_size" and _value(memory, features, "size") is None:
        missing.append("size")
    if intent == "add_color" and _value(memory, features, "color") is None:
        missing.append("color")
    return missing


def build_query(features: dict, memory: dict) -> dict:
    return {
        "product_ids": list(features.get("product_ids") or []),
        "category": _value(memory, features, "category"),
        "color": _value(memory, features, "color"),
        "size": _value(memory, features, "size"),
        "max_price": _value(memory, features, "budget"),
        "price_op": "lt",
    }


def _decision(route, reason, intent, must_not, **extra):
    decision = {
        "route": route,
        "route_reason_code": reason,
        "intent": intent,
        "must_not_invoke_planner": must_not,
        "skill_id": extra.get("skill_id"),
        "skill_args": extra.get("skill_args") or {},
        "goal_type": extra.get("goal_type"),
        "missing": extra.get("missing") or [],
        "contradicted": extra.get("contradicted") or [],
    }
    return decision
