"""Extract shopping features from a user turn. No plan and no tool facts."""

from __future__ import annotations

import re

COLORS = ("sand", "blue", "red", "navy", "olive", "steel", "natural", "pink")
CATEGORIES = ("drinkware", "apparel", "home", "stationery")
MATERIALS = ("ceramic", "stoneware", "steel", "cotton", "linen", "paper")

_BUDGET = re.compile(r"(?:under|less than|my max is|budget to)\s+(\d+)")
_TRACKING = re.compile(r"\b(TRK-[A-Z0-9-]+)\b", re.I)
_CHANGE = re.compile(r"change the budget to\s+(\d+)")
_SIZE = re.compile(r"size\s+([A-Za-z0-9]+)")
_COLOR_IN = re.compile(r"(?:in|color|be)\s+(sand|blue|red|navy|olive|steel|natural|pink)\b")
_MAYBE = re.compile(r"maybe\s+(ceramic|stoneware|steel|cotton|linen|paper)\b")


def detect_intent(text: str) -> str:
    lowered = text.strip().lower()
    if "a person" in lowered:
        return "ask_human"
    if lowered.startswith("thank"):
        return "thanks"
    if lowered in {"hello.", "hello", "hi there.", "hi there"}:
        return "greeting"
    if lowered in {"okay.", "okay", "sounds good.", "sounds good"}:
        return "ack"
    if "weather" in lowered or "joke" in lowered:
        return "irrelevant"
    if "what shopping topics" in lowered or "what can you help" in lowered:
        return "capabilities"
    if "actually my max" in lowered:
        return "contradict_budget"
    if lowered.startswith("compare warranty"):
        return "compare_warranty"
    if lowered.startswith("compare"):
        return "compare"
    if "if it is unavailable" in lowered:
        return "oos"
    if "older return" in lowered:
        return "policy_stale"
    if "disagree" in lowered:
        return "policy_conflict"
    if "gift wrap" in lowered:
        return "policy_missing"
    if "legacy gift" in lowered:
        return "policy_legacy"
    if "care note" in lowered:
        return "policy_care"
    if "return window" in lowered or "return note" in lowered:
        return "policy_returns"
    if "warranty" in lowered:
        return "policy_warranty"
    if "shipping" in lowered:
        return "policy_shipping"
    if "tracking " in lowered:
        return "shipment"
    if "price" in lowered or "in stock" in lowered or "stock count" in lowered:
        return "quote"
    if lowered.startswith("change the budget"):
        return "change_budget"
    if "has to be" in lowered:
        return "add_color"
    if lowered.startswith("recommend"):
        return "recommend"
    if lowered.startswith("find") or lowered.startswith("show me"):
        return "search_size" if "size " in lowered else "search"
    if lowered.startswith("help me find"):
        return "help_find"
    return "unknown"


def find_products(text: str, name_index: list[dict]) -> list[dict]:
    lowered = text.lower()
    found = []
    for entry in sorted(name_index, key=lambda item: len(item["name"]), reverse=True):
        name = entry["name"].lower()
        if name and name in lowered:
            found.append(entry)
            lowered = lowered.replace(name, " ")
    found.sort(key=lambda item: item["product_id"])
    return found


def extract_features(text: str, name_index: list[dict]) -> dict:
    intent = detect_intent(text)
    products = find_products(text, name_index)
    budgets = [int(value) for value in _BUDGET.findall(text.lower())]
    change = _CHANGE.search(text.lower())
    if change:
        budgets = [int(change.group(1))]
    colors = []
    for match in _COLOR_IN.findall(text.lower()):
        if match not in colors:
            colors.append(match)
    categories = [name for name in CATEGORIES if re.search(rf"\b{name}\b", text.lower())]
    sizes = _SIZE.findall(text)
    assumptions = []
    maybe = _MAYBE.search(text.lower())
    if maybe:
        assumptions.append({"field": "material", "value": maybe.group(1)})
    contradictions = []
    if intent == "contradict_budget" and len(_BUDGET.findall(text.lower())) >= 2:
        numbers = [int(value) for value in _BUDGET.findall(text.lower())]
        contradictions.append({"field": "budget", "values": numbers[:2]})
    topic = {
        "policy_returns": "returns",
        "policy_stale": "returns",
        "policy_warranty": "warranty",
        "policy_shipping": "shipping",
        "policy_care": "care",
        "policy_conflict": "returns_dispute",
        "policy_missing": "gift_wrap",
        "policy_legacy": "legacy_gift",
    }.get(intent)
    return {
        "intent": intent,
        "product_ids": [item["product_id"] for item in products],
        "products": products,
        "budgets": budgets,
        "colors": colors,
        "categories": categories,
        "sizes": sizes,
        "assumptions": assumptions,
        "contradictions": contradictions,
        "topic": topic,
        "include_stale": intent == "policy_stale",
        "tracking_id": (_TRACKING.search(text).group(1).upper() if _TRACKING.search(text) else None),
        "text": text,
    }
