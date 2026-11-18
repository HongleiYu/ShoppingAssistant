"""Communication decisions and session-memory updates."""

from __future__ import annotations

from shopassist.constraints import clone_memory, drop_constraint, upsert_constraint
from shopassist.dispatch import build_query, decide_route
from shopassist.parsing import extract_features


def prepare_turn(text: str, memory: dict, name_index: list[dict]) -> tuple[dict, dict, dict]:
    features = extract_features(text, name_index)
    decision = decide_route(features, memory)
    updated = apply_features(memory, features, decision, _next_turn(memory))
    return features, decision, updated


def _next_turn(memory: dict) -> int:
    return len(memory.get("task_summaries") or [])


def apply_features(memory: dict, features: dict, decision: dict, turn_index: int) -> dict:
    updated = clone_memory(memory)
    if decision["route"] in {"direct_answer", "human_escalation"} and decision["intent"] in {
        "thanks",
        "greeting",
        "ack",
        "irrelevant",
        "capabilities",
        "ask_human",
    }:
        return updated
    if decision["route"] == "clarification" and decision.get("contradicted"):
        updated = _apply_positive(updated, features, turn_index)
        updated["confirmed_constraints"] = drop_constraint(updated["confirmed_constraints"], "budget")
        updated["contradictions"] = upsert_constraint(
            updated["contradictions"],
            _constraint("budget", features["contradictions"][0]["values"], "contradicted", turn_index),
        )
        updated["missing"] = upsert_constraint(
            updated["missing"],
            _constraint("budget", None, "missing", turn_index),
        )
        return updated
    updated = _apply_positive(updated, features, turn_index)
    if decision["route"] == "clarification":
        for field in decision.get("missing") or []:
            if field == "request":
                continue
            updated["missing"] = upsert_constraint(
                updated["missing"],
                _constraint(field, None, "missing", turn_index),
            )
    return updated


def _apply_positive(memory: dict, features: dict, turn_index: int) -> dict:
    mapping = {}
    if features.get("categories"):
        mapping["category"] = features["categories"][0]
    if features.get("colors"):
        mapping["color"] = features["colors"][0]
    if features.get("sizes"):
        mapping["size"] = features["sizes"][0]
    if features.get("budgets") and features["intent"] != "contradict_budget":
        mapping["budget"] = features["budgets"][-1]
    for field, value in mapping.items():
        memory["confirmed_constraints"] = upsert_constraint(
            memory["confirmed_constraints"],
            _constraint(field, value, "confirmed", turn_index),
        )
        memory["missing"] = drop_constraint(memory["missing"], field)
        memory["contradictions"] = drop_constraint(memory["contradictions"], field)
    for assumption in features.get("assumptions") or []:
        memory["assumptions"] = upsert_constraint(
            memory["assumptions"],
            _constraint(assumption["field"], assumption["value"], "assumed", turn_index),
        )
    return memory


def _constraint(field: str, value, status: str, turn_index: int) -> dict:
    return {
        "id": f"c-{field}-{turn_index}",
        "field": field,
        "value": value,
        "status": status,
        "source_turn": turn_index,
        "text": f"{field} is {value}" if value is not None else f"{field} is {status}",
    }


def task_from_decision(features: dict, decision: dict, memory: dict, task_id: str, user_text: str) -> dict:
    return {
        "task_id": task_id,
        "goal_type": decision.get("goal_type"),
        "confirmed_constraints": list(memory.get("confirmed_constraints") or []),
        "assumptions": list(memory.get("assumptions") or []),
        "missing": list(memory.get("missing") or []),
        "query": build_query(features, memory),
        "user_text": user_text,
    }
