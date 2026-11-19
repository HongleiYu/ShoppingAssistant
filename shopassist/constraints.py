"""Pure constraint checks shared by catalog search and validation skills."""

from __future__ import annotations

DYNAMIC_FIELDS = frozenset({"price", "stock"})
STATIC_FIELDS = frozenset(
    {"category", "color", "size", "material", "warranty_months", "name", "sku"}
)


def item_matches(item: dict, filters: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if filters.get("in_stock", True) and int(item.get("stock") or 0) <= 0:
        return False
    freshness = item.get("freshness", "current")
    if freshness != "current" and not filters.get("include_stale"):
        return False
    if item.get("price_supported") is False and "max_price" in filters:
        return False
    if "max_price" in filters and item.get("price") is not None:
        price = int(item["price"])
        limit = int(filters["max_price"])
        op = filters.get("price_op", "lt")
        if op == "lte" and price > limit:
            return False
        if op != "lte" and price >= limit:
            return False
    for key in ("category", "color", "size", "material"):
        if key in filters and item.get(key) != filters[key]:
            return False
    return True


def empty_memory() -> dict:
    return {
        "confirmed_constraints": [],
        "assumptions": [],
        "missing": [],
        "contradictions": [],
        "task_summaries": [],
        "active_task_id": None,
        "consulted_product_id": None,
    }


def clone_memory(memory: dict | None) -> dict:
    base = empty_memory()
    if not memory:
        return base
    for key in base:
        value = memory.get(key)
        if isinstance(value, list):
            base[key] = [dict(item) for item in value]
        else:
            base[key] = value
    return base


def constraints_by_field(items: list[dict]) -> dict[str, dict]:
    out = {}
    for item in items:
        if isinstance(item, dict) and item.get("field"):
            out[item["field"]] = item
    return out


def upsert_constraint(items: list[dict], constraint: dict) -> list[dict]:
    kept = [item for item in items if item.get("field") != constraint.get("field")]
    kept.append(dict(constraint))
    kept.sort(key=lambda item: item.get("field") or "")
    return kept


def drop_constraint(items: list[dict], field: str) -> list[dict]:
    return [item for item in items if item.get("field") != field]
