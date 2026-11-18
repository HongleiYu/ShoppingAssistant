"""MySQL durable adapter. Connections use parameterized statements.

This module talks to a MySQL-compatible server. It does not emulate one.
"""

from __future__ import annotations

import json

from shopassist.storage.errors import StaleWriteError, StoreUnavailable
from shopassist.storage.schema import MYSQL_MIGRATION, SCHEMA_VERSION


class MysqlStore:
    kind = "mysql"

    def __init__(self, host: str, port: int, user: str, password: str, database: str, connect_timeout: float = 3):
        try:
            import pymysql
        except ImportError as exc:
            raise StoreUnavailable(
                "MySQL mode needs the pymysql package. "
                "Install it for a real MySQL server, or set SHOPASSIST_STORE=local_sqlite. "
                "Refusing to use another database and call it MySQL."
            ) from exc
        try:
            self._conn = pymysql.connect(
                host=host,
                port=int(port),
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
                autocommit=False,
                connect_timeout=connect_timeout,
                cursorclass=pymysql.cursors.DictCursor,
            )
        except Exception as exc:
            raise StoreUnavailable(
                f"MySQL store selected but the server is not reachable at {host}:{port}. "
                "Start a local disposable server or set SHOPASSIST_STORE=local_sqlite. "
                "Refusing to pretend a MySQL backend is active."
            ) from exc
        self._pymysql = pymysql
        self.migrate()

    def migrate(self) -> None:
        statements = [part.strip() for part in MYSQL_MIGRATION.split(";") if part.strip()]
        with self._conn.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            cursor.execute("SELECT version FROM schema_migrations WHERE version=%s", (SCHEMA_VERSION,))
            if cursor.fetchone() is None:
                cursor.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (SCHEMA_VERSION, "migration"),
                )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def append_message(self, tenant_id, user_id, session_id, role, body, created_at, message_id=None) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("message_role")
        with self._conn.cursor() as cursor:
            if message_id is not None:
                cursor.execute(
                    "SELECT role, body FROM session_messages WHERE tenant_id=%s AND session_id=%s AND message_id=%s",
                    (tenant_id, session_id, message_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing["role"] != role or existing["body"] != body:
                        raise StaleWriteError("message", int(message_id))
                    return int(message_id)
                cursor.execute(
                    "INSERT INTO session_messages (tenant_id, user_id, session_id, message_id, role, body, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (tenant_id, user_id, session_id, message_id, role, body, created_at),
                )
                self._conn.commit()
                return int(message_id)
            cursor.execute(
                "SELECT COALESCE(MAX(message_id), 0) AS max_id FROM session_messages WHERE tenant_id=%s AND session_id=%s",
                (tenant_id, session_id),
            )
            next_id = int(cursor.fetchone()["max_id"]) + 1
            cursor.execute(
                "INSERT INTO session_messages (tenant_id, user_id, session_id, message_id, role, body, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, user_id, session_id, next_id, role, body, created_at),
            )
        self._conn.commit()
        return next_id

    def list_messages(self, tenant_id, session_id) -> list[dict]:
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT message_id, role, body, created_at, user_id FROM session_messages WHERE tenant_id=%s AND session_id=%s ORDER BY message_id ASC",
                (tenant_id, session_id),
            )
            rows = cursor.fetchall()
        return [
            {
                "message_id": int(row["message_id"]),
                "role": row["role"],
                "body": row["body"],
                "created_at": row["created_at"],
                "user_id": row["user_id"],
            }
            for row in rows
        ]

    def load_state(self, tenant_id, session_id):
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT version, state_json, updated_at FROM session_state WHERE tenant_id=%s AND session_id=%s",
                (tenant_id, session_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {"version": int(row["version"]), "state": json.loads(row["state_json"]), "updated_at": row["updated_at"]}

    def save_state(self, tenant_id, session_id, expected_version: int, state: dict, updated_at: str) -> int:
        payload = json.dumps(state, sort_keys=True)
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT version FROM session_state WHERE tenant_id=%s AND session_id=%s FOR UPDATE",
                (tenant_id, session_id),
            )
            row = cursor.fetchone()
            current = 0 if row is None else int(row["version"])
            if current != expected_version:
                self._conn.rollback()
                raise StaleWriteError("session_state", expected_version)
            new_version = expected_version + 1
            if row is None:
                cursor.execute(
                    "INSERT INTO session_state (tenant_id, session_id, version, state_json, updated_at) VALUES (%s, %s, %s, %s, %s)",
                    (tenant_id, session_id, new_version, payload, updated_at),
                )
            else:
                updated = cursor.execute(
                    "UPDATE session_state SET version=%s, state_json=%s, updated_at=%s WHERE tenant_id=%s AND session_id=%s AND version=%s",
                    (new_version, payload, updated_at, tenant_id, session_id, expected_version),
                )
                if cursor.rowcount != 1:
                    self._conn.rollback()
                    raise StaleWriteError("session_state", expected_version)
            self._conn.commit()
            return new_version

    def load_summary(self, tenant_id, session_id):
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT version, watermark_message_id, summary_text, source_kind, provenance_json, created_at FROM session_summaries WHERE tenant_id=%s AND session_id=%s ORDER BY version DESC LIMIT 1",
                (tenant_id, session_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "version": int(row["version"]),
            "watermark_message_id": int(row["watermark_message_id"]),
            "summary_text": row["summary_text"],
            "source_kind": row["source_kind"],
            "provenance": json.loads(row["provenance_json"]),
            "created_at": row["created_at"],
        }

    def save_summary(self, tenant_id, session_id, expected_version: int, watermark_message_id: int, summary_text: str, source_kind: str, provenance: dict, created_at: str) -> int:
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM session_summaries WHERE tenant_id=%s AND session_id=%s FOR UPDATE",
                (tenant_id, session_id),
            )
            current = int(cursor.fetchone()["version"])
            if current != expected_version:
                self._conn.rollback()
                raise StaleWriteError("session_summary", expected_version)
            new_version = expected_version + 1
            cursor.execute(
                "INSERT INTO session_summaries (tenant_id, session_id, version, watermark_message_id, summary_text, source_kind, provenance_json, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, session_id, new_version, watermark_message_id, summary_text, source_kind, json.dumps(provenance, sort_keys=True), created_at),
            )
        self._conn.commit()
        return new_version

    def load_profile(self, tenant_id, user_id):
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT version, preferences_json, updated_at FROM user_profiles WHERE tenant_id=%s AND user_id=%s",
                (tenant_id, user_id),
            )
            row = cursor.fetchone()
        if row is None:
            return {"version": 0, "preferences": {}, "updated_at": None}
        return {"version": int(row["version"]), "preferences": json.loads(row["preferences_json"]), "updated_at": row["updated_at"]}

    def save_profile(self, tenant_id, user_id, expected_version: int, preferences: dict, updated_at: str) -> int:
        payload = json.dumps(preferences, sort_keys=True)
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT version FROM user_profiles WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
                (tenant_id, user_id),
            )
            row = cursor.fetchone()
            current = 0 if row is None else int(row["version"])
            if current != expected_version:
                self._conn.rollback()
                raise StaleWriteError("profile", expected_version)
            new_version = expected_version + 1
            if row is None:
                cursor.execute(
                    "INSERT INTO user_profiles (tenant_id, user_id, version, preferences_json, updated_at) VALUES (%s, %s, %s, %s, %s)",
                    (tenant_id, user_id, new_version, payload, updated_at),
                )
            else:
                cursor.execute(
                    "UPDATE user_profiles SET version=%s, preferences_json=%s, updated_at=%s WHERE tenant_id=%s AND user_id=%s AND version=%s",
                    (new_version, payload, updated_at, tenant_id, user_id, expected_version),
                )
                if cursor.rowcount != 1:
                    self._conn.rollback()
                    raise StaleWriteError("profile", expected_version)
        self._conn.commit()
        return new_version

    def upsert_recent(self, tenant_id, user_id, memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(
                "INSERT IGNORE INTO recent_memories (tenant_id, user_id, memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, user_id, memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at),
            )
        self._conn.commit()

    def select_recent(self, tenant_id, user_id, now_iso: str, window_start_iso: str, limit: int) -> list[dict]:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at
                FROM recent_memories
                WHERE tenant_id=%s AND user_id=%s AND created_at>=%s AND created_at<=%s AND expires_at>%s
                ORDER BY relevance DESC, created_at DESC, memory_id ASC
                LIMIT %s
                """,
                (tenant_id, user_id, window_start_iso, now_iso, now_iso, limit),
            )
            rows = cursor.fetchall()
        return list(rows)

    def reset_session(self, tenant_id, session_id) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute("DELETE FROM session_messages WHERE tenant_id=%s AND session_id=%s", (tenant_id, session_id))
            cursor.execute("DELETE FROM session_state WHERE tenant_id=%s AND session_id=%s", (tenant_id, session_id))
            cursor.execute("DELETE FROM session_summaries WHERE tenant_id=%s AND session_id=%s", (tenant_id, session_id))
        self._conn.commit()
