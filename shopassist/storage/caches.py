"""Summary caches.

`local_memory_cache` is an in-process stand-in for offline demos.
`redis` speaks to a Redis server. `mysql_fallback` reads the durable MySQL
summary when Redis was requested and is unavailable. It is not labeled Redis.
"""

from __future__ import annotations

import json
import threading

from shopassist.storage.errors import CacheUnavailable

_LUA = """
local cur = redis.call('HGET', KEYS[1], 'version')
if cur == false then
  if ARGV[1] ~= '0' then return 0 end
else
  if cur ~= ARGV[1] then return 0 end
end
redis.call('HSET', KEYS[1], 'version', ARGV[2], 'payload', ARGV[3])
return 1
"""


class LocalMemoryCache:
    kind = "local_memory_cache"

    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[str, dict] = {}

    def get(self, key: str):
        with self._lock:
            value = self._values.get(key)
            return None if value is None else json.loads(json.dumps(value))

    def compare_and_set(self, key: str, expected_version: int, payload: dict) -> bool:
        with self._lock:
            current = self._values.get(key)
            current_version = 0 if current is None else int(current["version"])
            if current_version != expected_version:
                return False
            stored = dict(payload)
            stored["version"] = expected_version + 1
            self._values[key] = stored
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)


class RedisCache:
    kind = "redis"

    def __init__(self, url: str, timeout: float = 2):
        try:
            import redis
        except ImportError as exc:
            raise CacheUnavailable(
                "Redis cache selected but the redis client library is not installed. "
                "Refusing to use the in-process cache and call it Redis."
            ) from exc
        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        try:
            self._client.ping()
        except Exception as exc:
            raise CacheUnavailable(
                f"Redis cache selected but the server is not reachable at {url}. "
                "Start Redis, set SHOPASSIST_CACHE=local_memory, or set SHOPASSIST_CACHE_FALLBACK=mysql "
                "when the durable store is MySQL. Refusing to pretend Redis is active."
            ) from exc

    def get(self, key: str):
        payload = self._client.hget(key, "payload")
        if payload is None:
            return None
        return json.loads(payload)

    def compare_and_set(self, key: str, expected_version: int, payload: dict) -> bool:
        stored = dict(payload)
        stored["version"] = expected_version + 1
        result = self._client.eval(_LUA, 1, key, str(expected_version), str(expected_version + 1), json.dumps(stored, sort_keys=True))
        return int(result) == 1

    def delete(self, key: str) -> None:
        self._client.delete(key)


class MysqlFallbackCache:
    """Durable MySQL summary used only as a documented Redis outage fallback."""

    kind = "mysql_fallback"

    def __init__(self, store):
        if getattr(store, "kind", "") != "mysql":
            raise CacheUnavailable("mysql fallback requires a mysql store, not another database labeled as MySQL")
        self.store = store
        self._tenant_session: dict[str, tuple[str, str]] = {}

    def bind(self, key: str, tenant_id: str, session_id: str) -> None:
        self._tenant_session[key] = (tenant_id, session_id)

    def get(self, key: str):
        pair = self._tenant_session.get(key)
        if pair is None:
            return None
        summary = self.store.load_summary(*pair)
        if summary is None:
            return None
        return _cache_payload(summary)

    def compare_and_set(self, key: str, expected_version: int, payload: dict) -> bool:
        pair = self._tenant_session.get(key)
        if pair is None:
            return False
        try:
            self.store.save_summary(
                pair[0],
                pair[1],
                expected_version,
                int(payload["watermark_message_id"]),
                payload["summary_text"],
                payload["source_kind"],
                payload.get("provenance") or {},
                payload.get("created_at") or "",
            )
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        return None


def _cache_payload(summary: dict) -> dict:
    return {
        "version": summary["version"],
        "watermark_message_id": summary["watermark_message_id"],
        "summary_text": summary["summary_text"],
        "source_kind": summary["source_kind"],
        "provenance": summary.get("provenance") or {},
        "created_at": summary.get("created_at"),
    }
