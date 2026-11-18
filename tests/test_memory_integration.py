"""Optional MySQL and Redis checks. Skipped with an explicit reason when services are absent."""

from __future__ import annotations

import os
import secrets
import subprocess
import unittest

from shopassist.storage.caches import RedisCache
from shopassist.storage.errors import StaleWriteError


def _redis_ok() -> bool:
    try:
        import redis

        return bool(redis.Redis.from_url(os.environ.get("SHOPASSIST_REDIS_URL", "redis://127.0.0.1:6379/0"), socket_connect_timeout=1).ping())
    except Exception:
        return False


def _mysql_settings():
    if os.environ.get("SHOPASSIST_MYSQL_HOST"):
        return {
            "host": os.environ["SHOPASSIST_MYSQL_HOST"],
            "port": int(os.environ.get("SHOPASSIST_MYSQL_PORT", "3306")),
            "user": os.environ.get("SHOPASSIST_MYSQL_USER", "root"),
            "password": os.environ.get("SHOPASSIST_MYSQL_PASSWORD", ""),
            "database": os.environ.get("SHOPASSIST_MYSQL_DATABASE", "shopassist"),
        }
    socket = "/var/run/mysqld/mysqld.sock"
    if not os.path.exists(socket):
        return None
    try:
        subprocess.run(["sudo", "mysql", f"--socket={socket}", "-e", "SELECT 1"], check=True, capture_output=True)
    except Exception:
        return None
    password = secrets.token_hex(16)
    sql = (
        "CREATE DATABASE IF NOT EXISTS shopassist_it;"
        f"CREATE USER IF NOT EXISTS 'shopassist_it'@'127.0.0.1' IDENTIFIED BY '{password}';"
        f"ALTER USER 'shopassist_it'@'127.0.0.1' IDENTIFIED BY '{password}';"
        "GRANT ALL ON shopassist_it.* TO 'shopassist_it'@'127.0.0.1';"
        "FLUSH PRIVILEGES;"
    )
    subprocess.run(["sudo", "mysql", f"--socket={socket}", "-e", sql], check=True, capture_output=True)
    return {"host": "127.0.0.1", "port": 3306, "user": "shopassist_it", "password": password, "database": "shopassist_it"}


class ServiceIntegrationTest(unittest.TestCase):
    def test_mysql_and_redis_roundtrip(self):
        settings = _mysql_settings()
        redis_ok = _redis_ok()
        if settings is None or not redis_ok:
            if os.environ.get("SHOPASSIST_REQUIRE_INTEGRATION") == "1":
                self.fail(f"MySQL/Redis integration required but mysql={settings is not None} redis={redis_ok}")
            self.skipTest(
                f"MySQL/Redis integration skipped: mysql_reachable={settings is not None} redis_reachable={redis_ok}. "
                "Offline CI uses local_sqlite and local_memory_cache."
            )
        from shopassist.storage.mysql_store import MysqlStore

        store = MysqlStore(**settings)
        try:
            message_id = store.append_message("it", "user", "sess", "user", "hello", "2026-01-15T00:00:00Z")
            again = store.append_message("it", "user", "sess", "user", "hello", "2026-01-15T00:00:00Z", message_id=message_id)
            self.assertEqual(message_id, again)
            version = store.save_summary("it", "sess", 0, message_id, "durable summary", "mock", {"source_kind": "mock"}, "2026-01-15T00:00:00Z")
            self.assertEqual(version, 1)
            with self.assertRaises(StaleWriteError):
                store.save_summary("it", "sess", 0, message_id, "stale", "mock", {"source_kind": "mock"}, "2026-01-15T00:00:00Z")
            cache = RedisCache(os.environ.get("SHOPASSIST_REDIS_URL", "redis://127.0.0.1:6379/0"))
            self.assertTrue(cache.compare_and_set("it-summary", 0, {"summary_text": "v", "watermark_message_id": 1, "source_kind": "mock"}))
            self.assertFalse(cache.compare_and_set("it-summary", 0, {"summary_text": "other", "watermark_message_id": 1, "source_kind": "mock"}))
            cache.delete("it-summary")
            self.assertIsNone(cache.get("it-summary"))
            restored = store.load_summary("it", "sess")
            self.assertEqual(restored["summary_text"], "durable summary")
            store.reset_session("it", "sess")
        finally:
            store.close()
