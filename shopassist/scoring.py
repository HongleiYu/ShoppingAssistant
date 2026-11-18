"""Outcome scoring from observed traces and independent expectations."""

from __future__ import annotations

import re

_INT = re.compile(r"\d+")
_PRODUCT = re.compile(r"prod-[a-z0-9-]+")
_SKU = re.compile(r"SKU-[A-Z0-9-]+")


def score_trace(expected: dict, trace: dict) -> dict:
    failures = []
    turns = trace.get("turns") or []
    routes = [turn.get("route") for turn in turns]
    if "routes" in expected and routes != expected["routes"]:
        failures.append({"check": "route", "detail": {"observed": routes, "expected": expected["routes"]}})
    planner_turns = [index for index, turn in enumerate(turns) if turn.get("planner_invoked")]
    if "planner_turns" in expected and planner_turns != expected["planner_turns"]:
        failures.append({"check": "planner_turns", "detail": {"observed": planner_turns}})
    unnecessary = False
    for index, turn in enumerate(turns):
        if expected.get("planner_forbidden_turns") and index in expected["planner_forbidden_turns"] and turn.get("planner_invoked"):
            unnecessary = True
            failures.append({"check": "unnecessary_planner", "turn_index": index, "detail": "planner_invoked"})
    if trace.get("active_task_id_after") is not None:
        failures.append({"check": "task_lifecycle", "detail": "active_task_still_set"})
    task_ids = trace.get("task_ids") or []
    if len(task_ids) != len(set(task_ids)):
        failures.append({"check": "task_lifecycle", "detail": "duplicate_task_id"})
    if "task_count" in expected and len(task_ids) != expected["task_count"]:
        failures.append({"check": "task_count", "detail": {"observed": len(task_ids)}})

    claims = [claim for turn in turns for claim in turn.get("claims") or []]
    evidence = [item for turn in turns for item in turn.get("evidence") or []]
    unsupported = [claim for claim in claims if not claim_supported(claim, evidence)]
    if unsupported:
        failures.append(
            {
                "check": "unsupported_claim",
                "detail": [_claim_brief(claim) for claim in unsupported[:8]],
            }
        )
    for fact in expected.get("expected_facts") or []:
        if not _has_supported_fact(fact, claims, evidence):
            failures.append({"check": "missing_fact", "detail": fact})
    def presented_ids(claim_rows: list[dict]) -> set[str]:
        found = {
            claim.get("product_id")
            for claim in claim_rows
            if claim.get("product_id")
            and claim.get("field") in {"price", "stock", "material", "warranty_months", "color", "category"}
        }
        found.discard(None)
        return found

    product_turns = [turn for turn in turns if presented_ids(turn.get("claims") or [])]
    presented = presented_ids(product_turns[-1].get("claims") or []) if product_turns else set()
    if "exact_product_ids" in expected and presented != set(expected["exact_product_ids"]):
        failures.append(
            {
                "check": "product_set",
                "detail": {"observed": sorted(presented), "expected": sorted(expected["exact_product_ids"])},
            }
        )
    if "turn_product_ids" in expected:
        for index, ids in enumerate(expected["turn_product_ids"]):
            if ids is None:
                continue
            observed = presented_ids(turns[index].get("claims") or []) if index < len(turns) else set()
            if observed != set(ids):
                failures.append({"check": "turn_product_set", "turn_index": index, "detail": sorted(observed)})
    limitation_codes = {
        item.get("code") for turn in turns for item in turn.get("limitations") or [] if item.get("code")
    }
    for code in expected.get("limitation_codes") or []:
        if code not in limitation_codes:
            failures.append({"check": "limitation", "detail": code})
    if expected.get("escalated") and not any(turn.get("escalated") for turn in turns):
        failures.append({"check": "escalation", "detail": "not_escalated"})
    if expected.get("escalated") is False and any(turn.get("escalated") for turn in turns):
        failures.append({"check": "escalation", "detail": "unexpected_escalation"})
    strategies = [item.get("strategy") for turn in turns for item in turn.get("recovery") or []]
    for strategy in expected.get("recovery") or []:
        if strategy not in strategies:
            failures.append({"check": "recovery", "detail": strategy})
    tool_statuses = [tool.get("status") for turn in turns for tool in turn.get("tools") or []]
    for status in expected.get("tool_statuses") or []:
        if status not in tool_statuses:
            failures.append({"check": "tool_status", "detail": status})
    tool_count = sum(len(turn.get("tools") or []) for turn in turns)
    if "min_tool_calls" in expected and tool_count < expected["min_tool_calls"]:
        failures.append({"check": "tool_count", "detail": tool_count})
    if "max_tool_calls" in expected and tool_count > expected["max_tool_calls"]:
        failures.append({"check": "tool_count", "detail": tool_count})
    for turn in turns:
        for tool in turn.get("tools") or []:
            filters = (tool.get("arguments") or {}).get("filters") or {}
            for banned in expected.get("filters_must_exclude") or []:
                if banned in filters:
                    failures.append({"check": "assumption_filter", "detail": banned})
            if "max_price" in filters and filters.get("price_op") != expected.get("price_op", "lt"):
                if expected.get("enforce_price_op", True) and "max_price" in filters:
                    failures.append({"check": "price_op", "detail": filters.get("price_op")})
    memory = trace.get("memory") or {}
    confirmed = {item["field"]: item.get("value") for item in memory.get("confirmed_constraints") or []}
    assumptions = {item["field"]: item.get("value") for item in memory.get("assumptions") or []}
    if "final_confirmed" in expected and confirmed != expected["final_confirmed"]:
        failures.append({"check": "memory_confirmed", "detail": {"observed": confirmed}})
    if "final_assumptions" in expected and assumptions != expected["final_assumptions"]:
        failures.append({"check": "memory_assumptions", "detail": {"observed": assumptions}})
    if "memory_after" in expected:
        for index, wanted in enumerate(expected["memory_after"]):
            if index >= len(turns):
                failures.append({"check": "memory_after", "turn_index": index, "detail": "missing_turn"})
                continue
            observed = {
                item["field"]: item.get("value")
                for item in (turns[index].get("memory") or {}).get("confirmed_constraints") or []
            }
            if observed != wanted:
                failures.append({"check": "memory_after", "turn_index": index, "detail": observed})
    reply_text = "\n".join(turn.get("reply_text") or "" for turn in turns)
    for term in expected.get("forbidden_reply_terms") or []:
        if term.lower() in reply_text.lower():
            failures.append({"check": "forbidden_term", "detail": term})
    literal_issues = reply_literal_issues(reply_text, claims, evidence, turns, memory)
    failures.extend(literal_issues)
    hard = _hard_constraints(turns)
    failures.extend(hard)
    passed = not failures
    return {
        "passed": passed,
        "failures": failures,
        "unsupported_claim_count": len(unsupported),
        "hard_constraint_violations": len(hard),
        "unnecessary_planner": unnecessary,
        "planner_invoked": bool(planner_turns),
        "tool_call_count": tool_count,
        "escalated": any(turn.get("escalated") for turn in turns),
        "memory_consistent": not any(item["check"].startswith("memory") for item in failures),
        "schema_ok": all(turn.get("schema_ok", True) for turn in turns),
    }


def claim_supported(claim: dict, evidence: list[dict]) -> bool:
    if claim.get("freshness") not in (None, "current"):
        return False
    ids = claim.get("evidence_ids") or []
    if not ids:
        return False
    for evidence_id in ids:
        found = next((item for item in evidence if item.get("evidence_id") == evidence_id), None)
        if found is None or found.get("status") != "ok":
            return False
        if _matches(found.get("payload"), claim):
            return True
    return False


def _matches(node, claim) -> bool:
    if isinstance(node, list):
        return any(_matches(item, claim) for item in node)
    if not isinstance(node, dict):
        return False
    if node.get("status") == "conflict":
        return False
    facts = node.get("facts")
    if isinstance(facts, dict) and claim.get("field") in facts:
        if facts[claim["field"]] == claim.get("value") and _identity_ok(node, claim):
            if node.get("freshness", "current") != "current":
                return False
            return True
    if claim.get("field") in node and node[claim["field"]] == claim.get("value") and _identity_ok(node, claim):
        if claim["field"] in {"price", "stock"}:
            if node.get("price_supported") is False:
                return False
            if node.get("freshness", "current") != "current":
                return False
        return True
    return any(_matches(value, claim) for value in node.values())


def _identity_ok(node: dict, claim: dict) -> bool:
    if claim.get("product_id") and "product_id" in node and node.get("product_id") != claim.get("product_id"):
        return False
    if claim.get("doc_id") and "doc_id" in node and node.get("doc_id") != claim.get("doc_id"):
        return False
    if claim.get("product_id") and "product_id" not in node and "facts" not in node and claim.get("field") in node:
        # A bare field on a parent envelope is not item-specific.
        if claim["field"] in node and "name" not in node:
            return False
    return True


def _has_supported_fact(fact: dict, claims: list[dict], evidence: list[dict]) -> bool:
    for claim in claims:
        if claim.get("field") != fact.get("field") or claim.get("value") != fact.get("value"):
            continue
        if fact.get("product_id") and claim.get("product_id") != fact.get("product_id"):
            continue
        if fact.get("doc_id") and claim.get("doc_id") != fact.get("doc_id"):
            continue
        if claim_supported(claim, evidence):
            return True
    return False


def reply_literal_issues(reply: str, claims, evidence, turns, memory) -> list[dict]:
    issues = []
    names = sorted(_collect_names(evidence), key=len, reverse=True)
    stripped = reply
    for name in names:
        stripped = stripped.replace(name, " ")
    for product_id in _PRODUCT.findall(reply):
        if not any(claim.get("product_id") == product_id for claim in claims):
            issues.append({"check": "unsupported_literal", "detail": product_id})
    for sku in _SKU.findall(reply):
        if not any(claim.get("value") == sku or sku in str(claim.get("value")) for claim in claims):
            issues.append({"check": "unsupported_literal", "detail": sku})
    allowed = set()
    for claim in claims:
        if isinstance(claim.get("value"), int) and claim_supported(claim, evidence):
            allowed.add(str(claim["value"]))
    for turn in turns:
        allowed.update(_INT.findall(turn.get("user_text") or ""))
    for item in (memory or {}).get("confirmed_constraints") or []:
        if isinstance(item.get("value"), int):
            allowed.add(str(item["value"]))
    for number in _INT.findall(stripped):
        if number not in allowed:
            issues.append({"check": "unsupported_literal", "detail": number})
    return issues


def _collect_names(node, found=None) -> set[str]:
    if found is None:
        found = set()
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str):
            found.add(name)
        for value in node.values():
            _collect_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_names(item, found)
    return found


def _hard_constraints(turns: list[dict]) -> list[dict]:
    issues = []
    for index, turn in enumerate(turns):
        confirmed = {
            item.get("field"): item.get("value")
            for item in (turn.get("memory") or {}).get("confirmed_constraints") or []
        }
        budget = confirmed.get("budget")
        color = confirmed.get("color")
        for claim in turn.get("claims") or []:
            if budget is not None and claim.get("field") == "price" and isinstance(claim.get("value"), int):
                if int(claim["value"]) >= int(budget):
                    issues.append(
                        {
                            "check": "hard_constraint",
                            "turn_index": index,
                            "detail": {"price": claim["value"], "budget": budget},
                        }
                    )
            if color and claim.get("field") == "color" and claim.get("value") != color:
                issues.append(
                    {
                        "check": "hard_constraint",
                        "turn_index": index,
                        "detail": {"color": claim.get("value")},
                    }
                )
    return issues


def _claim_brief(claim: dict) -> dict:
    return {
        "field": claim.get("field"),
        "value": claim.get("value"),
        "product_id": claim.get("product_id"),
        "doc_id": claim.get("doc_id"),
        "evidence_ids": claim.get("evidence_ids"),
    }
