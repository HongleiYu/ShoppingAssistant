"""Three evaluation suites, separate from capability tags and from train/val/test.

Suites overlap. Unique cases are de-duplicated by case_id. The v1 six-purpose
names remain only as a migration map. Synthetic fixtures are not an online sample.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from shopassist.cards import all_cards
from shopassist.context_engine import Clock, ContextEngine
from shopassist.eval.compare import aggregate
from shopassist.harness import evaluate_card
from shopassist.session_runner import AgentSession
from shopassist.sandbox import SessionSandbox
from shopassist.versioning import CATALOG_SNAPSHOT_ID, KB_VERSION

CATALOG_VERSION = "eval-catalog-v2"

SUITES = {
    "development": "Current issues, feature variants, and faults. Run a tag subset while tuning.",
    "regression": "Versioned acceptance set: representative behavior plus reviewed important failures. Critical is a flag inside this suite.",
    "online_sample": "Rolling deidentified real sessions. Empty until a real batch is imported. Synthetic demos are not this suite.",
}

# v1 purpose -> how a caller filters v2. Not a fourth suite.
LEGACY_PURPOSE_FILTER = {
    "capability_acceptance": {"suite": "regression"},
    "release_regression": {"suite": "regression", "critical_only": True},
    "robustness_challenge": {"suite": "development", "tag": "robustness"},
    "distributional_sample": {"synthetic_demo_only": True},
    "sealed_generalization": {"suite": "regression", "tag": "novel_combination"},
    "workload_cost": {"tag": "perf_cost"},
}

REQUIRED_TAGS = (
    "long_context",
    "memory",
    "ca",
    "pa",
    "mcp_tools",
    "robustness",
    "perf_cost",
    "novel_combination",
)

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

_CAPABILITY_TAGS = {
    "ca_routing": ("ca",),
    "pa_planning": ("pa",),
    "retrieval": ("mcp_tools",),
    "tools_mcp": ("mcp_tools",),
    "skills": ("mcp_tools",),
    "profile": ("memory",),
    "recent_memory": ("memory",),
    "session_state": ("memory",),
    "e2e": ("pa",),
    "safety_untrusted": ("robustness", "ca"),
    "correction": ("memory",),
    "workload": ("perf_cost", "long_context"),
}

_ROBUST_CARD_TAGS = {
    "tool_timeout",
    "tool_error",
    "tool_malformed",
    "tool_conflict",
    "untrusted_injection",
    "stale_record",
    "retrieval_empty",
    "retrieval_conflict",
    "fallback",
    "retry",
    "replan",
}

_SYNTHETIC_DEMO_IDS = {"thanks-a", "policy-returns", "search-sand"}


def build_catalog() -> list[dict]:
    cases = []
    for card in all_cards():
        capabilities = sorted({_TAG_CAPABILITIES[tag] for tag in card["tags"] if tag in _TAG_CAPABILITIES})
        if card["split"] != "sealed" and "pa_planning" in capabilities:
            capabilities.append("e2e")
        if card["card_id"] == "sealed-notebook":
            capabilities.extend(["pa_planning", "e2e", "tools_mcp"])
        tags = _tags_for(capabilities, card["tags"], card["card_id"])
        suites = ["development"] if card["split"] in {"train", "val", "test"} else ["regression"]
        if card["split"] == "regression" and _ROBUST_CARD_TAGS.intersection(card["tags"]):
            suites.append("development")
        critical = card["split"] == "regression"
        sampling = None
        synthetic_demo = card["card_id"] in _SYNTHETIC_DEMO_IDS
        if synthetic_demo:
            sampling = {
                "design": "synthetic_demo",
                "stratum": {"thanks-a": "simple_dialog", "policy-returns": "policy", "search-sand": "search"}[card["card_id"]],
                "weight": None,
                "weight_status": "unavailable",
                "online": False,
            }
        cases.append(
            _case(
                case_id=card["card_id"],
                runner="card",
                suites=suites,
                critical=critical,
                tags=tags,
                capabilities=capabilities,
                scenario_id=card["scenario_id"],
                complexity="multi_turn" if len(card["turns"]) > 1 else "single_turn",
                lineage_id=card["lineage_id"],
                session_id=card["session_id"],
                split=card["split"],
                faults=card["faults"],
                sampling=sampling,
                synthetic_demo=synthetic_demo,
                legacy=_legacy_for_card(card, synthetic_demo),
            )
        )
    cases.extend(_memory_cases())
    _reject_duplicate_ids(cases)
    return cases


def _tags_for(capabilities: list[str], card_tags: list[str], case_id: str) -> list[str]:
    tags = set()
    for name in capabilities:
        tags.update(_CAPABILITY_TAGS.get(name, ()))
    if _ROBUST_CARD_TAGS.intersection(card_tags):
        tags.add("robustness")
    if case_id == "sealed-notebook":
        tags.add("novel_combination")
    if "ca" not in tags and "pa" not in tags:
        tags.add("ca")
    return sorted(tags)


def _legacy_for_card(card: dict, synthetic_demo: bool) -> list[str]:
    """v1 purpose names kept on the case so the migration stays inspectable."""
    names = ["capability_acceptance"]
    if card["split"] == "regression":
        names.append("release_regression")
    if card["split"] == "sealed":
        names.append("sealed_generalization")
    if _ROBUST_CARD_TAGS.intersection(card["tags"]):
        names.append("robustness_challenge")
    if synthetic_demo:
        names.append("distributional_sample")
    return sorted(set(names))


def _case(**fields) -> dict:
    return {
        "case_id": fields["case_id"],
        "runner": fields["runner"],
        "suites": sorted(set(fields["suites"])),
        "critical": bool(fields["critical"]) and "regression" in fields["suites"],
        "tags": sorted(set(fields["tags"])),
        "capabilities": sorted(set(fields["capabilities"])),
        "scenario_id": fields["scenario_id"],
        "complexity": fields["complexity"],
        "lineage_id": fields["lineage_id"],
        "session_id": fields["session_id"],
        "source": {"kind": "synthetic", "authored_at": "2026-01-15", "deidentified": True},
        "split": fields["split"],
        "pipeline_split": fields["split"],
        "catalog_version": CATALOG_VERSION,
        "faults": fields["faults"],
        "sampling": fields["sampling"],
        "synthetic_demo": fields["synthetic_demo"],
        "legacy_purposes": fields["legacy"],
        "expected_predicates": fields.get("expected_predicates") or [],
        "review": None,
        "provenance": {"builder": "eval_catalog", "card_id": fields["case_id"], "source_refs": []},
    }


def _memory_cases() -> list[dict]:
    common = {
        "runner": "memory",
        "faults": [],
        "sampling": None,
        "synthetic_demo": False,
        "complexity": "long_session",
    }
    rows = [
        {
            **common,
            "case_id": "mem-long-correction",
            "suites": ["regression"],
            "critical": True,
            "tags": ["memory", "long_context", "perf_cost", "ca"],
            "capabilities": ["session_state", "correction", "workload", "e2e"],
            "scenario_id": "session_memory",
            "lineage_id": "lin-mem-long",
            "session_id": "sess-mem-long",
            "split": "regression",
            "legacy": ["capability_acceptance", "release_regression", "workload_cost"],
            "expected_predicates": ["confirmed.budget == 13", "tombstone_count >= 1", "coverage_complete"],
        },
        {
            **common,
            "case_id": "mem-seven-day",
            "suites": ["development"],
            "critical": False,
            "tags": ["memory", "robustness"],
            "capabilities": ["recent_memory", "profile"],
            "scenario_id": "recent_memory",
            "lineage_id": "lin-mem-seven",
            "session_id": "sess-mem-seven",
            "split": "dev_challenge",
            "legacy": ["capability_acceptance", "robustness_challenge"],
            "complexity": "store",
        },
        {
            **common,
            "case_id": "mem-isolation",
            "suites": ["development"],
            "critical": False,
            "tags": ["memory", "robustness"],
            "capabilities": ["profile", "recent_memory"],
            "scenario_id": "recent_memory",
            "lineage_id": "lin-mem-iso",
            "session_id": "sess-mem-iso",
            "split": "dev_challenge",
            "legacy": ["capability_acceptance"],
            "complexity": "store",
        },
        {
            **common,
            "case_id": "mem-summary-lag",
            "suites": ["development"],
            "critical": False,
            "tags": ["memory", "long_context", "perf_cost", "robustness"],
            "capabilities": ["session_state", "workload"],
            "scenario_id": "session_memory",
            "lineage_id": "lin-mem-lag",
            "session_id": "sess-mem-lag",
            "split": "dev_challenge",
            "legacy": ["robustness_challenge", "workload_cost"],
        },
        {
            **common,
            "case_id": "mem-cache-restore",
            "suites": ["development"],
            "critical": False,
            "tags": ["memory", "robustness"],
            "capabilities": ["session_state"],
            "scenario_id": "session_memory",
            "lineage_id": "lin-mem-cache",
            "session_id": "sess-mem-cache",
            "split": "dev_challenge",
            "legacy": ["robustness_challenge"],
            "complexity": "store",
        },
    ]
    return [_case(**row) for row in rows]


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
    tag_counts = defaultdict(int)
    overlaps = []
    for case in cases:
        for suite in case["suites"]:
            memberships[suite] += 1
        for tag in case["tags"]:
            tag_counts[tag] += 1
        if len(case["suites"]) > 1:
            overlaps.append(case["case_id"])
    capabilities = set()
    tags = set()
    for case in cases:
        capabilities.update(case["capabilities"])
        tags.update(case["tags"])
    online = [case for case in cases if "online_sample" in case["suites"] and not case.get("synthetic_demo")]
    return {
        "catalog_version": CATALOG_VERSION,
        "unique_case_count": len(unique),
        "membership_counts": dict(sorted(memberships.items())),
        "membership_sum": sum(memberships.values()),
        "overlap_case_ids": sorted(overlaps),
        "tag_counts": dict(sorted(tag_counts.items())),
        "counts_are_not_additive": True,
        "missing_capabilities": [name for name in REQUIRED_CAPABILITIES if name not in capabilities],
        "missing_tags": [name for name in REQUIRED_TAGS if name not in tags],
        "synthetic_demo_count": sum(1 for case in cases if case.get("synthetic_demo")),
        "online_sample_count": len(online),
        "production_sample_count": 0,
        "production_kpi": None,
        "online_metrics": None,
        "production_kpi_reason": "no_deidentified_or_real_samples_loaded",
    }


def select_cases(
    cases: list[dict],
    *,
    purposes: list[str] | None = None,
    suites: list[str] | None = None,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
    critical_only: bool = False,
    synthetic_demo_only: bool = False,
    include_development: bool = True,
) -> list[dict]:
    wanted_suites = set(suites or [])
    wanted_tags = set(tags or [])
    if purposes:
        for name in purposes:
            mapped = LEGACY_PURPOSE_FILTER.get(name)
            if mapped is None:
                raise ValueError("unknown_purpose:" + name)
            if mapped.get("suite"):
                wanted_suites.add(mapped["suite"])
            if mapped.get("tag"):
                wanted_tags.add(mapped["tag"])
            critical_only = critical_only or bool(mapped.get("critical_only"))
            synthetic_demo_only = synthetic_demo_only or bool(mapped.get("synthetic_demo_only"))
    selected = []
    for case in cases:
        if not include_development and case["suites"] == ["development"]:
            continue
        if wanted_suites and not wanted_suites.intersection(case["suites"]):
            continue
        if wanted_tags and not wanted_tags.intersection(case["tags"]):
            continue
        if case_ids and case["case_id"] not in case_ids:
            continue
        if critical_only and not case["critical"]:
            continue
        if synthetic_demo_only and not case.get("synthetic_demo"):
            continue
        selected.append(case)
    return list({case["case_id"]: case for case in selected}.values())


def run_eval_catalog(
    purposes: list[str] | None = None,
    *,
    include_development: bool = True,
    suites: list[str] | None = None,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
    critical_only: bool = False,
    repeat: int = 1,
    baseline: dict | None = None,
    gate_config: dict | None = None,
) -> dict:
    if repeat < 1:
        raise ValueError("repeat")
    cases = build_catalog()
    ordered = select_cases(
        cases,
        purposes=purposes,
        suites=suites,
        tags=tags,
        case_ids=case_ids,
        critical_only=critical_only,
        include_development=include_development,
    )
    trials = []
    for _ in range(repeat):
        trials.append([_execute(case) for case in ordered])
    results = _merge_trials(trials)
    by_suite = {}
    for suite in SUITES:
        members = [item for item in results if suite in item["suites"]]
        if members:
            by_suite[suite] = _suite_summary(suite, members)
    by_tag = {}
    for tag in REQUIRED_TAGS:
        members = [item for item in results if tag in item["tags"]]
        if members:
            by_tag[tag] = _pass_summary(members)
    critical = [item for item in results if item["critical"] and "regression" in item["suites"]]
    release_passed = all(item["passed"] and not item["unknown"] for item in critical) if critical else None
    gates = apply_suite_gates(results, baseline, gate_config) if gate_config is not None else None
    return {
        "catalog_version": CATALOG_VERSION,
        "snapshot_id": CATALOG_SNAPSHOT_ID,
        "kb_version": KB_VERSION,
        "evaluation_kind": "mock_demo",
        "unique_executed": len(results),
        "matrix": coverage_matrix(cases),
        "suites": by_suite,
        "tags": by_tag,
        "results": results,
        "release_passed": release_passed,
        "critical_failure_ids": [item["case_id"] for item in critical if not item["passed"] or item["unknown"]],
        "gates": gates,
        "weighted_aggregate": {"value": None, "reason": "weights_unavailable_or_unvalidated"},
        "repeat_trials": repeat,
        "significance_test": None,
        "judge": {"available": False, "score": None, "reason": "judge_not_configured"},
        "production_kpi": None,
        "online_metrics": None,
        "production_release": False,
        "notes": [
            "Suite memberships overlap. Do not add them into a sample size.",
            "Behavioral checks use outcome constraints, not one exact tool path.",
            "Synthetic demos are not an online sample. production_kpi and online metrics stay null.",
            "Promote into regression only with review metadata, expected predicates, and a new catalog version.",
        ],
    }


def _merge_trials(trials: list[list[dict]]) -> list[dict]:
    if not trials:
        return []
    merged = []
    for index, first in enumerate(trials[0]):
        passes = [trial[index]["passed"] for trial in trials]
        unknown = any(trial[index]["unknown"] for trial in trials)
        row = dict(first)
        row["passed"] = all(passes) and not unknown
        row["unknown"] = unknown
        row["trial_pass_count"] = sum(1 for item in passes if item)
        row["trial_count"] = len(passes)
        merged.append(row)
    return merged


def apply_suite_gates(results: list[dict], baseline: dict | None, gate_config: dict | None) -> dict:
    """Three release gates. A critical failure is not offset by other gains."""
    config = gate_config or {}
    regression = [item for item in results if "regression" in item["suites"]]
    critical = [item for item in regression if item.get("critical")]
    critical_unknown = [item["case_id"] for item in critical if item.get("unknown")]
    critical_failed = [item["case_id"] for item in critical if not item.get("unknown") and not item.get("passed")]
    if not critical:
        critical_status = "unknown"
    elif critical_failed:
        critical_status = "fail"
    elif critical_unknown:
        critical_status = "unknown"
    else:
        critical_status = "pass"

    version_ok = _same_snapshot(baseline, config)
    core = _core_gate(regression, baseline, config, version_ok)
    cost = _cost_gate(regression, config)
    statuses = [critical_status, core["status"], cost["status"]]
    if "fail" in statuses:
        overall = "fail"
    elif "unknown" in statuses or "unavailable" in statuses:
        overall = "unknown"
    else:
        overall = "pass"
    return {
        "status": overall,
        "passed": overall == "pass",
        "offline_gray_eligible": overall == "pass",
        "production_release": False,
        "production_release_reason": "no_real_online_sample",
        "critical": {"status": critical_status, "failed_case_ids": critical_failed, "unknown_case_ids": critical_unknown},
        "core_regression": core,
        "latency_cost": cost,
        "snapshot_match": version_ok,
        "significance_test": None,
    }


def _same_snapshot(baseline: dict | None, config: dict) -> bool:
    if baseline is None:
        return config.get("allow_missing_baseline") is True
    if baseline.get("catalog_version") not in {None, CATALOG_VERSION, config.get("catalog_version")}:
        return False
    expected_snapshot = config.get("snapshot_id") or CATALOG_SNAPSHOT_ID
    if baseline.get("snapshot_id") not in {None, expected_snapshot}:
        return False
    return True


def _core_gate(regression: list[dict], baseline: dict | None, config: dict, version_ok: bool) -> dict:
    known = [item for item in regression if not item.get("unknown")]
    if not known or not version_ok:
        return {"status": "unknown", "pass_rate": None, "reason": "missing_comparable_results"}
    pass_rate = sum(1 for item in known if item.get("passed")) / len(known)
    base_rate = _baseline_pass_rate(baseline)
    drop = config.get("allowed_pass_rate_drop")
    floor = config.get("core_regression_min_pass_rate")
    rate_status = "unknown"
    if base_rate is None and floor is None:
        rate_status = "unknown"
        reason = "tolerance_not_configured"
    elif base_rate is not None and drop is None:
        rate_status = "unknown"
        reason = "tolerance_not_configured"
    else:
        reason = "within_tolerance"
        rate_status = "pass"
        if floor is not None and pass_rate < float(floor):
            rate_status = "fail"
            reason = "below_floor"
        if base_rate is not None and drop is not None and pass_rate < base_rate - float(drop):
            rate_status = "fail"
            reason = "below_baseline_tolerance"
    target = _target_issue_gate(regression, baseline, config.get("target_issue_ids") or [])
    if target["status"] == "fail" or rate_status == "fail":
        status = "fail"
    elif target["status"] == "unknown" or rate_status == "unknown":
        status = "unknown"
    else:
        status = "pass"
    return {
        "status": status,
        "pass_rate": pass_rate,
        "baseline_pass_rate": base_rate,
        "reason": reason,
        "target_issue": target,
    }


def _baseline_pass_rate(baseline: dict | None):
    if not baseline:
        return None
    rows = [item for item in baseline.get("results") or [] if "regression" in (item.get("suites") or []) and not item.get("unknown")]
    if not rows:
        metrics = (baseline.get("metrics") or {}).get("success_rate")
        return metrics
    return sum(1 for item in rows if item.get("passed")) / len(rows)


def _target_issue_gate(regression: list[dict], baseline: dict | None, issue_ids: list[str]) -> dict:
    if not issue_ids:
        return {"status": "pass", "reason": "no_target_selected", "issues": []}
    by_id = {item["case_id"]: item for item in regression}
    base_rows = {item["case_id"]: item for item in (baseline or {}).get("results") or []}
    issues = []
    status = "pass"
    for issue_id in issue_ids:
        current = by_id.get(issue_id)
        before = base_rows.get(issue_id)
        if current is None or current.get("unknown") or before is None or before.get("unknown"):
            issues.append({"case_id": issue_id, "status": "unknown"})
            status = "unknown" if status != "fail" else status
            continue
        resolved = (not before.get("passed")) and current.get("passed")
        issues.append({"case_id": issue_id, "status": "pass" if resolved else "fail"})
        if not resolved:
            status = "fail"
    return {"status": status, "reason": "selected_issue", "issues": issues}


def _cost_gate(regression: list[dict], config: dict) -> dict:
    latency_budget = config.get("latency_budget_ms")
    token_budget = config.get("token_budget")
    cost_budget = config.get("cost_budget")
    if latency_budget is None and token_budget is None and cost_budget is None:
        return {
            "status": "not_configured",
            "latency_ms_max": None,
            "token_usage": None,
            "cost": None,
            "p95_latency_ms": None,
            "reason": "budgets_not_configured",
        }
    latencies = [item.get("latency_ms") for item in regression]
    tokens = [item.get("token_usage") for item in regression]
    if latency_budget is not None and (not latencies or any(value is None for value in latencies)):
        return {"status": "unavailable", "reason": "latency_unavailable", "p95_latency_ms": None, "token_usage": None, "cost": None}
    if (token_budget is not None or cost_budget is not None) and any(value is None for value in tokens):
        return {"status": "unavailable", "reason": "token_or_cost_unavailable", "p95_latency_ms": None, "token_usage": None, "cost": None}
    max_latency = max(latencies) if latencies and all(value is not None for value in latencies) else None
    if latency_budget is not None and max_latency is not None and max_latency > float(latency_budget):
        return {"status": "fail", "reason": "latency_budget", "latency_ms_max": max_latency, "p95_latency_ms": None, "token_usage": None, "cost": None}
    return {
        "status": "pass",
        "reason": "offline_proxy_within_budget",
        "latency_ms_max": max_latency,
        "token_usage": None,
        "cost": None,
        "p95_latency_ms": None,
    }


def _execute(case: dict) -> dict:
    if case["runner"] == "card":
        card = next(item for item in all_cards() if item["card_id"] == case["case_id"])
        evaluated = evaluate_card(card)
        score = evaluated["score"]
        trace = evaluated["trace"]
        return _result(
            case,
            score["passed"],
            score["failures"],
            trace.get("latency_ms"),
            score["tool_call_count"],
            trace.get("token_usage"),
            trace.get("token_usage_reason") or "unavailable_for_mock",
            False,
        )
    failures = _run_memory_case(case["case_id"])
    return _result(case, not failures, failures, None, None, None, "unavailable_for_memory_case", False)


def _result(case, passed, failures, latency, tool_calls, token_usage, token_reason, unknown) -> dict:
    modules = sorted({_module_for(item.get("check")) for item in failures})
    return {
        "case_id": case["case_id"],
        "suites": case["suites"],
        "critical": case["critical"],
        "tags": case["tags"],
        "capabilities": case["capabilities"],
        "split": case["split"],
        "passed": passed,
        "unknown": unknown,
        "failures": failures,
        "localized_modules": modules,
        "latency_ms": latency,
        "latency_kind": "offline_proxy" if latency is not None else "unavailable",
        "tool_call_count": tool_calls,
        "token_usage": token_usage,
        "token_usage_reason": token_reason,
        "sampling": case["sampling"],
        "synthetic_demo": case.get("synthetic_demo", False),
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


def _suite_summary(suite: str, results: list[dict]) -> dict:
    summary = _pass_summary(results)
    summary["description"] = SUITES[suite]
    summary["critical_count"] = sum(1 for item in results if item.get("critical"))
    summary["critical_failed_case_ids"] = [item["case_id"] for item in results if item.get("critical") and not item["passed"]]
    return summary


def _pass_summary(results: list[dict]) -> dict:
    strata = defaultdict(list)
    for result in results:
        sampling = result.get("sampling") or {}
        strata[sampling.get("stratum") or "unspecified"].append(result)
    return {
        "case_count": len(results),
        "passed": sum(1 for item in results if item["passed"] and not item["unknown"]),
        "unknown_case_ids": [item["case_id"] for item in results if item["unknown"]],
        "failed_case_ids": [item["case_id"] for item in results if not item["passed"] and not item["unknown"]],
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
    schema = document.get("schema")
    if schema not in {"deidentified-sample-v1", "deidentified-session-v1"}:
        raise ValueError("sample_schema")
    if document.get("production_kpi") is not None or document.get("online_metrics") is not None:
        raise ValueError("production_kpi_not_allowed_without_a_real_sample_pipeline")
    records = document.get("records") or []
    for record in records:
        _reject_online_record(record, require_session=schema == "deidentified-session-v1")
    return {
        "accepted": len(records),
        "production_kpi": None,
        "online_metrics": None,
        "sample_count": len(records),
        "suite": "online_sample" if schema == "deidentified-session-v1" else None,
    }


def archive_online_batch(document: dict, *, archived_at: str) -> dict:
    if document.get("schema") not in {"deidentified-session-v1", "online-batch-v1"}:
        raise ValueError("sample_schema")
    archived = {
        "schema": "online-batch-v1",
        "status": "archived",
        "archived_at": archived_at,
        "batch_id": document.get("batch_id"),
        "window": document.get("window"),
        "sampling": document.get("sampling"),
        "records": list(document.get("records") or []),
        "production_kpi": None,
        "online_metrics": None,
    }
    return archived


def copy_failure_to_development(record: dict) -> dict:
    """Copy one online failure into development. This does not promote it."""
    _reject_online_record(record, require_session=True)
    lineage = record.get("lineage_id")
    if not lineage:
        raise ValueError("lineage_id")
    source_refs = list((record.get("source") or {}).get("refs") or [])
    if not source_refs:
        raise ValueError("source_refs")
    return {
        "case_id": "dev-" + str(record.get("case_id") or lineage),
        "suites": ["development"],
        "critical": False,
        "tags": sorted(set(record.get("tags") or ["robustness"])),
        "lineage_id": lineage,
        "session_id": record.get("session_id"),
        "split": "dev_challenge",
        "pipeline_split": "dev_challenge",
        "catalog_version": CATALOG_VERSION,
        "source": record.get("source"),
        "expected_predicates": [],
        "review": None,
        "provenance": {"builder": "online_failure_copy", "source_refs": source_refs},
        "training_row": False,
    }


def promote_to_regression(case: dict, review: dict, *, new_version: str) -> dict:
    """Promote one reviewed case. Other variants stay where they are."""
    if review.get("decision") != "promote":
        raise ValueError("review_decision")
    for key in ("reviewer", "reviewed_at", "expected_predicates"):
        if not review.get(key):
            raise ValueError("review_" + key)
    predicates = review["expected_predicates"]
    if not isinstance(predicates, list) or not predicates:
        raise ValueError("expected_predicates")
    if review.get("catalog_version") != new_version or new_version == case.get("catalog_version"):
        raise ValueError("new_version")
    previous = list(case.get("expected_predicates") or [])
    if previous and list(predicates) != previous and review.get("predicates_validated") is not True:
        raise ValueError("expected_behavior_change")
    source_refs = list(review.get("source_refs") or (case.get("provenance") or {}).get("source_refs") or [])
    if not case.get("lineage_id"):
        raise ValueError("lineage_id")
    promoted = dict(case)
    promoted["suites"] = sorted(set(case.get("suites") or []) | {"regression"})
    promoted["critical"] = bool(review.get("critical", False))
    promoted["catalog_version"] = new_version
    promoted["expected_predicates"] = list(predicates)
    promoted["review"] = {
        "reviewer": review["reviewer"],
        "reviewed_at": review["reviewed_at"],
        "source_refs": source_refs,
        "lineage_id": case.get("lineage_id"),
    }
    promoted["provenance"] = dict(case.get("provenance") or {})
    promoted["provenance"]["source_refs"] = source_refs
    promoted["training_row"] = False
    return promoted


def dedup_cases(cases: list[dict]) -> list[dict]:
    ordered: dict[str, dict] = {}
    for case in cases:
        current = ordered.get(case["case_id"])
        if current is None:
            ordered[case["case_id"]] = case
            continue
        merged = dict(current)
        merged["suites"] = sorted(set(current.get("suites") or []) | set(case.get("suites") or []))
        merged["tags"] = sorted(set(current.get("tags") or []) | set(case.get("tags") or []))
        ordered[case["case_id"]] = merged
    return list(ordered.values())


def blocked_training_session_ids(cases: list[dict]) -> set[str]:
    """Regression, sealed, and online sessions are not training rows."""
    blocked = set()
    for case in cases:
        if "regression" in case.get("suites", []) or "online_sample" in case.get("suites", []) or case.get("split") in {"regression", "sealed"}:
            if case.get("session_id"):
                blocked.add(case["session_id"])
    return blocked


def _reject_online_record(record: dict, *, require_session: bool) -> None:
    source = record.get("source") or {}
    if source.get("kind") != "deidentified_sample" or source.get("deidentified") is not True:
        raise ValueError("source_kind")
    blob = json.dumps(record)
    for banned in ("@", "api_key", "employer"):
        if banned in blob.lower():
            raise ValueError("sample_rejected")
    if require_session:
        for key in ("session", "tool_evidence", "window", "sampling"):
            if key not in record:
                raise ValueError("missing_" + key)


def render_catalog_markdown(report: dict) -> str:
    lines = [
        "# Evaluation catalog",
        "",
        f"Version: {report['catalog_version']}",
        f"Unique cases executed: {report['unique_executed']}",
        f"Critical regression passed: {report['release_passed']}",
        f"Production release: {report.get('production_release')}",
        f"Weighted aggregate: {report['weighted_aggregate']['reason']}",
        "",
        "Suite memberships overlap. Do not add them into a sample size.",
        "",
    ]
    matrix = report["matrix"]
    lines.append(f"Unique catalog cases: {matrix['unique_case_count']}")
    lines.append(f"Missing tags: {', '.join(matrix.get('missing_tags') or []) or 'none'}")
    lines.append(f"Online samples: {matrix.get('online_sample_count')}. Synthetic demos are separate and are not a traffic score.")
    lines.append("")
    for suite, summary in report.get("suites", {}).items():
        lines.append(f"## {suite}")
        lines.append("")
        lines.append(summary["description"])
        lines.append(f"Cases: {summary['case_count']}. Passed: {summary['passed']}.")
        if summary["failed_case_ids"]:
            lines.append("Failed: " + ", ".join(summary["failed_case_ids"]))
        lines.append("")
    if report.get("tags"):
        lines.append("## Tags")
        lines.append("")
        for tag, summary in report["tags"].items():
            lines.append(f"- {tag}: {summary['passed']}/{summary['case_count']}")
        lines.append("")
    lines.append("Production KPI: null. Online metrics: null.")
    lines.append("")
    return "\n".join(lines)
