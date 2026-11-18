"""Evaluation purposes are separate from capability tags and from train/val/test splits.

Membership counts overlap. Unique cases are de-duplicated by case_id.
Synthetic fixtures are not a real-user distribution, so weighted production
scores stay null until validated weights and real samples exist.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from shopassist.cards import all_cards
from shopassist.context_engine import Clock, ContextEngine, authoritative_confirmed
from shopassist.eval.compare import aggregate
from shopassist.harness import evaluate_card
from shopassist.scoring import score_trace
from shopassist.session_runner import AgentSession
from shopassist.sandbox import SessionSandbox

CATALOG_VERSION = "eval-catalog-v1"

PURPOSES = {
    "capability_acceptance": "Golden checks for communication, planning, retrieval, tools, skills, memory, and end-to-end runs.",
    "release_regression": "Frozen must-pass set for this catalog version. Failures here are release blockers.",
    "robustness_challenge": "Fault injection, untrusted text, and corrections. Development challenges stay out of the frozen set until a version promotes them.",
    "distributional_sample": "Synthetic strata with sampling metadata. Not a production traffic sample.",
    "sealed_generalization": "Novel template or constraint combinations excluded from training and routine tuning.",
    "workload_cost": "Longer sessions used to record tokens, latency, and call counts. Token usage is null when the runtime does not report it.",
}

REQUIRED_CAPABILITIES = (
    "ca_routing",
    "pa_planning",
    "retrieval",
    "tools_mcp",
    "skills",
    "profile",
    "recent_memory",
    "session_state",
    "e2e",
    "safety_untrusted",
    "correction",
    "workload",
)

_TAG_CAPABILITIES = {
    "direct_answer": "ca_routing",
    "clarification": "ca_routing",
    "unnecessary_planner": "ca_routing",
    "irrelevant": "ca_routing",
    "simple_tool": "skills",
    "kb_policy": "retrieval",
    "retrieval_miss": "retrieval",
    "retrieval_conflict": "retrieval",
    "retrieval_empty": "retrieval",
    "comparison": "pa_planning",
    "complex_recommendation": "pa_planning",
    "dynamic_field": "tools_mcp",
    "static_attribute": "tools_mcp",
    "tool_timeout": "tools_mcp",
    "tool_error": "tools_mcp",
    "tool_malformed": "tools_mcp",
    "tool_conflict": "tools_mcp",
    "stale_record": "tools_mcp",
    "untrusted_injection": "safety_untrusted",
    "human_escalation": "safety_untrusted",
    "constraint_change": "session_state",
    "missing_or_contradiction": "session_state",
    "fallback": "pa_planning",
    "retry": "pa_planning",
    "replan": "pa_planning",
}


def build_catalog() -> list[dict]:
    cases = []
    for card in all_cards():
        purposes = ["capability_acceptance"]
        if card["split"] == "regression":
            purposes.append("release_regression")
        if card["split"] == "sealed":
            purposes.append("sealed_generalization")
        if any(tag in card["tags"] for tag in ("tool_timeout", "tool_error", "tool_malformed", "tool_conflict", "untrusted_injection", "stale_record", "retrieval_empty", "retrieval_conflict")):
            purposes.append("robustness_challenge")
        sampling = None
        if card["card_id"] in {"thanks-a", "policy-returns", "search-sand"}:
            purposes.append("distributional_sample")
            sampling = {
                "design": "stratified_synthetic",
                "stratum": {"thanks-a": "simple_dialog", "policy-returns": "policy", "search-sand": "search"}[card["card_id"]],
                "weight": None,
                "weight_status": "unavailable",
            }
        capabilities = sorted({_TAG_CAPABILITIES[tag] for tag in card["tags"] if tag in _TAG_CAPABILITIES})
        if card["split"] != "sealed" and "pa_planning" in capabilities:
            capabilities.append("e2e")
        if card["card_id"] == "sealed-notebook":
            capabilities.extend(["pa_planning", "e2e", "tools_mcp"])
        cases.append(_case_from_card(card, purposes, capabilities, sampling))
    cases.extend(_memory_cases())
    _reject_duplicate_ids(cases)
    return cases


def _case_from_card(card, purposes, capabilities, sampling) -> dict:
    return {
        "case_id": card["card_id"],
        "runner": "card",
        "purposes": sorted(set(purposes)),
        "capabilities": sorted(set(capabilities)),
        "scenario_id": card["scenario_id"],
        "complexity": "multi_turn" if len(card["turns"]) > 1 else "single_turn",
        "lineage_id": card["lineage_id"],
        "session_id": card["session_id"],
        "source": {"kind": "synthetic", "authored_at": "2026-01-15", "deidentified": True},
        "split": card["split"],
        "catalog_version": CATALOG_VERSION,
        "faults": card["faults"],
        "sampling": sampling,
        "suite_role": "acceptance",
        "provenance": {"builder": "eval_catalog", "card_id": card["card_id"]},
    }


def _memory_cases() -> list[dict]:
    common = {
        "runner": "memory",
        "source": {"kind": "synthetic", "authored_at": "2026-01-15", "deidentified": True},
        "catalog_version": CATALOG_VERSION,
        "faults": [],
        "sampling": None,
        "complexity": "long_session",
    }
    return [
        {**common, "case_id": "mem-long-correction", "purposes": ["capability_acceptance", "release_regression", "workload_cost"], "capabilities": ["session_state", "correction", "workload", "e2e"], "scenario_id": "session_memory", "lineage_id": "lin-mem-long", "session_id": "sess-mem-long", "split": "regression", "suite_role": "acceptance"},
        {**common, "case_id": "mem-seven-day", "purposes": ["capability_acceptance", "robustness_challenge"], "capabilities": ["recent_memory", "profile"], "scenario_id": "recent_memory", "lineage_id": "lin-mem-seven", "session_id": "sess-mem-seven", "split": "dev_challenge", "suite_role": "development_challenge", "complexity": "store"},
        {**common, "case_id": "mem-isolation", "purposes": ["capability_acceptance"], "capabilities": ["profile", "recent_memory"], "scenario_id": "recent_memory", "lineage_id": "lin-mem-iso", "session_id": "sess-mem-iso", "split": "dev_challenge", "suite_role": "development_challenge", "complexity": "store"},
        {**common, "case_id": "mem-summary-lag", "purposes": ["robustness_challenge", "workload_cost"], "capabilities": ["session_state", "workload"], "scenario_id": "session_memory", "lineage_id": "lin-mem-lag", "session_id": "sess-mem-lag", "split": "dev_challenge", "suite_role": "development_challenge"},
        {**common, "case_id": "mem-cache-restore", "purposes": ["robustness_challenge"], "capabilities": ["session_state"], "scenario_id": "session_memory", "lineage_id": "lin-mem-cache", "session_id": "sess-mem-cache", "split": "dev_challenge", "suite_role": "development_challenge", "complexity": "store"},
    ]


def _reject_duplicate_ids(cases: list[dict]) -> None:
    seen = set()
    for case in cases:
        if case["case_id"] in seen:
            raise ValueError("duplicate case_id " + case["case_id"])
        seen.add(case["case_id"])


def coverage_matrix(cases: list[dict] | None = None) -> dict:
    cases = cases if cases is not None else build_catalog()
    unique = {case["case_id"]: case for case in cases}
    memberships = defaultdict(int)
    overlaps = []
    for case in cases:
        for purpose in case["purposes"]:
            memberships[purpose] += 1
        if len(case["purposes"]) > 1:
            overlaps.append(case["case_id"])
    present = set()
    for case in cases:
        present.update(case["capabilities"])
    missing = [name for name in REQUIRED_CAPABILITIES if name not in present]
    return {
        "catalog_version": CATALOG_VERSION,
        "unique_case_count": len(unique),
        "membership_counts": dict(sorted(memberships.items())),
        "membership_sum": sum(memberships.values()),
        "overlap_case_ids": sorted(overlaps),
        "counts_are_not_additive": True,
        "missing_capabilities": missing,
        "production_sample_count": 0,
        "production_kpi": None,
        "production_kpi_reason": "no_deidentified_or_real_samples_loaded",
    }


def run_eval_catalog(purposes: list[str] | None = None, *, include_development: bool = True) -> dict:
    cases = build_catalog()
    selected = []
    for case in cases:
        if purposes and not set(purposes).intersection(case["purposes"]):
            continue
        if not include_development and case["suite_role"] != "acceptance":
            continue
        selected.append(case)
    # De-duplicate execution by case_id even if the filter matched multiple purposes.
    ordered = list({case["case_id"]: case for case in selected}.values())
    results = [_execute(case) for case in ordered]
    by_purpose = {}
    for purpose in PURPOSES:
        if purposes and purpose not in purposes:
            continue
        members = [result for result in results if purpose in result["purposes"]]
        by_purpose[purpose] = _purpose_summary(purpose, members)
    critical = [result for result in results if "release_regression" in result["purposes"]]
    return {
        "catalog_version": CATALOG_VERSION,
        "evaluation_kind": "mock_demo",
        "unique_executed": len(results),
        "matrix": coverage_matrix(cases),
        "purposes": by_purpose,
        "results": results,
        "release_passed": all(item["passed"] for item in critical) if critical else False,
        "weighted_aggregate": {"value": None, "reason": "weights_unavailable_or_unvalidated"},
        "repeat_trials": 1,
        "significance_test": None,
        "judge": {"available": False, "score": None, "reason": "judge_not_configured"},
        "notes": [
            "Fixtures are synthetic. Membership counts overlap and are not independent sample sizes.",
            "Behavioral checks use outcome constraints, not one exact tool path.",
            "Promote a development challenge into release_regression only with a validated expected outcome and a new catalog version.",
        ],
    }


def _execute(case: dict) -> dict:
    if case["runner"] == "card":
        card = next(item for item in all_cards() if item["card_id"] == case["case_id"])
        evaluated = evaluate_card(card)
        score = evaluated["score"]
        trace = evaluated["trace"]
        return _result(case, score["passed"], score["failures"], trace.get("latency_ms"), score["tool_call_count"], trace.get("token_usage"), trace.get("token_usage_reason"))
    failures = _run_memory_case(case["case_id"])
    return _result(case, not failures, failures, None, None, None, "unavailable_for_memory_case")


def _result(case, passed, failures, latency, tool_calls, token_usage, token_reason) -> dict:
    modules = sorted({_module_for(item.get("check")) for item in failures})
    return {
        "case_id": case["case_id"],
        "purposes": case["purposes"],
        "capabilities": case["capabilities"],
        "suite_role": case["suite_role"],
        "split": case["split"],
        "passed": passed,
        "failures": failures,
        "localized_modules": modules,
        "latency_ms": latency,
        "tool_call_count": tool_calls,
        "token_usage": token_usage,
        "token_usage_reason": token_reason,
        "sampling": case["sampling"],
        "unknown": False,
    }


def _module_for(check: str | None) -> str:
    if check in {"route", "unnecessary_planner", "planner_turns"}:
        return "communication"
    if check in {"recovery", "tool_status", "tool_count"}:
        return "planner_or_tools"
    if check in {"unsupported_claim", "missing_fact", "product_set", "hard_constraint"}:
        return "grounding"
    if check in {"coverage", "summary", "memory", "profile", "recent_memory"}:
        return "context"
    return "case"


def _purpose_summary(purpose: str, results: list[dict]) -> dict:
    strata = defaultdict(list)
    for result in results:
        sampling = result.get("sampling") or {}
        strata[sampling.get("stratum") or "unspecified"].append(result)
    return {
        "description": PURPOSES[purpose],
        "case_count": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed_case_ids": [item["case_id"] for item in results if not item["passed"]],
        "unweighted_by_stratum": {
            name: {
                "count": len(items),
                "failures": sum(1 for item in items if not item["passed"]),
                "pass_rate": (sum(1 for item in items if item["passed"]) / len(items)) if items else None,
            }
            for name, items in sorted(strata.items())
        },
        "weighted_pass_rate": None,
        "weighted_pass_rate_reason": "synthetic_weights_not_validated",
        "metrics": aggregate(
            [
                {
                    "passed": item["passed"],
                    "unsupported_claim_count": sum(1 for failure in item["failures"] if failure.get("check") == "unsupported_claim"),
                    "hard_constraint_violations": sum(1 for failure in item["failures"] if failure.get("check") == "hard_constraint"),
                    "unnecessary_planner": any(failure.get("check") == "unnecessary_planner" for failure in item["failures"]),
                    "schema_ok": True,
                }
                for item in results
            ]
        ),
    }


def _run_memory_case(case_id: str) -> list[dict]:
    if case_id == "mem-long-correction":
        return _long_correction()
    if case_id == "mem-seven-day":
        return _seven_day()
    if case_id == "mem-isolation":
        return _isolation()
    if case_id == "mem-summary-lag":
        return _summary_lag()
    if case_id == "mem-cache-restore":
        return _cache_restore()
    return [{"check": "unknown_case", "detail": case_id}]


def _long_correction() -> list[dict]:
    engine = ContextEngine(sync_catchup_message_budget=0, summary_min_new_messages=100)
    box = SessionSandbox.start(seed=3)
    failures = []
    try:
        session = AgentSession(box, "sess-mem-long", context=engine, tenant_id="local", user_id="mem-user")
        session.user_message("Find a drinkware item under 20 dollars.")
        for _ in range(5):
            session.user_message("Thanks, that is all I needed.")
        session.user_message("Change the budget to 13 dollars.")
        view = session.history[-1]["context_view"]
        coverage = view["coverage"]
        if not coverage["coverage_complete"]:
            failures.append({"check": "coverage", "detail": coverage["missing_message_ids"]})
        if coverage["message_count"] <= 10:
            failures.append({"check": "workload", "detail": coverage["message_count"]})
        if view["structured_state"]["confirmed"].get("budget") != 13:
            failures.append({"check": "correction", "detail": view["structured_state"]["confirmed"]})
        if view["structured_state"]["tombstone_count"] < 1:
            failures.append({"check": "memory", "detail": "missing_tombstone"})
        if session.trace()["active_task_id_after"] is not None:
            failures.append({"check": "task_lifecycle", "detail": "active_task_open"})
    finally:
        box.close()
    return failures


def _seven_day() -> list[dict]:
    start = datetime(2026, 1, 15, tzinfo=timezone.utc)
    clock = Clock(start)
    engine = ContextEngine(clock=clock, sync_catchup_message_budget=0)
    memory = empty_like()
    engine.record_exchange("tenant", "user-a", "s1", "I always prefer sand.", "Noted.", memory)
    failures = []
    fresh = engine.assemble("tenant", "user-a", "s1")
    if (fresh["profile"].get("stable") or {}).get("color") != "sand":
        failures.append({"check": "profile", "detail": fresh["profile"]})
    if not fresh["recent_memories"]:
        failures.append({"check": "recent_memory", "detail": "missing_before_expiry"})
    clock.advance(seconds=(7 * 24 * 60 * 60) - 1)
    still = engine.assemble("tenant", "user-a", "s1")
    if not still["recent_memories"]:
        failures.append({"check": "recent_memory", "detail": "expired_too_early"})
    clock.advance(seconds=1)
    expired = engine.assemble("tenant", "user-a", "s1")
    if expired["recent_memories"]:
        failures.append({"check": "recent_memory", "detail": "present_at_seven_day_boundary"})
    if (expired["profile"].get("stable") or {}).get("color") != "sand":
        failures.append({"check": "profile", "detail": "profile_should_remain"})
    return failures


def _isolation() -> list[dict]:
    engine = ContextEngine(sync_catchup_message_budget=0)
    memory = empty_like()
    engine.record_exchange("tenant-a", "user-a", "s1", "I always prefer sand.", "Noted.", memory)
    other_user = engine.assemble("tenant-a", "user-b", "s2")
    other_tenant = engine.assemble("tenant-b", "user-a", "s3")
    same = engine.assemble("tenant-a", "user-a", "s9")
    failures = []
    if other_user["recent_memories"] or other_user["profile"]:
        failures.append({"check": "profile", "detail": "leaked_across_users"})
    if other_tenant["recent_memories"] or other_tenant["profile"]:
        failures.append({"check": "profile", "detail": "leaked_across_tenants"})
    if not same["recent_memories"]:
        failures.append({"check": "recent_memory", "detail": "missing_same_user_new_session"})
    return failures


def _summary_lag() -> list[dict]:
    engine = ContextEngine(sync_catchup_message_budget=0, summary_min_new_messages=100)
    memory = empty_like()
    for index in range(6):
        engine.record_exchange("t", "u", "lag", f"note {index}", f"ack {index}", memory)
    view = engine.assemble("t", "u", "lag")
    failures = []
    if view["coverage"]["summary_watermark"] != 0:
        failures.append({"check": "summary", "detail": "refresh_ran_too_soon"})
    if not view["coverage"]["uncovered_message_ids"]:
        failures.append({"check": "coverage", "detail": "tail_missing"})
    if not view["coverage"]["coverage_complete"]:
        failures.append({"check": "coverage", "detail": view["coverage"]["missing_message_ids"]})
    engine.refresh_now("t", "u", "lag")
    refreshed = engine.assemble("t", "u", "lag")
    if refreshed["coverage"]["summary_watermark"] < 12:
        failures.append({"check": "summary", "detail": refreshed["coverage"]["summary_watermark"]})
    if refreshed["summary"]["source_kind"] != "mock":
        failures.append({"check": "summary", "detail": refreshed["summary"]["source_kind"]})
    return failures


def _cache_restore() -> list[dict]:
    engine = ContextEngine(sync_catchup_message_budget=0, summary_min_new_messages=1)
    memory = empty_like()
    engine.record_exchange("t", "u", "cache", "hello", "ok", memory)
    engine.record_exchange("t", "u", "cache", "again", "ok", memory)
    engine.refresh_now("t", "u", "cache")
    key_status = engine.assemble("t", "u", "cache")["cache_status"]
    engine.cache.delete("shopassist:t:cache:summary")
    restored = engine.assemble("t", "u", "cache")
    failures = []
    if restored["cache_status"] not in {"restored_from_durable", "restored_from_mysql"}:
        failures.append({"check": "summary", "detail": restored["cache_status"]})
    if not restored["summary"]:
        failures.append({"check": "summary", "detail": "lost_after_cache_delete"})
    if key_status not in {"hit", "restored_from_durable", "restored_from_mysql", "durable_newer_than_cache"}:
        failures.append({"check": "summary", "detail": key_status})
    return failures


def empty_like() -> dict:
    return {
        "confirmed_constraints": [],
        "assumptions": [],
        "missing": [],
        "contradictions": [],
        "task_summaries": [],
        "active_task_id": None,
    }


def ingest_deidentified(document: dict) -> dict:
    if document.get("schema") != "deidentified-sample-v1":
        raise ValueError("sample_schema")
    if document.get("production_kpi") is not None:
        raise ValueError("production_kpi_not_allowed_without_a_real_sample_pipeline")
    records = document.get("records") or []
    for record in records:
        source = record.get("source") or {}
        if source.get("kind") != "deidentified_sample":
            raise ValueError("source_kind")
        if source.get("deidentified") is not True:
            raise ValueError("deidentified_flag")
        blob = json.dumps(record)
        for banned in ("@", "api_key", "employer"):
            if banned in blob.lower():
                raise ValueError("sample_rejected")
    return {"accepted": len(records), "production_kpi": None, "sample_count": len(records)}


def render_catalog_markdown(report: dict) -> str:
    lines = [
        "# Evaluation catalog",
        "",
        f"Version: {report['catalog_version']}",
        f"Unique cases executed: {report['unique_executed']}",
        f"Release passed: {report['release_passed']}",
        f"Weighted aggregate: {report['weighted_aggregate']['reason']}",
        "",
        "Membership counts overlap. Do not add them into a sample size.",
        "",
    ]
    matrix = report["matrix"]
    lines.append(f"Unique catalog cases: {matrix['unique_case_count']}")
    lines.append(f"Missing capabilities: {', '.join(matrix['missing_capabilities']) or 'none'}")
    lines.append("")
    for purpose, summary in report["purposes"].items():
        lines.append(f"## {purpose}")
        lines.append("")
        lines.append(summary["description"])
        lines.append(f"Cases: {summary['case_count']}. Passed: {summary['passed']}.")
        if summary["failed_case_ids"]:
            lines.append("Failed: " + ", ".join(summary["failed_case_ids"]))
        lines.append("")
    lines.append("Production KPI: null. No real sample file was scored.")
    lines.append("")
    return "\n".join(lines)
