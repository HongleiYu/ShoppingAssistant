"""Mandatory evidence checks. Models and settings cannot skip these.

The gate covers known business fields. It does not prove that arbitrary
free text is grounded, and it does not claim to stop every hallucination.
"""

from __future__ import annotations

import re

HIGH_RISK_FIELDS = (
    "price",
    "currency",
    "stock",
    "product_id",
    "logistics_status",
    "eta",
    "tracking_number",
    "order_placed",
    "purchase_id",
)

_LOGISTICS_WORDS = ("delivered", "in transit", "in_transit", "out for delivery", "eta", "tracking")
_SIDE_EFFECT_WORDS = ("order placed", "purchase complete", "bought it", "order confirmed")
_USD = re.compile(r"(\d+)\s*usd", re.I)
_STOCK = re.compile(r"stock\s+(\d+)", re.I)


def typed_record(
    *,
    field: str,
    entity: str | None,
    value,
    availability: str,
    freshness: str,
    evidence_id: str | None,
    source: str,
    request_id: str | None = None,
) -> dict:
    return {
        "field": field,
        "entity": entity,
        "value": value,
        "availability": availability,
        "freshness": freshness,
        "evidence_id": evidence_id,
        "source": source,
        "request_id": request_id,
    }


def records_from_facts(facts: list[dict], *, source: str, request_id: str | None = None) -> list[dict]:
    rows = []
    for fact in facts or []:
        freshness = fact.get("freshness") or "current"
        availability = "present" if freshness == "current" else "stale"
        if fact.get("availability"):
            availability = fact["availability"]
        rows.append(
            typed_record(
                field=str(fact.get("field")),
                entity=fact.get("product_id") or fact.get("doc_id") or fact.get("tracking_id"),
                value=fact.get("value"),
                availability=availability,
                freshness=freshness,
                evidence_id=(fact.get("evidence_ids") or [None])[0],
                source=source,
                request_id=request_id,
            )
        )
    return rows


def gate_turn(turn: dict, *, request_id: str | None = None) -> dict:
    facts = []
    for evidence in turn.get("evidence") or []:
        facts.extend(evidence.get("facts") or [])
    facts.extend(turn.get("facts") or [])
    for claim in turn.get("claims") or []:
        if claim.get("evidence_ids") and claim.get("freshness") == "current":
            facts.append(claim)
    rows = records_from_facts(facts, source="turn", request_id=request_id)
    allowed, blocked = _split_claims(turn.get("claims") or [], rows)
    limitations = list(turn.get("limitations") or [])
    for item in blocked:
        code = item["code"]
        if code not in {entry.get("code") for entry in limitations}:
            limitations.append({"code": code})
    text = guard_text(turn.get("reply_text") or "", allowed, blocked)
    return {
        "claims": allowed,
        "limitations": limitations,
        "reply_text": text,
        "blocked": blocked,
        "records": rows,
    }


def guard_text(text: str, allowed: list[dict], blocked: list[dict]) -> str:
    allowed_tokens = set()
    for claim in allowed:
        allowed_tokens.add(_norm(claim.get("value")))
        allowed_tokens.add(_norm(claim.get("product_id")))
    sentences = re.split(r"(?<=[.!?])\s+", text.strip()) if text.strip() else []
    kept = []
    for sentence in sentences:
        if not sentence:
            continue
        if _sentence_blocked(sentence, allowed_tokens):
            continue
        kept.append(sentence)
    if kept:
        return " ".join(kept)
    if not sentences:
        return text
    codes = {item["code"] for item in blocked}
    if "logistics_not_in_snapshot" in codes:
            return "The logistics snapshot has no record for that id."
    if any(item["field"] in {"logistics_status", "eta", "tracking_number"} for item in blocked) or _logistics_marker(text):
        return "Logistics status is unavailable for this request."
    return "That detail is not confirmed by the evidence for this request."


def guard_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop stale dynamic fields so a cache hit is not described as live."""
    cleaned = []
    limitations = []
    for item in items or []:
        copy = dict(item)
        if copy.get("freshness") not in (None, "current") or copy.get("price_supported") is False:
            copy.pop("price", None)
            copy.pop("stock", None)
            copy["dynamic_verified"] = False
            limitations.append({"code": "dynamic_unverified"})
        else:
            copy["dynamic_verified"] = True
        cleaned.append(copy)
    return cleaned, limitations


def _split_claims(claims: list[dict], rows: list[dict]) -> tuple[list[dict], list[dict]]:
    allowed = []
    blocked = []
    for claim in claims:
        field = str(claim.get("field"))
        if field not in HIGH_RISK_FIELDS and field not in {"window_days", "warranty_months", "material", "color", "category", "size"}:
            allowed.append(claim)
            continue
        match = _matching_row(claim, rows)
        if match is None:
            blocked.append({"field": field, "code": _missing_code(field), "reason": "not_in_evidence"})
            continue
        if match["availability"] == "empty":
            blocked.append({"field": field, "code": _empty_code(field), "reason": "successful_empty"})
            continue
        if match["availability"] != "present" or match["freshness"] != "current":
            blocked.append({"field": field, "code": _unavailable_code(match), "reason": match["availability"]})
            continue
        if claim.get("value") != match["value"] and field in HIGH_RISK_FIELDS:
            blocked.append({"field": field, "code": "evidence_mismatch", "reason": "value"})
            continue
        allowed.append(claim)
    return allowed, blocked


def _matching_row(claim: dict, rows: list[dict]):
    evidence_ids = set(claim.get("evidence_ids") or [])
    entity = claim.get("product_id") or claim.get("doc_id") or claim.get("tracking_id")
    for row in rows:
        if row["field"] != claim.get("field"):
            continue
        if evidence_ids and row.get("evidence_id") not in evidence_ids:
            continue
        if entity and row.get("entity") not in {None, entity}:
            continue
        return row
    return None


def _sentence_blocked(sentence: str, allowed_tokens: set[str]) -> bool:
    lowered = sentence.lower()
    markers = [word for word in _LOGISTICS_WORDS + _SIDE_EFFECT_WORDS if word in lowered]
    usd = _USD.findall(sentence)
    stock = _STOCK.findall(sentence)
    if not markers and not usd and not stock:
        return False
    folded = _norm(sentence)
    if any(token and token in folded for token in allowed_tokens):
        return False
    return True


def _missing_code(field: str) -> str:
    if field in {"logistics_status", "eta", "tracking_number"}:
        return "logistics_not_queried"
    if field in {"order_placed", "purchase_id"}:
        return "side_effect_without_receipt"
    return "fact_not_in_evidence"


def _empty_code(field: str) -> str:
    if field in {"logistics_status", "eta", "tracking_number"}:
        return "logistics_not_in_snapshot"
    return "successful_empty"


def _unavailable_code(row: dict) -> str:
    if row["field"] in {"logistics_status", "eta", "tracking_number"}:
        return "logistics_unavailable"
    if row["availability"] == "stale" or row["freshness"] == "stale":
        return "dynamic_unverified"
    if row["availability"] == "conflict":
        return "conflicting_evidence"
    return "evidence_unavailable"


def _logistics_marker(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _LOGISTICS_WORDS)


def _norm(value) -> str:
    return re.sub(r"[\s_]+", "", str(value or "").lower())
