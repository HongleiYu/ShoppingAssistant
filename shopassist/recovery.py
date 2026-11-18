"""One recovery decision for simple skills and the planner.

Retries stay inside the current request budget. Permanent auth and bad
requests are not retried. Non-idempotent calls are not repeated.
"""

from __future__ import annotations

import random
import time

TRANSIENT_HTTP = {429, 502, 503, 504}
PERMANENT_AUTH = {401, 403}
PERMANENT_REQUEST = {400, 404, 422}


class TransientEndpointError(Exception):
    def __init__(self, http_status: int, retry_after: float | None = None):
        self.http_status = int(http_status)
        self.retry_after = retry_after
        super().__init__(f"http_{http_status}")


class PermanentEndpointError(Exception):
    def __init__(self, http_status: int):
        self.http_status = int(http_status)
        super().__init__(f"http_{http_status}")


def classify(*, status: str | None = None, http_status: int | None = None, error_code: str | None = None) -> str:
    if http_status in PERMANENT_AUTH or error_code in {"unauthorized", "forbidden"}:
        return "auth"
    if http_status in PERMANENT_REQUEST or error_code in {"bad_arguments", "invalid_request"}:
        return "permanent"
    if http_status in TRANSIENT_HTTP or error_code in {"timeout", "transport", "rate_limited", "unavailable"}:
        return "transient"
    if status == "timeout":
        return "transient"
    if status == "malformed":
        return "malformed"
    if status == "stale":
        return "stale"
    if status == "conflict":
        return "conflict"
    if status == "empty":
        return "empty"
    if status == "error":
        return "unavailable"
    if status == "ok":
        return "ok"
    return "permanent"


def choose(
    *,
    status: str | None,
    http_status: int | None = None,
    error_code: str | None = None,
    skill_id: str | None = None,
    goal_type: str | None = None,
    attempt: int = 0,
    fallback_count: int = 0,
    replan_count: int = 0,
    limits: dict | None = None,
    idempotent: bool = True,
    partial_streamed: bool = False,
) -> dict:
    limits = limits or {}
    max_retries = int(limits.get("max_retries_per_step", 1))
    max_fallbacks = int(limits.get("max_fallbacks", 1))
    max_replans = int(limits.get("max_replans", 1))
    kind = classify(status=status, http_status=http_status, error_code=error_code)
    if partial_streamed:
        return {"action": "stop", "reason": "partial_output", "kind": kind}
    if attempt > 0 and not idempotent:
        return {"action": "stop", "reason": "side_effect_not_retried", "kind": kind}
    if kind == "auth":
        return {"action": "stop", "reason": "auth", "kind": kind}
    if kind == "permanent":
        return {"action": "stop", "reason": "invalid_request", "kind": kind}
    if kind == "transient" and not idempotent:
        return {"action": "stop", "reason": "side_effect_not_retried", "kind": kind}
    if kind == "transient" and attempt < max_retries:
        return {"action": "retry", "reason": "transient", "kind": kind}
    if kind == "stale" and skill_id == "product_quote" and attempt < max_retries and idempotent:
        return {"action": "refresh", "reason": "stale", "kind": kind}
    if kind == "unavailable" and skill_id == "product_search" and fallback_count < max_fallbacks:
        return {"action": "cached_fallback", "reason": "error", "kind": kind}
    if kind == "malformed" and goal_type in {"search", "recommend"} and replan_count < max_replans:
        return {"action": "replan", "reason": "malformed", "kind": kind}
    if kind == "conflict":
        return {"action": "escalate", "reason": "conflicting_evidence", "kind": kind}
    if kind == "empty":
        return {"action": "accept_empty", "reason": "empty", "kind": kind}
    if kind == "transient":
        return {"action": "stop", "reason": "transient_exhausted", "kind": kind}
    if kind == "stale":
        return {"action": "stop", "reason": "stale_unresolved", "kind": kind}
    if kind == "malformed":
        return {"action": "stop", "reason": "malformed_evidence", "kind": kind}
    if kind == "unavailable":
        return {"action": "stop", "reason": "tool_error", "kind": kind}
    return {"action": "stop", "reason": "tool_error", "kind": kind}


def delay_seconds(attempt: int, retry_after: float | None, settings: dict, rng: random.Random) -> float:
    recovery = settings["recovery"]
    if recovery.get("honor_retry_after", True) and retry_after is not None:
        return max(0.0, float(retry_after))
    base = float(recovery["backoff_base_seconds"]) * (2 ** max(0, attempt))
    cap = float(recovery["backoff_cap_seconds"])
    raw = min(base, cap)
    spread = raw * float(recovery["jitter_ratio"])
    return min(cap, raw + spread * rng.random())


def wait_bounded(seconds: float, budget, cancel=None, sleeper=None) -> str | None:
    if cancel is not None and cancel.is_set():
        return "cancelled"
    remaining = budget.request_deadline - budget.clock.now()
    if seconds > remaining:
        return "wall_clock"
    if seconds > 0 and sleeper is not None:
        sleeper(seconds, budget.clock)
    elif seconds > 0 and hasattr(budget.clock, "advance"):
        budget.clock.advance(seconds)
    elif seconds > 0:
        time.sleep(seconds)
    if cancel is not None and cancel.is_set():
        return "wall_clock" if False else "cancelled"
    return None
