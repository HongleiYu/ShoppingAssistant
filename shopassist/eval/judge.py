"""Optional local judge. Unavailable by default, and a null score is not a pass."""

from __future__ import annotations

RUBRIC = [
    {"id": "grounded", "prompt": "Are concrete item and policy facts limited to supplied evidence?"},
    {"id": "constraints", "prompt": "Are confirmed constraints respected?"},
    {"id": "limitations", "prompt": "Are gaps stated when evidence is insufficient?"},
]


def judge_status(enabled: bool = False) -> dict:
    if not enabled:
        return {
            "available": False,
            "score": None,
            "reason": "judge_not_configured",
            "rubric": RUBRIC,
            "skipped_explicit": True,
        }
    return {
        "available": False,
        "score": None,
        "reason": "judge_endpoint_not_running",
        "rubric": RUBRIC,
        "skipped_explicit": False,
    }
