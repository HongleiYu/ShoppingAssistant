"""Execution progress events. These are not model-token streams.

The assistant reply is delivered once, after the turn finishes. A separate
channel reports that work is still running, or names a milestone that the
runtime actually started.
"""

from __future__ import annotations

import json
import queue
import threading
import time

TERMINAL_TYPES = {"done", "error"}

MILESTONE_TEXT = {
    "product_search": ("retrieving", "正在检索"),
    "product_quote": ("retrieving", "正在检索"),
    "policy_lookup": ("retrieving", "正在检索"),
    "product_compare": ("comparing", "正在比较"),
    "budget_attribute_validate": ("validating", "正在核对条件"),
    "shipment_lookup": ("retrieving", "正在查询物流快照"),
    "knowledge_retrieve": ("retrieving", "正在检索知识"),
}

PENDING_TEXT = "我正在处理中"


class CancelledTurn(Exception):
    pass


class ProgressPump:
    """Decides which progress events are visible. Time is injected."""

    def __init__(self, pending_after_s: float, milestone_min_interval_s: float):
        self.pending_after_s = pending_after_s
        self.milestone_min_interval_s = milestone_min_interval_s
        self.started = None
        self.pending_sent = False
        self.saw_milestone = False
        self.last_milestone = None
        self.terminal = None

    def start(self, now: float) -> None:
        self.started = now

    def poll(self, now: float, alive: bool) -> list[dict]:
        if self.terminal is not None or self.pending_sent or self.saw_milestone or not alive:
            return []
        if self.started is None:
            return []
        if now - self.started >= self.pending_after_s:
            self.pending_sent = True
            return [_progress("pending", PENDING_TEXT)]
        return []

    def push(self, event: dict, now: float) -> list[dict]:
        kind = event.get("type")
        if kind == "progress":
            if event.get("kind") == "pending":
                return []
            if self.last_milestone is not None and now - self.last_milestone < self.milestone_min_interval_s:
                return []
            self.last_milestone = now
            self.saw_milestone = True
            return [event]
        if kind in TERMINAL_TYPES:
            if self.terminal is not None:
                return []
            self.terminal = kind
            return [event]
        if kind in {"text_delta", "card", "clarification"}:
            return [event]
        return []


def milestone_for(skill_id: str) -> dict | None:
    found = MILESTONE_TEXT.get(skill_id)
    if found is None:
        return None
    kind, text = found
    return _progress(kind, text)


def encode_sse(event_type: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n"


def iter_turn_events(session, text: str, settings: dict, *, request_id: str, now=None, idle_wait: float = 0.02):
    """Run one turn on a worker and yield encoded events. Tool work is not repeated here."""
    from shopassist.verbalize import iter_validated_text

    clock = time.monotonic if now is None else now
    events: queue.Queue = queue.Queue()
    cancel = threading.Event()
    previous_cancel = getattr(session, "cancel_event", None)
    session.cancel_event = cancel
    pump = ProgressPump(
        float(settings["progress"]["pending_after_ms"]) / 1000.0,
        float(settings["progress"]["milestone_min_interval_ms"]) / 1000.0,
    )
    pump.start(clock())
    sequence = 0

    def emit(event: dict) -> str:
        nonlocal sequence
        sequence += 1
        payload = {
            "request_id": request_id,
            "session_id": session.session_id,
            "sequence": sequence,
        }
        payload.update(event)
        return encode_sse(event["type"], payload)

    def work() -> None:
        try:
            turn = session.user_message(text, on_progress=lambda event: events.put(event))
            products = validated_cards(turn)
            if products:
                events.put({"type": "card", "products": products, "withheld_until_validated": True})
            status = (turn.get("terminal") or {}).get("status")
            if status == "WAITING_USER":
                events.put(
                    {
                        "type": "clarification",
                        "text": turn.get("reply_text") or "",
                        "source": "deterministic",
                        "missing": (turn.get("terminal") or {}).get("missing") or [],
                    }
                )
            else:
                for piece in iter_validated_text(session, turn.get("reply_text") or ""):
                    events.put(piece)
            events.put(
                {
                    "type": "done",
                    "status": _done_status(status),
                    "partial": False,
                    "reply_text": turn.get("reply_text") or "",
                    "terminal": (turn.get("terminal") or {}),
                }
            )
        except CancelledTurn:
            events.put({"type": "done", "status": "cancelled", "partial": True, "reply_text": "", "text": "已取消"})
        except Exception as exc:  # noqa: BLE001 — one sanitized terminal event
            events.put({"type": "error", "error_code": getattr(exc, "code", "request_failed"), "partial": True})
        finally:
            events.put({"type": "_end"})

    worker = threading.Thread(target=work, name="shopassist-turn", daemon=True)
    worker.start()
    try:
        while True:
            try:
                item = events.get(timeout=idle_wait)
            except queue.Empty:
                if not worker.is_alive() and events.empty():
                    break
                for event in pump.poll(clock(), worker.is_alive()):
                    yield emit(event)
                continue
            if item.get("type") == "_end":
                break
            for event in pump.push(item, clock()):
                yield emit(event)
            if item.get("type") in TERMINAL_TYPES:
                break
    finally:
        cancel.set()
        session.generation = getattr(session, "generation", 0) + 1
        session.cancel_event = previous_cancel


def validated_cards(turn: dict) -> list[dict]:
    products = []
    seen = set()
    for claim in turn.get("claims") or []:
        product_id = claim.get("product_id")
        if claim.get("field") != "price" or not product_id or product_id in seen:
            continue
        if not claim.get("evidence_ids"):
            continue
        seen.add(product_id)
        products.append(
            {
                "product_id": product_id,
                "price": claim.get("value"),
                "source": "validated_evidence",
            }
        )
    return products


def _done_status(status: str | None) -> str:
    return {
        "COMPLETED": "completed",
        "COMPLETED_NO_MATCH": "completed_no_match",
        "WAITING_USER": "waiting_user",
        "FAILED": "failed",
        "STOPPED": "stopped",
        "ESCALATED": "escalated",
    }.get(status or "", "completed")


def public_turn(turn: dict) -> dict:
    return {
        "reply_text": turn.get("reply_text") or "",
        "route": turn.get("route"),
        "route_reason_code": turn.get("route_reason_code"),
        "planner_invoked": bool(turn.get("planner_invoked")),
        "escalated": bool(turn.get("escalated")),
        "terminal": {
            "status": (turn.get("terminal") or {}).get("status"),
            "reason_code": (turn.get("terminal") or {}).get("reason_code"),
        },
        "limitations": turn.get("limitations") or [],
        "skills": turn.get("skills") or [],
        "tools": [
            {"tool": tool.get("tool"), "status": tool.get("status")}
            for tool in turn.get("tools") or []
        ],
        "context_view": turn.get("context_view"),
    }


def _progress(kind: str, text: str) -> dict:
    return {"type": "progress", "kind": kind, "text": text}


def parse_sse(blob: str) -> list[dict]:
    found = []
    for block in blob.split("\n\n"):
        if not block.strip():
            continue
        event_type = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data += line[len("data:") :].strip()
        found.append({"event": event_type, "data": json.loads(data) if data else {}})
    return found
