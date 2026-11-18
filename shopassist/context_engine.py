"""Profile, seven-day memory, and current-session context.

A message is one stored user or assistant record, not a round.
Structured state is authoritative for confirmed conditions. The session
summary is background only. The planner task state is not this object.
"""

from __future__ import annotations

import os
import queue
import re
import threading
from datetime import datetime, timedelta, timezone

from shopassist.constraints import empty_memory
from shopassist.settings import DEFAULTS
from shopassist.storage.caches import LocalMemoryCache, MysqlFallbackCache, RedisCache
from shopassist.storage.errors import CacheUnavailable, StaleWriteError, StoreUnavailable
from shopassist.storage.sqlite_store import SqliteStore

LATEST_MESSAGES_DEFAULT = DEFAULTS["context"]["latest_messages"]
RECENT_DAYS = DEFAULTS["context"]["recent_memory_days"]
SUMMARY_MIN_NEW_MESSAGES = DEFAULTS["context"]["summary_min_new_messages"]
SUMMARY_TOKEN_BUDGET = DEFAULTS["context"]["summary_token_budget"]
SYNC_CATCHUP_BUDGET = DEFAULTS["context"]["sync_catchup_messages"]
_PREFER = re.compile(r"i always prefer ([a-z]+)")


class Clock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def now_iso(self) -> str:
        return self._now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


def empty_state() -> dict:
    return {
        "goals": [],
        "confirmed": [],
        "assumptions": [],
        "unresolved": [],
        "excluded": [],
        "contradictions": [],
        "tombstones": [],
        "progress": {"active_task_id": None, "task_summaries": [], "phase": "idle"},
    }


def project_memory(state: dict) -> dict:
    memory = empty_memory()
    memory["confirmed_constraints"] = [dict(item) for item in state.get("confirmed") or []]
    memory["assumptions"] = [dict(item) for item in state.get("assumptions") or []]
    memory["missing"] = [dict(item) for item in state.get("unresolved") or []]
    memory["contradictions"] = [dict(item) for item in state.get("contradictions") or []]
    progress = state.get("progress") or {}
    memory["task_summaries"] = [dict(item) for item in progress.get("task_summaries") or []]
    memory["active_task_id"] = progress.get("active_task_id")
    return memory


def authoritative_confirmed(state: dict) -> dict:
    values = {}
    for item in state.get("confirmed") or []:
        if item.get("status") == "tombstoned":
            continue
        values[item.get("field")] = item.get("value")
    return values


def sync_state(previous: dict, memory: dict, message_id: int) -> dict:
    previous = previous or empty_state()
    prior = {item.get("field"): item for item in previous.get("confirmed") or []}
    tombstones = [dict(item) for item in previous.get("tombstones") or []]
    confirmed = []
    for item in memory.get("confirmed_constraints") or []:
        copied = dict(item)
        old = prior.get(copied.get("field"))
        if old and old.get("value") != copied.get("value"):
            tombstones.append(
                {
                    "field": old.get("field"),
                    "value": old.get("value"),
                    "status": "tombstoned",
                    "superseded_by_message_id": message_id,
                }
            )
        confirmed.append(copied)
    assumption_fields = {item.get("field") for item in memory.get("assumptions") or []}
    for item in confirmed:
        if item.get("field") in assumption_fields and item.get("status") == "assumed":
            raise ValueError("assumption_promoted")
    return {
        "goals": [dict(item) for item in previous.get("goals") or []],
        "confirmed": confirmed,
        "assumptions": [dict(item) for item in memory.get("assumptions") or []],
        "unresolved": [dict(item) for item in memory.get("missing") or []],
        "excluded": [dict(item) for item in previous.get("excluded") or []],
        "contradictions": [dict(item) for item in memory.get("contradictions") or []],
        "tombstones": tombstones,
        "progress": {
            "active_task_id": memory.get("active_task_id"),
            "task_summaries": [dict(item) for item in memory.get("task_summaries") or []],
            "phase": "task" if memory.get("active_task_id") else "idle",
        },
    }


def validate_state(state: dict) -> None:
    required = {"goals", "confirmed", "assumptions", "unresolved", "excluded", "contradictions", "tombstones", "progress"}
    if set(state) != required:
        raise ValueError("state_schema")
    for bucket in ("confirmed", "assumptions", "unresolved", "contradictions", "tombstones"):
        for item in state[bucket]:
            if not isinstance(item, dict) or "field" not in item:
                raise ValueError("state_item")


def estimate_tokens(messages: list[dict]) -> int:
    return sum((len(item.get("body") or "") // 4) + 1 for item in messages)


def needs_summary_refresh(messages: list[dict], watermark: int, min_new: int, token_budget: int) -> bool:
    fresh = [item for item in messages if item["message_id"] > watermark]
    if not fresh:
        return False
    return len(fresh) >= min_new or estimate_tokens(fresh) >= token_budget


class MockSummarizer:
    kind = "mock"

    def summarize(self, previous_text: str, messages: list[dict], watermark: int) -> str:
        del previous_text
        parts = [f"{item['message_id']}:{item['role']}:{item['body'][:80]}" for item in messages if item["message_id"] <= watermark]
        return f"Mock session summary through message {watermark}. " + " | ".join(parts)


class ConfiguredSummarizer:
    kind = "configured_model"

    def __init__(self, client):
        if client is None:
            raise StoreUnavailable(
                "Configured summary mode requires a summary endpoint. Refusing to use the mock summarizer silently."
            )
        self.client = client

    def summarize(self, previous_text: str, messages: list[dict], watermark: int) -> str:
        transcript = "\n".join(f"{item['message_id']} {item['role']}: {item['body']}" for item in messages if item["message_id"] <= watermark)
        raw = self.client.complete(
            [
                {
                    "role": "system",
                    "content": "Rewrite a background summary of the whole session through the watermark. Do not invent confirmed constraints.",
                },
                {"role": "user", "content": f"watermark={watermark}\nprevious={previous_text}\n{transcript}"},
            ]
        )
        return str(raw).strip()


class SummaryWorker:
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="summary-refresh", daemon=True)
        self._thread.start()

    def submit(self, func) -> None:
        self._queue.put(func)

    def flush(self, timeout: float = 2.0) -> None:
        done = threading.Event()
        self._queue.put(done.set)
        if not done.wait(timeout):
            raise TimeoutError("summary_flush_timeout")

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                job()
            finally:
                self._queue.task_done()


class ContextEngine:
    def __init__(
        self,
        store=None,
        cache=None,
        clock: Clock | None = None,
        summarizer=None,
        latest_messages: int = LATEST_MESSAGES_DEFAULT,
        recent_days: int = RECENT_DAYS,
        summary_min_new_messages: int = SUMMARY_MIN_NEW_MESSAGES,
        summary_token_budget: int = SUMMARY_TOKEN_BUDGET,
        sync_catchup_message_budget: int = SYNC_CATCHUP_BUDGET,
        worker: SummaryWorker | None = None,
    ):
        self.store = store or SqliteStore(":memory:")
        self.cache = cache or LocalMemoryCache()
        self.clock = clock or Clock()
        self.summarizer = summarizer or MockSummarizer()
        self.latest_messages = latest_messages
        self.recent_days = recent_days
        self.summary_min_new_messages = summary_min_new_messages
        self.summary_token_budget = summary_token_budget
        self.sync_catchup_message_budget = sync_catchup_message_budget
        self.worker = worker

    def projected_memory(self, tenant_id: str, session_id: str) -> dict:
        loaded = self.store.load_state(tenant_id, session_id)
        if loaded is None:
            return empty_memory()
        return project_memory(loaded["state"])

    def record_exchange(self, tenant_id, user_id, session_id, user_text: str, assistant_text: str, memory: dict) -> dict:
        created = self.clock.now_iso()
        user_message_id = self.store.append_message(tenant_id, user_id, session_id, "user", user_text, created)
        assistant_message_id = self.store.append_message(tenant_id, user_id, session_id, "assistant", assistant_text, created)
        self._extract(tenant_id, user_id, session_id, user_message_id, user_text, created)
        loaded = self.store.load_state(tenant_id, session_id)
        previous = empty_state() if loaded is None else loaded["state"]
        version = 0 if loaded is None else loaded["version"]
        state = sync_state(previous, memory, assistant_message_id)
        if "complex" in (assistant_text or "") or memory.get("active_task_id") or memory.get("task_summaries"):
            goals = list(state["goals"])
            if user_text and not any(item.get("text") == user_text for item in goals):
                goals.append({"text": user_text, "source_message_id": user_message_id})
            state["goals"] = goals[-8:]
        validate_state(state)
        self.store.save_state(tenant_id, session_id, version, state, created)
        self._schedule(tenant_id, user_id, session_id)
        return self.assemble(tenant_id, user_id, session_id)

    def assemble(self, tenant_id: str, user_id: str, session_id: str) -> dict:
        if self.sync_catchup_message_budget > 0:
            self._catch_up(tenant_id, user_id, session_id)
        messages = self.store.list_messages(tenant_id, session_id)
        summary, cache_status = self._load_summary(tenant_id, session_id)
        watermark = 0 if summary is None else int(summary["watermark_message_id"])
        latest = messages[-self.latest_messages :]
        latest_ids = [item["message_id"] for item in latest]
        latest_set = set(latest_ids)
        uncovered = [item for item in messages if item["message_id"] > watermark and item["message_id"] not in latest_set]
        overlap = [item for item in latest_ids if item <= watermark]
        covered = {item["message_id"] for item in messages if item["message_id"] <= watermark}
        covered.update(latest_set)
        covered.update(item["message_id"] for item in uncovered)
        missing = [item["message_id"] for item in messages if item["message_id"] not in covered]
        loaded = self.store.load_state(tenant_id, session_id)
        state = empty_state() if loaded is None else loaded["state"]
        profile = self.store.load_profile(tenant_id, user_id)
        now = self.clock.now()
        window_start = (now - timedelta(days=self.recent_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = self.store.select_recent(tenant_id, user_id, self.clock.now_iso(), window_start, 8)
        return {
            "profile": profile["preferences"],
            "profile_version": profile["version"],
            "recent_memories": recent,
            "summary": summary,
            "summary_authoritative": False,
            "latest_messages": latest,
            "uncovered_messages": uncovered,
            "structured_state": state,
            "authoritative_confirmed": authoritative_confirmed(state),
            "cache_status": cache_status,
            "cache_kind": self.cache.kind,
            "store_kind": self.store.kind,
            "coverage": {
                "summary_watermark": watermark,
                "latest_message_ids": latest_ids,
                "uncovered_message_ids": [item["message_id"] for item in uncovered],
                "overlap_message_ids": overlap,
                "missing_message_ids": missing,
                "coverage_complete": not missing,
                "message_count": len(messages),
                "latest_window": self.latest_messages,
                "recent_days": self.recent_days,
            },
        }

    def public_view(self, assembled: dict) -> dict:
        summary = assembled.get("summary") or {}
        text = summary.get("summary_text") or ""
        state = assembled.get("structured_state") or empty_state()
        return {
            "store_kind": assembled.get("store_kind"),
            "cache_kind": assembled.get("cache_kind"),
            "cache_status": assembled.get("cache_status"),
            "profile": assembled.get("profile") or {},
            "recent_memories": [
                {
                    "memory_id": item["memory_id"],
                    "body": item["body"],
                    "created_at": item["created_at"],
                    "expires_at": item["expires_at"],
                    "source_message_id": item["source_message_id"],
                }
                for item in assembled.get("recent_memories") or []
            ],
            "summary": {
                "version": summary.get("version"),
                "watermark_message_id": summary.get("watermark_message_id"),
                "source_kind": summary.get("source_kind"),
                "text": text[:500],
                "authoritative": False,
            },
            "structured_state": {
                "confirmed": authoritative_confirmed(state),
                "assumptions": [item.get("field") for item in state.get("assumptions") or []],
                "unresolved": [item.get("field") for item in state.get("unresolved") or []],
                "excluded_count": len(state.get("excluded") or []),
                "tombstone_count": len(state.get("tombstones") or []),
                "active_task_id": (state.get("progress") or {}).get("active_task_id"),
                "phase": (state.get("progress") or {}).get("phase"),
            },
            "coverage": assembled.get("coverage") or {},
        }

    def refresh_now(self, tenant_id, user_id, session_id) -> None:
        self._refresh(tenant_id, user_id, session_id)

    def flush(self, timeout: float = 2.0) -> None:
        if self.worker is not None:
            self.worker.flush(timeout)

    def reset_session(self, tenant_id, session_id) -> None:
        self.store.reset_session(tenant_id, session_id)
        self.cache.delete(_summary_key(tenant_id, session_id))

    def _extract(self, tenant_id, user_id, session_id, message_id, text, created_at) -> None:
        match = _PREFER.search((text or "").lower())
        if not match:
            return
        color = match.group(1)
        profile = self.store.load_profile(tenant_id, user_id)
        preferences = dict(profile["preferences"])
        stable = dict(preferences.get("stable") or {})
        stable["color"] = color
        preferences["stable"] = stable
        self.store.save_profile(tenant_id, user_id, profile["version"], preferences, created_at)
        created = self.clock.now()
        expires = (created + timedelta(days=self.recent_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.upsert_recent(
            tenant_id,
            user_id,
            f"mem-{session_id}-{message_id}-prefer",
            session_id,
            message_id,
            text,
            "preference_signal",
            0.9,
            created_at,
            expires,
        )

    def _schedule(self, tenant_id, user_id, session_id) -> None:
        messages = self.store.list_messages(tenant_id, session_id)
        summary = self.store.load_summary(tenant_id, session_id)
        watermark = 0 if summary is None else int(summary["watermark_message_id"])
        if not needs_summary_refresh(messages, watermark, self.summary_min_new_messages, self.summary_token_budget):
            return
        if self.worker is None:
            return
        self.worker.submit(lambda: self._refresh(tenant_id, user_id, session_id))

    def _catch_up(self, tenant_id, user_id, session_id) -> None:
        messages = self.store.list_messages(tenant_id, session_id)
        summary = self.store.load_summary(tenant_id, session_id)
        watermark = 0 if summary is None else int(summary["watermark_message_id"])
        latest = messages[-self.latest_messages :]
        latest_set = {item["message_id"] for item in latest}
        uncovered = [item for item in messages if item["message_id"] > watermark and item["message_id"] not in latest_set]
        if len(uncovered) > self.sync_catchup_message_budget:
            self._refresh(tenant_id, user_id, session_id)

    def _refresh(self, tenant_id, user_id, session_id) -> None:
        del user_id
        messages = self.store.list_messages(tenant_id, session_id)
        if not messages:
            return
        current = self.store.load_summary(tenant_id, session_id)
        version = 0 if current is None else int(current["version"])
        watermark = messages[-1]["message_id"]
        if current and int(current["watermark_message_id"]) >= watermark:
            return
        text = self.summarizer.summarize("" if current is None else current["summary_text"], messages, watermark)
        provenance = {
            "source_kind": self.summarizer.kind,
            "watermark_message_id": watermark,
            "message_count": len(messages),
            "covers_all_history_through_watermark": True,
        }
        try:
            new_version = self.store.save_summary(
                tenant_id,
                session_id,
                version,
                watermark,
                text,
                self.summarizer.kind,
                provenance,
                self.clock.now_iso(),
            )
        except StaleWriteError:
            return
        payload = {
            "version": version,
            "watermark_message_id": watermark,
            "summary_text": text,
            "source_kind": self.summarizer.kind,
            "provenance": provenance,
            "created_at": self.clock.now_iso(),
        }
        key = _summary_key(tenant_id, session_id)
        if isinstance(self.cache, MysqlFallbackCache):
            self.cache.bind(key, tenant_id, session_id)
        self.cache.compare_and_set(key, version, payload)
        if new_version != version + 1:
            return

    def _load_summary(self, tenant_id, session_id):
        key = _summary_key(tenant_id, session_id)
        if isinstance(self.cache, MysqlFallbackCache):
            self.cache.bind(key, tenant_id, session_id)
        cached = self.cache.get(key)
        durable = self.store.load_summary(tenant_id, session_id)
        if cached is None and durable is None:
            return None, "miss"
        if cached is None and durable is not None:
            payload = {
                "version": durable["version"] - 1,
                "watermark_message_id": durable["watermark_message_id"],
                "summary_text": durable["summary_text"],
                "source_kind": durable["source_kind"],
                "provenance": durable["provenance"],
                "created_at": durable["created_at"],
            }
            self.cache.compare_and_set(key, payload["version"], payload)
            label = "restored_from_mysql" if self.store.kind == "mysql" else "restored_from_durable"
            return durable, label
        if cached is not None and durable is not None and int(cached.get("version") or 0) < int(durable["version"]):
            return durable, "durable_newer_than_cache"
        if cached is None:
            return durable, "miss"
        return {
            "version": int(cached["version"]),
            "watermark_message_id": int(cached["watermark_message_id"]),
            "summary_text": cached["summary_text"],
            "source_kind": cached["source_kind"],
            "provenance": cached.get("provenance") or {},
            "created_at": cached.get("created_at"),
        }, "hit"


def _summary_key(tenant_id: str, session_id: str) -> str:
    return f"shopassist:{tenant_id}:{session_id}:summary"


def open_store_from_env():
    from shopassist.settings import load_settings

    settings = load_settings()
    mode = settings["store"]["kind"]
    if mode == "local_sqlite":
        return SqliteStore(settings["store"]["sqlite_path"])
    if mode == "mysql":
        from shopassist.storage.mysql_store import MysqlStore

        mysql = settings["mysql"]
        return MysqlStore(
            host=mysql["host"],
            port=int(mysql["port"]),
            user=mysql["user"],
            password=mysql["password"],
            database=mysql["database"],
            connect_timeout=float(mysql["connect_timeout_seconds"]),
        )
    raise StoreUnavailable(
        f"Unknown SHOPASSIST_STORE={mode}. Use local_sqlite or mysql. local_sqlite is the offline demo."
    )


def open_cache_from_env(store):
    from shopassist.settings import load_settings

    settings = load_settings()
    mode = settings["cache"]["kind"]
    if mode == "local_memory":
        return LocalMemoryCache()
    if mode == "mysql_fallback":
        return MysqlFallbackCache(store)
    if mode == "redis":
        try:
            url = settings["cache"]["redis_url"] or "redis://127.0.0.1:6379/0"
            return RedisCache(url, timeout=float(settings["cache"]["socket_timeout_seconds"]))
        except CacheUnavailable:
            if settings["cache"]["fallback"] == "mysql":
                return MysqlFallbackCache(store)
            raise
    raise CacheUnavailable(
        f"Unknown SHOPASSIST_CACHE={mode}. Use local_memory, redis, or mysql_fallback."
    )
