"""Public turn projection for the browser demo.

The projection is built from a finished mock turn. It does not add prices,
tracking facts, or planner steps that the runtime did not record.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from shopassist.builtin_data import CATALOG_SNAPSHOT_ID, KB_VERSION
from shopassist.inprocess import InProcessSandbox
from shopassist.redact import scrub_object
from shopassist.session_runner import AgentSession
from shopassist.settings import load_settings
from shopassist.versioning import BANNED_TRACE_KEYS

CONFIG_PATH = Path(__file__).resolve().parent / "pages" / "demo_config.json"
_PREVIEW_LIMIT = 160
_PRODUCT_ID = re.compile(r"(?:prod-[a-z0-9-]+|syn-p-[0-9]+)\Z")


def load_demo_config() -> dict:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("demo_config_not_object")
    prompts = data.get("prompts")
    scenarios = data.get("scenarios")
    images = data.get("images")
    if not isinstance(prompts, dict) or "en" not in prompts or "zh" not in prompts:
        raise ValueError("demo_config_missing_fields")
    if not data.get("max_message_chars") or not isinstance(scenarios, list) or not scenarios:
        raise ValueError("demo_config_missing_fields")
    if not isinstance(images, dict) or data.get("default_product_id") not in images:
        raise ValueError("demo_config_missing_fields")
    if data.get("brand") != "Demo Flagship Store" or data.get("brand_zh") != "演示旗舰店":
        raise ValueError("demo_config_missing_fields")
    for scenario in scenarios:
        product_ids = scenario.get("product_ids")
        if not isinstance(product_ids, list) or not product_ids:
            raise ValueError("demo_config_missing_fields")
        if not isinstance(scenario.get("purpose"), dict) or not isinstance(scenario.get("samples"), dict):
            raise ValueError("demo_config_missing_fields")
        welcome = scenario.get("welcome") or {}
        suggestions = scenario.get("suggestions") or {}
        context_note = scenario.get("context_note") or {}
        if not welcome.get("en") or not welcome.get("zh"):
            raise ValueError("demo_config_missing_fields")
        if not context_note.get("en") or not context_note.get("zh"):
            raise ValueError("demo_config_missing_fields")
        if not suggestions.get("en") or not suggestions.get("zh"):
            raise ValueError("demo_config_missing_fields")
    stream = data.get("stream") or {}
    if not stream.get("step_ms") or not stream.get("chunk_chars"):
        raise ValueError("demo_config_missing_fields")
        for product_id in product_ids:
            if product_id not in images:
                raise ValueError("demo_config_missing_fields")
    return data


def project_turn(turn: dict, *, request_id: str, duration_ms: float) -> dict:
    evidence_by_id = {}
    for item in turn.get("evidence") or []:
        evidence_id = item.get("evidence_id")
        if evidence_id:
            evidence_by_id[evidence_id] = item
    tools = []
    for tool in turn.get("tools") or []:
        evidence = evidence_by_id.get(tool.get("evidence_id")) or {}
        tools.append(
            {
                "tool": tool.get("tool"),
                "status": tool.get("status"),
                "evidence_id": tool.get("evidence_id"),
                "arguments": tool.get("arguments") or {},
                "output": _tool_output(evidence.get("payload") or {}),
                "untrusted_preview": [_preview(text) for text in (evidence.get("untrusted_text") or [])[:2]],
                "transport": "in_process",
                "data_source": "synthetic_snapshot",
            }
        )
    plan = []
    for step in turn.get("plan") or []:
        plan.append(
            {
                "step_id": step.get("step_id"),
                "skill_id": step.get("skill_id"),
                "depends_on": list(step.get("depends_on") or []),
                "arguments": step.get("arguments") or {},
            }
        )
    constraints = []
    for item in (turn.get("memory") or {}).get("confirmed_constraints") or []:
        constraints.append(
            {"field": item.get("field"), "value": item.get("value"), "status": item.get("status")}
        )
    context = turn.get("context_view") or {}
    payload = {
        "ok": True,
        "request_id": request_id,
        "duration_ms": round(float(duration_ms), 2),
        "duration_scope": "user_message",
        "user_text": turn.get("user_text") or "",
        "reply_text": turn.get("reply_text") or "",
        "route": turn.get("route"),
        "route_reason_code": turn.get("route_reason_code"),
        "planner_invoked": bool(turn.get("planner_invoked")),
        "task_id": turn.get("task_id"),
        "resumed": bool(turn.get("resumed")),
        "terminal": {
            "status": (turn.get("terminal") or {}).get("status"),
            "reason_code": (turn.get("terminal") or {}).get("reason_code"),
        },
        "limitations": [{"code": item.get("code")} for item in turn.get("limitations") or [] if item.get("code")],
        "recovery": list(turn.get("recovery") or []),
        "skills": [
            {
                "skill_id": item.get("skill_id"),
                "version": item.get("version"),
                "status": item.get("status"),
            }
            for item in turn.get("skills") or []
        ],
        "tools": tools,
        "plan": plan,
        "planner_actions": list(turn.get("planner_actions") or []),
        "evidence_gate": turn.get("evidence_gate") or {"allowed_count": 0, "blocked": []},
        "cards": _cards(turn),
        "shipment": _shipment(turn),
        "facts": [
            {
                "field": claim.get("field"),
                "value": claim.get("value"),
                "product_id": claim.get("product_id"),
                "doc_id": claim.get("doc_id"),
            }
            for claim in turn.get("claims") or []
            if claim.get("field")
        ],
        "consulted_product_id": (turn.get("memory") or {}).get("consulted_product_id"),
        "constraints": constraints,
        "context": {
            "store_kind": context.get("store_kind"),
            "cache_kind": context.get("cache_kind"),
            "cache_status": context.get("cache_status"),
        },
        "model_called": False,
        "simulated_model": bool(turn.get("model_invocations")),
        "network_model": False,
        "token_usage": None,
        "token_usage_reason": "unavailable_for_mock",
        "mcp_process": False,
        "model_invocations": _public_invocations(turn.get("model_invocations") or []),
        "display_cards": _display_cards(turn, _cards(turn)),
        "presentation": _presentation(turn.get("model_invocations") or [], [
            {
                "field": claim.get("field"),
                "value": claim.get("value"),
                "product_id": claim.get("product_id"),
            }
            for claim in turn.get("claims") or []
            if claim.get("field")
        ]),
        "stream": {"source": "validated_reply_replay", "simulated": True, "network": False},
    }
    _drop_banned(payload)
    cleaned, count = scrub_object(payload)
    cleaned["redaction_count"] = count
    return cleaned


def _public_invocations(items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        rows.append(
            {
                "agent": item.get("agent"),
                "phase": item.get("phase"),
                "simulated": True,
                "network": False,
                "adapter": item.get("adapter"),
                "input": item.get("input") or {},
                "output": item.get("output") or {},
            }
        )
    return rows


def _display_cards(turn: dict, cards: list[dict]) -> list[dict]:
    skill_ids = [item.get("skill_id") for item in turn.get("skills") or []]
    if any(skill_id in {"product_search", "product_compare"} for skill_id in skill_ids):
        return cards
    consulted_before = (turn.get("memory_before") or {}).get("consulted_product_id")
    if "product_quote" in skill_ids:
        if len(cards) == 1 and cards[0].get("product_id") == consulted_before:
            return []
        return cards
    return []


def _presentation(invocations: list[dict], facts: list[dict]) -> list[dict]:
    steps = []
    for item in invocations:
        if item.get("phase") == "answer":
            continue
        output = item.get("output") or {}
        steps.append(
            {
                "kind": "running",
                "agent": item.get("agent"),
                "phase": item.get("phase"),
                "skill_id": output.get("skill_id"),
                "route": output.get("route"),
                "simulated": True,
                "network": False,
            }
        )
    grounded = []
    for fact in facts:
        if not fact.get("field"):
            continue
        grounded.append(
            {
                "field": fact.get("field"),
                "value": fact.get("value"),
                "product_id": fact.get("product_id"),
            }
        )
        if len(grounded) == 6:
            break
    if grounded:
        steps.append({"kind": "grounded", "facts": grounded, "simulated": True, "network": False})
    return steps


def comparable(projection: dict) -> dict:
    data = json.loads(json.dumps(projection))
    data.pop("request_id", None)
    data.pop("duration_ms", None)
    data.pop("redaction_count", None)
    return data


class DemoApp:
    def __init__(self, index_dir: str | None = None):
        self.config = load_demo_config()
        self.max_chars = int(self.config["max_message_chars"])
        self.settings = load_settings(defaults_only=True)
        self.index_dir = index_dir
        self._session: AgentSession | None = None
        self._open()

    def meta(self) -> dict:
        loop = self.settings["loop"]
        return {
            "brand": self.config.get("brand"),
            "mode": "mock",
            "evaluation_kind": "mock_demo",
            "engine": "shopassist.session_runner.AgentSession",
            "tool_gateway": "shopassist.inprocess.InProcessSandbox",
            "catalog_snapshot": CATALOG_SNAPSHOT_ID,
            "kb_version": KB_VERSION,
            "model_called": False,
            "mcp_process": False,
            "network_tools": False,
            "purchase": False,
            "store": "sqlite_memory",
            "cache": "local_memory",
            "settings_source": "built_in_defaults",
            "limits": {
                "max_plan_steps": loop["max_plan_steps"],
                "max_tool_calls": loop["max_tool_calls"],
                "max_retries_per_step": loop["max_retries_per_step"],
                "max_replans": loop["max_replans"],
                "max_fallbacks": loop["max_fallbacks"],
                "wall_clock_seconds": loop["wall_clock_seconds"],
                "pending_after_ms": self.settings["progress"]["pending_after_ms"],
            },
            "differences": self.config.get("differences") or [],
            "max_message_chars": self.max_chars,
        }

    def catalog(self) -> list[dict]:
        server = self._session.sandbox.server
        rows = []
        for product in server.catalog.products:
            static = product["static_attributes"]
            name = product["name"]
            rows.append(
                {
                    "product_id": product["product_id"],
                    "name": name,
                    "category": static.get("category"),
                    "color": static.get("color"),
                    "size": static.get("size"),
                    "quote_text": f"price of {name}",
                }
            )
        rows.sort(key=lambda item: item["product_id"])
        return rows

    def turn(self, text: str, on_progress=None) -> dict:
        if not isinstance(text, str):
            return {"ok": False, "error": "message_not_text"}
        if not text.strip():
            return {"ok": False, "error": "empty_message"}
        if len(text) > self.max_chars:
            return {"ok": False, "error": "message_too_long", "limit": self.max_chars}
        request_id = "req-" + uuid.uuid4().hex[:12]
        started = time.perf_counter()
        turn = self._session.user_message(text, on_progress=on_progress)
        duration_ms = (time.perf_counter() - started) * 1000
        return project_turn(turn, request_id=request_id, duration_ms=duration_ms)

    def consult(self, product_id: str) -> dict:
        if not isinstance(product_id, str) or not _PRODUCT_ID.fullmatch(product_id):
            return {"ok": False, "error": "unknown_product", "product_id": product_id if isinstance(product_id, str) else ""}
        result = self._session.sandbox.invoke("product_quote", {"product_id": product_id, "fresh": False})
        items = [
            item
            for item in result.get("items") or []
            if item.get("price_supported") is True and item.get("freshness") == "current" and item.get("product_id") == product_id
        ]
        if result.get("status") != "ok" or not items:
            return {"ok": False, "error": "unknown_product", "product_id": product_id}
        item = items[0]
        self._session.memory["consulted_product_id"] = item["product_id"]
        card = {
            "product_id": item["product_id"],
            "name": item.get("name"),
            "currency": item.get("currency"),
            "fields": {
                key: item.get(key)
                for key in ("price", "stock", "category", "color", "size", "material", "warranty_months")
                if key in item
            },
            "name_source": "synthetic_catalog_snapshot",
            "evidence_id": (item.get("evidence_ids") or [None])[0],
        }
        payload = {
            "ok": True,
            "product_id": item["product_id"],
            "card": card,
            "tool": {
                "tool": "product_quote",
                "status": result.get("status"),
                "transport": "in_process",
                "data_source": "synthetic_snapshot",
                "arguments": {"product_id": product_id, "fresh": False},
            },
        }
        cleaned, _count = scrub_object(payload)
        return cleaned

    def reset(self, product_id: str | None = None) -> dict:
        self._session.reset()
        payload = {"ok": True, "meta": self.meta()}
        if product_id:
            payload["consult"] = self.consult(product_id)
        return payload

    def close(self) -> None:
        sandbox = getattr(self._session, "sandbox", None)
        if sandbox is not None:
            sandbox.close()

    def _open(self) -> None:
        sandbox = InProcessSandbox(
            seed=self.settings["seed"],
            max_calls=self.settings["loop"]["max_tool_calls"],
            timeout=self.settings["mcp"]["tool_timeout_seconds"],
            index_dir=self.index_dir,
            knowledge=self.settings["knowledge"] if self.index_dir else None,
        )
        self._session = AgentSession(
            sandbox,
            session_id="pages-demo",
            mode="mock",
            settings=self.settings,
        )


def _cards(turn: dict) -> list[dict]:
    names: dict[str, str] = {}
    skus: dict[str, str | None] = {}
    for evidence in turn.get("evidence") or []:
        payload = evidence.get("payload") or {}
        items = list(payload.get("items") or [])
        if payload.get("product_id") and payload.get("name"):
            items.append(payload)
        for item in items:
            if not isinstance(item, dict) or not item.get("product_id"):
                continue
            names[item["product_id"]] = item.get("name") or names.get(item["product_id"])
            skus[item["product_id"]] = item.get("sku")
    grouped: dict[str, dict] = {}
    for claim in turn.get("claims") or []:
        product_id = claim.get("product_id")
        field = claim.get("field")
        if not product_id or not field:
            continue
        grouped.setdefault(product_id, {})[field] = claim.get("value")
    cards = []
    for product_id in sorted(grouped):
        cards.append(
            {
                "product_id": product_id,
                "name": names.get(product_id),
                "sku": skus.get(product_id),
                "fields": grouped[product_id],
                "name_source": "synthetic_catalog_snapshot" if names.get(product_id) else None,
            }
        )
    return cards


def _shipment(turn: dict):
    facts = {}
    for claim in turn.get("claims") or []:
        if claim.get("field") in {"logistics_status", "eta", "tracking_number"}:
            facts[claim["field"]] = claim.get("value")
    codes = {item.get("code") for item in turn.get("limitations") or []}
    if facts:
        return {"availability": "present", "facts": facts, "source": "synthetic_snapshot"}
    if "logistics_not_in_snapshot" in codes:
        return {"availability": "empty", "facts": {}, "source": "synthetic_snapshot"}
    if "logistics_unavailable" in codes or "logistics_not_queried" in codes:
        return {"availability": "unavailable", "facts": {}, "source": "synthetic_snapshot"}
    return None


def _tool_output(payload: dict) -> dict:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    documents = payload.get("documents") if isinstance(payload.get("documents"), list) else []
    output = {
        "status": payload.get("status"),
        "error_code": payload.get("error_code"),
        "item_ids": [item.get("product_id") for item in items if isinstance(item, dict) and item.get("product_id")],
        "missing_product_ids": payload.get("missing_product_ids") or [],
        "document_ids": [doc.get("doc_id") for doc in documents if isinstance(doc, dict) and doc.get("doc_id")],
        "tracking_number": payload.get("tracking_number"),
        "logistics_status": payload.get("logistics_status") if payload.get("freshness") == "current" and payload.get("status") == "ok" else None,
        "snapshot_id": payload.get("snapshot_id"),
        "kb_version": payload.get("kb_version"),
    }
    if any(isinstance(doc, dict) and doc.get("body") for doc in documents):
        output["untrusted_body"] = "omitted"
    hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
    if hits:
        output["hits"] = [
            {
                "chunk_id": hit.get("chunk_id"),
                "doc_id": hit.get("doc_id"),
                "product_id": hit.get("product_id"),
                "corpus": hit.get("corpus"),
                "score": hit.get("score"),
                "score_kind": hit.get("score_kind"),
                "semantic": hit.get("semantic"),
                "encoder_kind": hit.get("encoder_kind"),
                "model_id": hit.get("model_id"),
                "index_version": hit.get("index_version"),
                "content_hash": hit.get("content_hash"),
                "freshness": hit.get("freshness"),
            }
            for hit in hits[:8]
            if isinstance(hit, dict)
        ]
        output["encoder_kind"] = payload.get("encoder_kind")
        output["semantic"] = payload.get("semantic")
        output["score_kind"] = payload.get("score_kind")
    return output


def _preview(text: str) -> str:
    value = str(text)
    if len(value) <= _PREVIEW_LIMIT:
        return value
    return value[:_PREVIEW_LIMIT] + "…"


def _drop_banned(node) -> None:
    if isinstance(node, dict):
        for key in list(node):
            if key in BANNED_TRACE_KEYS:
                del node[key]
            else:
                _drop_banned(node[key])
    elif isinstance(node, list):
        for item in node:
            _drop_banned(item)
