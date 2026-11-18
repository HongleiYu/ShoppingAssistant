"""Compare a candidate report with a baseline and apply explicit gates."""

from __future__ import annotations


def aggregate(cases: list[dict]) -> dict:
    total = len(cases)
    if total == 0:
        return {
            "case_count": 0,
            "success_rate": None,
            "unsupported_claim_count": 0,
            "unnecessary_planner_count": 0,
            "hard_constraint_violation_count": 0,
            "schema_pass_rate": None,
        }
    passed = sum(1 for case in cases if case.get("passed"))
    schema_ok = sum(1 for case in cases if case.get("schema_ok", True))
    return {
        "case_count": total,
        "success_rate": passed / total,
        "unsupported_claim_count": sum(int(case.get("unsupported_claim_count") or 0) for case in cases),
        "unnecessary_planner_count": sum(1 for case in cases if case.get("unnecessary_planner")),
        "hard_constraint_violation_count": sum(int(case.get("hard_constraint_violations") or 0) for case in cases),
        "schema_pass_rate": schema_ok / total,
    }


def apply_gates(metrics: dict, gates: list[dict]) -> list[dict]:
    results = []
    for gate in gates:
        if not gate.get("enabled", True):
            results.append({"id": gate["id"], "status": "skipped", "passed": True, "critical": bool(gate.get("critical"))})
            continue
        actual = metrics.get(gate["metric"]) if "metric" in gate else None
        if actual is None:
            results.append(
                {
                    "id": gate["id"],
                    "status": "unavailable",
                    "passed": False,
                    "critical": bool(gate.get("critical")),
                    "reason": "metric_unavailable",
                }
            )
            continue
        op = gate.get("op", "==")
        expected = gate.get("value")
        ok = _compare(actual, op, expected)
        results.append(
            {
                "id": gate["id"],
                "status": "pass" if ok else "fail",
                "passed": ok,
                "critical": bool(gate.get("critical")),
                "actual": actual,
                "expected": expected,
                "op": op,
            }
        )
    return results


def compare_reports(baseline: dict | None, candidate: dict | None, gate_config: dict) -> dict:
    if baseline is None:
        return {
            "status": "blocked_missing_baseline",
            "passed": False,
            "wins": [],
            "regressions": [],
            "unchanged": [],
            "gates": [],
            "mock_fallback": False,
        }
    if candidate is None:
        return {
            "status": "blocked_missing_candidate",
            "passed": False,
            "wins": [],
            "regressions": [],
            "unchanged": [],
            "gates": [],
        }
    base_cases = {case["case_id"]: case for case in baseline.get("cases") or []}
    cand_cases = {case["case_id"]: case for case in candidate.get("cases") or []}
    wins, regressions, unchanged = [], [], []
    for case_id in sorted(set(base_cases) | set(cand_cases)):
        before = base_cases.get(case_id)
        after = cand_cases.get(case_id)
        if before is None or after is None:
            regressions.append({"case_id": case_id, "reason": "missing_case"})
            continue
        if _better(before, after):
            wins.append({"case_id": case_id, "scenario_id": after.get("scenario_id")})
        elif _worse(before, after):
            regressions.append({"case_id": case_id, "scenario_id": after.get("scenario_id")})
        else:
            unchanged.append(case_id)
    metrics = aggregate(list(cand_cases.values()))
    gate_results = apply_gates(metrics, gate_config.get("gates") or [])
    critical_fail = any(item["critical"] and not item["passed"] for item in gate_results)
    passed = not regressions and not critical_fail and all(item["passed"] for item in gate_results)
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "threshold_profile": gate_config.get("threshold_profile"),
        "evaluation_kind": candidate.get("evaluation_kind"),
        "wins": wins,
        "regressions": regressions,
        "unchanged_count": len(unchanged),
        "gates": gate_results,
        "metrics": metrics,
        "identifiers": {
            "baseline": baseline.get("identifiers"),
            "candidate": candidate.get("identifiers"),
        },
    }


def degrade_report(report: dict) -> dict:
    """Intentionally bad candidate used to prove gates can fail."""
    import copy

    bad = copy.deepcopy(report)
    if not bad.get("cases"):
        bad["cases"] = []
    if bad["cases"]:
        bad["cases"][0]["passed"] = False
        bad["cases"][0]["unsupported_claim_count"] = int(bad["cases"][0].get("unsupported_claim_count") or 0) + 2
        bad["cases"][0]["unnecessary_planner"] = True
    bad["evaluation_kind"] = "intentionally_bad_candidate"
    return bad


def _better(before: dict, after: dict) -> bool:
    return (not before.get("passed") and after.get("passed")) or (
        int(after.get("unsupported_claim_count") or 0) < int(before.get("unsupported_claim_count") or 0)
    )


def _worse(before: dict, after: dict) -> bool:
    if before.get("passed") and not after.get("passed"):
        return True
    if int(after.get("unsupported_claim_count") or 0) > int(before.get("unsupported_claim_count") or 0):
        return True
    if int(after.get("hard_constraint_violations") or 0) > int(before.get("hard_constraint_violations") or 0):
        return True
    if after.get("unnecessary_planner") and not before.get("unnecessary_planner"):
        return True
    return False


def _compare(actual, op: str, expected) -> bool:
    if op == "==":
        return actual == expected
    if op == ">=":
        return actual >= expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == "<":
        return actual < expected
    raise ValueError(op)
