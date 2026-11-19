"""Structured records for the deterministic mock agents.

These calls are the same route and planner decisions the runtime already
executes. They do not open a network connection and they are not a
chat-completions model.
"""

from __future__ import annotations


def ca_route_record(features: dict, decision: dict) -> dict:
    return {
        "agent": "ca",
        "phase": "route",
        "simulated": True,
        "network": False,
        "adapter": "shopassist.dispatch.decide_route",
        "input": {
            "latest_user_turn": features.get("text") or "",
            "intent": features.get("intent"),
        },
        "output": {
            "route": decision.get("route"),
            "route_reason_code": decision.get("route_reason_code"),
            "intent": decision.get("intent"),
            "skill_id": decision.get("skill_id"),
            "skill_args": decision.get("skill_args") or {},
            "goal_type": decision.get("goal_type"),
            "must_not_invoke_planner": decision.get("must_not_invoke_planner"),
        },
    }


def ca_answer_record(turn: dict, rendered: dict) -> dict:
    return {
        "agent": "ca",
        "phase": "answer",
        "simulated": True,
        "network": False,
        "adapter": "shopassist.reply_render",
        "input": {
            "tool_results": [
                {"skill_id": item.get("skill_id"), "status": item.get("status")}
                for item in turn.get("skills") or []
            ],
            "note": "The reply is rendered from tool evidence, then checked by the evidence gate.",
        },
        "output": {
            "reply_text": rendered.get("text") or "",
            "route": rendered.get("route"),
            "speaks": "evidence_renderer",
        },
    }


def pa_record(task: dict, outcome: dict) -> dict:
    call = outcome.get("skill_call") or {}
    return {
        "agent": "pa",
        "phase": outcome.get("action") or "step",
        "simulated": True,
        "network": False,
        "adapter": "shopassist.planner_policy.act",
        "input": {
            "task_id": task.get("task_id"),
            "goal_type": task.get("goal_type"),
            "user_text": task.get("user_text") or "",
        },
        "output": {
            "action": outcome.get("action"),
            "skill_id": call.get("skill_id"),
            "step_id": call.get("step_id"),
            "failure_reason": (outcome.get("failure") or {}).get("reason_code") if isinstance(outcome.get("failure"), dict) else None,
        },
    }


def needs_route_call(decision: dict) -> bool:
    return decision.get("route") in {"simple_skill", "complex_handoff"}
