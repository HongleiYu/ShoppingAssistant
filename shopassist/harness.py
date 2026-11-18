"""Shared card runner for regression and the local chat UI."""

from __future__ import annotations

from shopassist.scoring import score_trace
from shopassist.session_runner import run_session
from shopassist.simulator import render_turns


def evaluate_card(card: dict, mode: str = "mock", model_clients: dict | None = None) -> dict:
    texts = render_turns(card["turns"])
    settings = None
    if card.get("runtime"):
        from shopassist.settings import load_settings

        settings = load_settings(overrides=card["runtime"])
    trace = run_session(
        texts,
        seed=card["seed"],
        faults=card["faults"],
        session_id=card["session_id"],
        mode=mode,
        model_clients=model_clients,
        settings=settings,
    )
    score = score_trace(card["expected"], trace)
    return {
        "card_id": card["card_id"],
        "scenario_id": card["scenario_id"],
        "split": card["split"],
        "trace": trace,
        "score": score,
    }


def public_result(result: dict) -> dict:
    turns = []
    for turn in result["trace"]["turns"]:
        products = []
        seen = set()
        for claim in turn.get("claims") or []:
            product_id = claim.get("product_id")
            if product_id and product_id not in seen and claim.get("field") == "price":
                seen.add(product_id)
                products.append(
                    {
                        "product_id": product_id,
                        "price": claim.get("value"),
                        "field": "price",
                    }
                )
        evidence = []
        for item in turn.get("evidence") or []:
            evidence.append(
                {
                    "evidence_id": item.get("evidence_id"),
                    "tool_name": item.get("tool_name"),
                    "status": item.get("status"),
                    "untrusted_text": item.get("untrusted_text") or [],
                }
            )
        turns.append(
            {
                "user_text": turn.get("user_text"),
                "reply_text": turn.get("reply_text"),
                "route": turn.get("route"),
                "route_reason_code": turn.get("route_reason_code"),
                "planner_invoked": turn.get("planner_invoked"),
                "task_id": turn.get("task_id"),
                "skills": turn.get("skills") or [],
                "tools": [
                    {"tool": tool.get("tool"), "status": tool.get("status"), "evidence_id": tool.get("evidence_id")}
                    for tool in turn.get("tools") or []
                ],
                "recovery": turn.get("recovery") or [],
                "limitations": turn.get("limitations") or [],
                "products": products,
                "evidence": evidence,
                "context_view": turn.get("context_view"),
            }
        )
    score = result["score"]
    return {
        "card_id": result["card_id"],
        "scenario_id": result["scenario_id"],
        "evaluation_kind": result["trace"].get("evaluation_kind"),
        "passed": score["passed"],
        "checks": score["failures"],
        "turns": turns,
        "tool_call_count": score["tool_call_count"],
        "token_usage": None,
        "token_usage_reason": "unavailable_for_mock",
    }
