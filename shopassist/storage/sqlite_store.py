"""Offline durable adapter. This is local SQLite, not a MySQL server."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from shopassist.storage.errors import StaleWriteError
from shopassist.storage.schema import SCHEMA_VERSION, SQLITE_MIGRATION


class SqliteStore:
    kind = "local_sqlite"

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SQLITE_MIGRATION)
            row = self._conn.execute("SELECT version FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, "migration"),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append_message(self, tenant_id, user_id, session_id, role, body, created_at, message_id=None) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("message_role")
        with self._lock:
            if message_id is not None:
                existing = self._conn.execute(
                    "SELECT role, body FROM session_messages WHERE tenant_id=? AND session_id=? AND message_id=?",
                    (tenant_id, session_id, message_id),
                ).fetchone()
                if existing is not None:
                    if existing["role"] != role or existing["body"] != body:
                        raise StaleWriteError("message", int(message_id))
                    return int(message_id)
                self._conn.execute(
                    "INSERT INTO session_messages (tenant_id, user_id, session_id, message_id, role, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, user_id, session_id, message_id, role, body, created_at),
                )
                self._conn.commit()
                return int(message_id)
            current = self._conn.execute(
                "SELECT COALESCE(MAX(message_id), 0) AS max_id FROM session_messages WHERE tenant_id=? AND session_id=?",
                (tenant_id, session_id),
            ).fetchone()
            next_id = int(current["max_id"]) + 1
            self._conn.execute(
                "INSERT INTO session_messages (tenant_id, user_id, session_id, message_id, role, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, user_id, session_id, next_id, role, body, created_at),
            )
            self._conn.commit()
            return next_id

    def list_messages(self, tenant_id, session_id) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_id, role, body, created_at, user_id FROM session_messages WHERE tenant_id=? AND session_id=? ORDER BY message_id ASC",
                (tenant_id, session_id),
            ).fetchall()
        return [_message_row(row) for row in rows]

    def load_state(self, tenant_id, session_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT version, state_json, updated_at FROM session_state WHERE tenant_id=? AND session_id=?",
                (tenant_id, session_id),
            ).fetchone()
        if row is None:
            return None
        return {"version": int(row["version"]), "state": json.loads(row["state_json"]), "updated_at": row["updated_at"]}

    def save_state(self, tenant_id, session_id, expected_version: int, state: dict, updated_at: str) -> int:
        payload = json.dumps(state, sort_keys=True)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT version FROM session_state WHERE tenant_id=? AND session_id=?",
                    (tenant_id, session_id),
                ).fetchone()
                current = 0 if row is None else int(row["version"])
                if current != expected_version:
                    self._conn.rollback()
                    raise StaleWriteError("session_state", expected_version)
                new_version = expected_version + 1
                if row is None:
                    self._conn.execute(
                        "INSERT INTO session_state (tenant_id, session_id, version, state_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (tenant_id, session_id, new_version, payload, updated_at),
                    )
                else:
                    updated = self._conn.execute(
                        "UPDATE session_state SET version=?, state_json=?, updated_at=? WHERE tenant_id=? AND session_id=? AND version=?",
                        (new_version, payload, updated_at, tenant_id, session_id, expected_version),
                    )
                    if updated.rowcount != 1:
                        self._conn.rollback()
                        raise StaleWriteError("session_state", expected_version)
                self._conn.commit()
                return new_version
            except StaleWriteError:
                raise
            except Exception:
                self._conn.rollback()
                raise

    def load_summary(self, tenant_id, session_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT version, watermark_message_id, summary_text, source_kind, provenance_json, created_at FROM session_summaries WHERE tenant_id=? AND session_id=? ORDER BY version DESC LIMIT 1",
                (tenant_id, session_id),
            ).fetchone()
        if row is None:
            return None
        return _summary_row(row)

    def save_summary(self, tenant_id, session_id, expected_version: int, watermark_message_id: int, summary_text: str, source_kind: str, provenance: dict, created_at: str) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM session_summaries WHERE tenant_id=? AND session_id=?",
                    (tenant_id, session_id),
                ).fetchone()
                current = int(row["version"])
                if current != expected_version:
                    self._conn.rollback()
                    raise StaleWriteError("session_summary", expected_version)
                new_version = expected_version + 1
                self._conn.execute(
                    "INSERT INTO session_summaries (tenant_id, session_id, version, watermark_message_id, summary_text, source_kind, provenance_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, session_id, new_version, watermark_message_id, summary_text, source_kind, json.dumps(provenance, sort_keys=True), created_at),
                )
                self._conn.commit()
                return new_version
            except StaleWriteError:
                raise
            except Exception:
                self._conn.rollback()
                raise

    def load_profile(self, tenant_id, user_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT version, preferences_json, updated_at FROM user_profiles WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id),
            ).fetchone()
        if row is None:
            return {"version": 0, "preferences": {}, "updated_at": None}
        return {"version": int(row["version"]), "preferences": json.loads(row["preferences_json"]), "updated_at": row["updated_at"]}

    def save_profile(self, tenant_id, user_id, expected_version: int, preferences: dict, updated_at: str) -> int:
        payload = json.dumps(preferences, sort_keys=True)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT version FROM user_profiles WHERE tenant_id=? AND user_id=?",
                    (tenant_id, user_id),
                ).fetchone()
                current = 0 if row is None else int(row["version"])
                if current != expected_version:
                    self._conn.rollback()
                    raise StaleWriteError("profile", expected_version)
                new_version = expected_version + 1
                if row is None:
                    self._conn.execute(
                        "INSERT INTO user_profiles (tenant_id, user_id, version, preferences_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (tenant_id, user_id, new_version, payload, updated_at),
                    )
                else:
                    updated = self._conn.execute(
                        "UPDATE user_profiles SET version=?, preferences_json=?, updated_at=? WHERE tenant_id=? AND user_id=? AND version=?",
                        (new_version, payload, updated_at, tenant_id, user_id, expected_version),
                    )
                    if updated.rowcount != 1:
                        self._conn.rollback()
                        raise StaleWriteError("profile", expected_version)
                self._conn.commit()
                return new_version
            except StaleWriteError:
                raise
            except Exception:
                self._conn.rollback()
                raise

    def upsert_recent(self, tenant_id, user_id, memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO recent_memories (tenant_id, user_id, memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, user_id, memory_id) DO NOTHING",
                (tenant_id, user_id, memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at),
            )
            self._conn.commit()

    def select_recent(self, tenant_id, user_id, now_iso: str, window_start_iso: str, limit: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT memory_id, session_id, source_message_id, body, kind, relevance, created_at, expires_at
                FROM recent_memories
                WHERE tenant_id=? AND user_id=? AND created_at>=? AND created_at<=? AND expires_at>?
                ORDER BY relevance DESC, created_at DESC, memory_id ASC
                LIMIT ?
                """,
                (tenant_id, user_id, window_start_iso, now_iso, now_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def reset_session(self, tenant_id, session_id) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM session_messages WHERE tenant_id=? AND session_id=?", (tenant_id, session_id))
            self._conn.execute("DELETE FROM session_state WHERE tenant_id=? AND session_id=?", (tenant_id, session_id))
            self._conn.execute("DELETE FROM session_summaries WHERE tenant_id=? AND session_id=?", (tenant_id, session_id))
            self._conn.commit()


def _message_row(row) -> dict:
    return {
        "message_id": int(row["message_id"]),
        "role": row["role"],
        "body": row["body"],
        "created_at": row["created_at"],
        "user_id": row["user_id"],
    }


def _summary_row(row) -> dict:
    return {
        "version": int(row["version"]),
        "watermark_message_id": int(row["watermark_message_id"]),
        "summary_text": row["summary_text"],
        "source_kind": row["source_kind"],
        "provenance": json.loads(row["provenance_json"]),
        "created_at": row["created_at"],
    }
