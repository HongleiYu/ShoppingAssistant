"""Product catalog module. Queries a snapshot; it does not place orders."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from shopassist.builtin_data import CATALOG_SNAPSHOT_ID, PRODUCTS
from shopassist.constraints import item_matches

FILTER_KEYS = {
    "category",
    "color",
    "size",
    "material",
    "max_price",
    "in_stock",
    "include_stale",
    "price_op",
}


class CatalogError(ValueError):
    pass


def _dynamic_view(product: dict, *, fresh: bool) -> dict:
    dynamic = product["dynamic"]
    refreshed = dynamic.get("refreshed")
    if fresh and refreshed:
        chosen = refreshed
        status = "ok"
        supported = True
    elif dynamic.get("freshness") == "stale" and not fresh:
        chosen = dynamic
        status = "stale"
        supported = False
    elif dynamic.get("freshness") == "current":
        chosen = dynamic
        status = "ok"
        supported = True
    elif refreshed:
        chosen = refreshed
        status = "ok"
        supported = True
    else:
        chosen = dynamic
        status = "stale" if dynamic.get("freshness") == "stale" else "ok"
        supported = status == "ok"
    static_attributes = product["static_attributes"]
    item = {
        "product_id": product["product_id"],
        "sku": product["sku"],
        "name": product["name"],
        "currency": product["currency"],
        "category": static_attributes["category"],
        "color": static_attributes["color"],
        "size": static_attributes["size"],
        "material": static_attributes["material"],
        "warranty_months": static_attributes["warranty_months"],
        "price": chosen["price"],
        "stock": chosen["stock"],
        "freshness": chosen["freshness"],
        "as_of": chosen["as_of"],
        "price_supported": supported,
        "price_record_class": "dynamic",
        "stock_record_class": "dynamic",
        "attribute_record_class": "static",
    }
    return {"status": status, "item": item}


class Catalog:
    def __init__(self, snapshot_id: str, products: list[dict]):
        self.snapshot_id = snapshot_id
        self.products = products
        self._by_id = {item["product_id"]: item for item in products}

    def name_index(self) -> list[dict]:
        rows = [
            {"product_id": item["product_id"], "sku": item["sku"], "name": item["name"]}
            for item in self.products
        ]
        rows.sort(key=lambda row: row["product_id"])
        return rows

    def get(self, product_id: str) -> dict | None:
        return self._by_id.get(product_id)

    def quote(self, product_id: str, *, fresh: bool = False) -> dict:
        product = self.get(product_id)
        if product is None:
            return {
                "status": "empty",
                "snapshot_id": self.snapshot_id,
                "product_id": product_id,
                "items": [],
            }
        viewed = _dynamic_view(product, fresh=fresh)
        return {
            "status": viewed["status"],
            "snapshot_id": self.snapshot_id,
            "product_id": product_id,
            **{k: v for k, v in viewed["item"].items() if k != "product_id"},
            "product_id": product_id,
            "items": [viewed["item"]],
        }

    def attributes(self, product_id: str) -> dict:
        # Attributes use the supported dynamic value when a refresh exists.
        quoted = self.quote(product_id, fresh=True)
        if quoted["status"] == "empty":
            return quoted
        quoted["status"] = "ok" if quoted.get("price_supported") else quoted["status"]
        return quoted

    def _items(self, *, fresh_dynamic: bool) -> list[dict]:
        rows = []
        for product in self.products:
            viewed = _dynamic_view(product, fresh=fresh_dynamic)
            if viewed["status"] == "ok" and viewed["item"]["price_supported"]:
                rows.append(viewed["item"])
            elif not fresh_dynamic:
                rows.append(viewed["item"])
        return rows

    def search(self, filters: dict | None = None, limit: int = 10) -> dict:
        filters = dict(filters or {})
        unknown = sorted(set(filters) - FILTER_KEYS)
        if unknown:
            raise CatalogError("unknown_filter:" + ",".join(unknown))
        matched = [item for item in self._items(fresh_dynamic=True) if item_matches(item, filters)]
        matched.sort(key=lambda item: (item["price"], item["product_id"]))
        items = matched[: max(0, int(limit))]
        status = "ok" if items else "ok"
        return {"status": status, "snapshot_id": self.snapshot_id, "items": items, "filters": filters}

    def browse_category(self, category: str) -> dict:
        items = []
        for product in self.products:
            if product["static_attributes"]["category"] != category:
                continue
            viewed = _dynamic_view(product, fresh=True)
            items.append(viewed["item"])
        items.sort(key=lambda item: item["product_id"])
        return {"status": "ok", "snapshot_id": self.snapshot_id, "items": items, "category": category}

    def compare(self, product_ids: list[str]) -> dict:
        items = []
        missing = []
        for product_id in product_ids:
            quoted = self.attributes(product_id)
            if quoted["status"] == "empty":
                missing.append(product_id)
                continue
            items.append(quoted["items"][0])
        status = "ok" if items and not missing else ("empty" if not items else "ok")
        return {
            "status": status,
            "snapshot_id": self.snapshot_id,
            "items": items,
            "missing_product_ids": missing,
        }


def default_catalog() -> Catalog:
    return Catalog(CATALOG_SNAPSHOT_ID, deepcopy(PRODUCTS))


def catalog_document() -> dict:
    return {"snapshot_id": CATALOG_SNAPSHOT_ID, "currency_default": "USD", "products": deepcopy(PRODUCTS)}


def load_catalog(path: str | Path | None = None) -> Catalog:
    if path is None:
        return default_catalog()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "products" not in data or "snapshot_id" not in data:
        raise CatalogError("catalog_file_missing_fields")
    return Catalog(data["snapshot_id"], data["products"])


def export_catalog(path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
