"""High-precision FAQ front layer.

A match is an exact normalized sentence, not a fuzzy guess. Matched turns
do not call the communication agent or the planner. Price and stock are not
answered here. A bare greeting such as "Hello" is not a FAQ, so a configured
endpoint still receives it.
"""

from __future__ import annotations

import re

_SPACE = re.compile(r"\s+")
_EDGE = re.compile(r"^[\s\"'`.,!?？。！，、]+|[\s\"'`.,!?？。！，、]+$")

_DIRECT = {
    "what can you help with": "capabilities",
    "what shopping topics can you help with": "capabilities",
    "你能帮什么忙": "capabilities",
    "你能做什么": "capabilities",
    "thanks that is all i needed": "thanks",
    "thank you for the help": "thanks",
    "sounds good": "ack",
}

_RETURNS = {
    "what is the return window",
    "what is the return window for unused items",
    "退货时限是多久",
}


def normalize_faq(text: str) -> str:
    collapsed = _SPACE.sub(" ", text or "").strip().casefold()
    return _EDGE.sub("", collapsed)


def match_faq(text: str) -> dict | None:
    key = normalize_faq(text)
    if key in _DIRECT:
        intent = _DIRECT[key]
        return {
            "route": "direct_answer",
            "route_reason_code": "faq_rule",
            "intent": intent,
            "must_not_invoke_planner": True,
            "skill_id": None,
            "skill_args": {},
            "faq": True,
        }
    if key in _RETURNS:
        return {
            "route": "simple_skill",
            "route_reason_code": "faq_rule",
            "intent": "policy_returns",
            "must_not_invoke_planner": True,
            "skill_id": "policy_lookup",
            "skill_args": {"topic": "returns", "include_stale": False},
            "faq": True,
        }
    return None
