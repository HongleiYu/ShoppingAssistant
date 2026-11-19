"""Observable replies built only from structured facts and evidence."""

from __future__ import annotations


def _limitation(code: str) -> dict:
    return {"code": code}


def render_direct(intent: str) -> dict:
    text = {
        "thanks": "You are welcome.",
        "greeting": "Hello. I can help with items, prices, stock, and policies.",
        "ack": "Okay.",
        "irrelevant": "I can help with shopping questions about items, prices, stock, and policies.",
        "capabilities": "I can help with items, prices, stock, policies, and comparisons.",
    }.get(intent, "I can help with items, prices, stock, and policies.")
    return _package(text, [], [], "direct_answer", intent)


DEMO_UNSUPPORTED_EN = (
    "This code demo only supports the provided example questions. "
    "Choose one of the suggested questions or a supported scenario."
)


def render_demo_unsupported() -> dict:
    return _package(DEMO_UNSUPPORTED_EN, [], [], "clarification", "unrecognized")


def render_clarification(decision: dict) -> dict:
    missing = decision.get("missing") or []
    if decision.get("contradicted"):
        text = "I see two different budgets in that request. Which maximum should I use?"
    elif "budget" in missing and "category" not in missing:
        text = "What budget should I stay under?"
    elif "category" in missing:
        text = "Which kind of item should I look for?"
    elif "product" in missing:
        text = "Which item should I check?"
    else:
        text = "I need a bit more detail before I can look that up."
    return _package(text, [], [], "clarification", decision.get("route_reason_code"))


def render_escalation(reason_code: str) -> dict:
    text = "I am escalating this to a person. I will not guess a value from conflicting or missing evidence."
    return _package(text, [], [_limitation(reason_code or "human_escalation")], "human_escalation", reason_code)


def render_policy(result: dict) -> dict:
    status = result.get("status")
    # Body text is intentionally ignored here.
    if status == "conflict":
        return _package(
            "The policy notes disagree, so I cannot state a single rule.",
            [],
            [_limitation("policy_conflict")],
            "direct_answer",
            "policy_conflict",
        )
    if status == "empty":
        return _package(
            "I could not find a knowledge-base document for that topic.",
            [],
            [_limitation("retrieval_miss")],
            "direct_answer",
            "retrieval_miss",
        )
    if status == "stale":
        return _package(
            "The matching knowledge-base record is stale, so I will not state it as the current rule.",
            [],
            [_limitation("stale_record_ignored")],
            "direct_answer",
            "stale_record",
        )
    facts = [fact for fact in result.get("facts") or [] if fact.get("freshness") == "current"]
    parts = []
    window = _fact(facts, "window_days")
    if window is not None:
        parts.append(f"The current return window is {window} days for unused items.")
    if _fact(facts, "listed_on_item") is True:
        parts.append("Warranty length is listed on each item and starts on delivery.")
    fee = _fact(facts, "flat_fee")
    if fee is not None:
        parts.append(f"Standard shipping has a flat fee of {fee} USD.")
    washable = _fact(facts, "washable")
    if washable is False:
        parts.append("The care note says the item is not described as washable.")
    elif washable is True:
        parts.append("The care note says the item is washable.")
    limitations = []
    if result.get("stale_documents"):
        limitations.append(_limitation("stale_record_ignored"))
        parts.append("An older note is stale and was not used.")
    if not parts:
        parts.append("I found a current knowledge-base record but it did not add a supported fact.")
    return _package(" ".join(parts), facts, limitations, "direct_answer", "policy_lookup")


def render_items(items: list[dict], limitations: list[dict] | None = None, lead: str = "") -> dict:
    from shopassist.evidence_gate import guard_items

    items, extra = guard_items(items)
    limitations = list(limitations or []) + extra
    limitations = list(limitations or [])
    claims = []
    parts = [lead] if lead else []
    supported = [
        item
        for item in items
        if item.get("price_supported") is True and item.get("freshness") == "current"
    ]
    if not supported and not limitations:
        limitations.append(_limitation("no_match"))
        parts.append("No item in the catalog snapshot satisfies the confirmed constraints.")
    for item in supported:
        parts.append(
            f"{item['name']} ({item['product_id']}) is {item['price']} USD with stock {item['stock']}."
        )
        claims.extend(_item_claims(item))
    if any(item.get("code") == "requested_out_of_stock" for item in limitations):
        parts.append("The requested item is out of stock.")
    return _package(" ".join(part for part in parts if part), claims, limitations, "direct_answer", "grounded_items")


def render_stopped(reason_code: str) -> dict:
    text = {
        "step_budget": "I stopped because the planning step budget was reached.",
        "tool_budget": "I stopped because the tool-call budget was reached.",
        "model_budget": "I stopped because the model-call budget was reached.",
        "wall_clock": "I stopped because the time budget was reached.",
        "token_budget": "I stopped because the token budget was reached.",
        "no_progress": "I stopped because the same lookup repeated without new evidence.",
        "budget_exhausted": "I stopped because the tool-call budget was exhausted.",
        "side_effect_not_retried": "I stopped instead of repeating an action that is not safe to retry.",
        "cancelled": "I stopped because this request was cancelled.",
    }.get(reason_code, "I stopped before completing this lookup.")
    return _package(text, [], [_limitation(reason_code or "stopped")], "direct_answer", reason_code or "stopped")


def render_knowledge(result: dict) -> dict:
    citations = result.get("citations") or []
    cite = ", ".join(item.get("chunk_id") or "" for item in citations[:5] if item.get("chunk_id"))
    limitations = list(result.get("limitations") or [])
    retrieval = result.get("retrieval") or {}
    encoder = retrieval.get("encoder_kind") or "unspecified"
    if retrieval.get("semantic") is False:
        score_note = "hashing lexical baseline, not a semantic embedding"
    elif retrieval.get("semantic") is True:
        score_note = "local semantic encoder"
    else:
        score_note = "encoder not reported"
    status = result.get("status")
    if status == "error":
        code = (result.get("data") or {}).get("error_code") or "tool_error"
        if not any(item.get("code") == code for item in limitations):
            limitations.append(_limitation(code))
        return _package(
            "The knowledge index could not be queried. I will not guess a policy or a price.",
            [],
            limitations,
            "direct_answer",
            code,
        )
    if status == "conflict":
        return _package(
            f"The fictional policy chunks disagree, so I will not state a single rule. Sources: {cite}. Encoder: {encoder} ({score_note}).",
            [],
            limitations,
            "direct_answer",
            "policy_conflict",
        )
    if result.get("items"):
        rendered = render_items(result["items"], limitations)
        rendered["text"] = (
            f"Price and stock were checked on the fictional catalog after retrieval. Sources: {cite}. "
            f"Encoder: {encoder} ({score_note}). " + rendered["text"]
        )
        return rendered
    facts = list(result.get("facts") or [])
    if status == "empty" or not facts:
        if not any(item.get("code") == "retrieval_miss" for item in limitations):
            limitations.append(_limitation("retrieval_miss"))
        return _package(
            "No fictional knowledge chunk matched that query with a supported fact. I will not fill the gap.",
            [],
            limitations,
            "direct_answer",
            "retrieval_miss",
        )
    parts = [
        f"Fictional policy note. Sources: {cite}. Encoder: {encoder} ({score_note}). The document body was not followed."
    ]
    for fact in facts:
        parts.append(f"{fact['field']} is {fact['value']}.")
    return _package(" ".join(parts), facts, limitations, "direct_answer", "knowledge_retrieve")


def render_no_match() -> dict:
    return _package(
        "No item in the catalog snapshot satisfies the confirmed constraints.",
        [],
        [_limitation("no_match")],
        "direct_answer",
        "no_match",
    )


def _item_claims(item: dict) -> list[dict]:
    evidence_ids = item.get("evidence_ids") or []
    claims = []
    for field, record_class in (
        ("price", "dynamic"),
        ("stock", "dynamic"),
        ("category", "static"),
        ("color", "static"),
        ("size", "static"),
        ("material", "static"),
        ("warranty_months", "static"),
    ):
        if field not in item:
            continue
        claims.append(
            {
                "field": field,
                "value": item[field],
                "product_id": item.get("product_id"),
                "doc_id": None,
                "evidence_ids": list(evidence_ids),
                "record_class": record_class,
                "freshness": "current",
            }
        )
    return claims


def _fact(facts: list[dict], field: str):
    for fact in facts:
        if fact.get("field") == field:
            return fact.get("value")
    return None


def _package(text, claims, limitations, route, reason) -> dict:
    return {
        "text": text,
        "claims": claims,
        "limitations": limitations,
        "route": route,
        "route_reason_code": reason,
    }
