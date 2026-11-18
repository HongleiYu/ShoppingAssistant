"""Build mock-demo evaluation reports. This does not call a model endpoint."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from shopassist import __version__
from shopassist.cards import all_cards
from shopassist.catalog import catalog_document
from shopassist.config import load_config
from shopassist.eval.judge import judge_status
from shopassist.harness import evaluate_card
from shopassist.knowledge import kb_document
from shopassist.redact import scrub_object
from shopassist.skills.registry import build_default_registry
from shopassist.versioning import (
    CATALOG_SNAPSHOT_ID,
    DATA_VERSION,
    KB_VERSION,
    PROMPT_CA,
    PROMPT_PA,
    SCHEMA_CA,
    SCHEMA_PA,
    SCHEMA_REPORT,
    TOOL_CONTRACT_VERSION,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sha(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _code_version() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def identifiers() -> dict:
    cfg = load_config()
    return {
        "package_version": __version__,
        "code_version": _code_version(),
        "prompt_ca": PROMPT_CA,
        "prompt_pa": PROMPT_PA,
        "schema_ca": SCHEMA_CA,
        "schema_pa": SCHEMA_PA,
        "catalog_snapshot_id": CATALOG_SNAPSHOT_ID,
        "kb_version": KB_VERSION,
        "catalog_sha256": _sha(catalog_document()),
        "kb_sha256": _sha(kb_document()),
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "skill_versions": build_default_registry().versions(),
        "data_version": DATA_VERSION,
        "model_ids": {"ca": None, "pa": None, "judge": None},
        "adapter_ids": {"ca": None, "pa": None},
        "credentials_used": False,
        "credentials_configured": bool(cfg["ca"].get("credential_present") or cfg["pa"].get("credential_present")),
        "mode": "mock",
    }


def case_from_result(result: dict) -> dict:
    score = result["score"]
    trace = result["trace"]
    return {
        "case_id": result["card_id"],
        "scenario_id": result["scenario_id"],
        "split": result["split"],
        "passed": score["passed"],
        "unsupported_claim_count": score["unsupported_claim_count"],
        "hard_constraint_violations": score["hard_constraint_violations"],
        "unnecessary_planner": score["unnecessary_planner"],
        "memory_consistent": score["memory_consistent"],
        "schema_ok": score["schema_ok"],
        "tool_call_count": score["tool_call_count"],
        "loop_count": trace.get("loop_count"),
        "token_usage": None,
        "token_usage_reason": "unavailable_for_mock",
        "latency_ms": trace.get("latency_ms"),
        "failures": score["failures"],
    }


def build_report(splits: set[str] | None = None) -> dict:
    selected = [card for card in all_cards() if splits is None or card["split"] in splits]
    results = [evaluate_card(card) for card in selected]
    cases = [case_from_result(result) for result in results]
    regression = [case for case in cases if case["split"] == "regression"]
    by_scenario: dict[str, dict] = {}
    for case in cases:
        row = by_scenario.setdefault(case["scenario_id"], {"cases": 0, "passed": 0, "unsupported_claim_count": 0})
        row["cases"] += 1
        row["passed"] += 1 if case["passed"] else 0
        row["unsupported_claim_count"] += case["unsupported_claim_count"]
    report = {
        "report_version": SCHEMA_REPORT,
        "evaluation_kind": "mock_demo",
        "mode": "mock",
        "threshold_profile": "demonstration_not_production",
        "gpu_training_verified": False,
        "identifiers": identifiers(),
        "judge": judge_status(False),
        "counts": {
            "cases": len(cases),
            "passed": sum(1 for case in cases if case["passed"]),
            "regression_cases": len(regression),
            "regression_passed": sum(1 for case in regression if case["passed"]),
        },
        "scenarios": by_scenario,
        "cases": cases,
        "traces_available": True,
        "notes": [
            "Mock demo report. It is not a configured-model evaluation.",
            "Token usage is unavailable for the reference runtime.",
            "The corpus is a demonstration and is not production training sufficiency.",
        ],
    }
    cleaned, redactions = scrub_object(report)
    cleaned["redaction_count"] = redactions
    return cleaned


def render_markdown(report: dict) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"Kind: {report.get('evaluation_kind')}",
        f"Mode: {report.get('mode')}",
        f"Threshold profile: {report.get('threshold_profile')}",
        "",
        "## Counts",
        "",
        f"- Cases: {report['counts']['cases']}",
        f"- Passed: {report['counts']['passed']}",
        f"- Regression cases: {report['counts']['regression_cases']}",
        f"- Regression passed: {report['counts']['regression_passed']}",
        "",
        "## Scenarios",
        "",
        "| Scenario | Cases | Passed | Unsupported claims |",
        "| --- | --- | --- | --- |",
    ]
    for name, row in sorted(report.get("scenarios", {}).items()):
        lines.append(f"| {name} | {row['cases']} | {row['passed']} | {row['unsupported_claim_count']} |")
    lines.extend(["", "## Cases", ""])
    for case in report.get("cases") or []:
        mark = "pass" if case["passed"] else "fail"
        lines.append(f"- {case['case_id']} ({case['split']}): {mark}")
        for failure in case.get("failures") or []:
            lines.append(f"  - {failure.get('check')}: {failure.get('detail')}")
    lines.extend(
        [
            "",
            "## Not measured",
            "",
            "- Judge score: unavailable (judge not configured).",
            "- Token usage: unavailable for the mock runtime.",
            "- Optimizer and GPU training: not run.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
