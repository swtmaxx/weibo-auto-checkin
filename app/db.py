from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cookie_ciphertext TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    last_verified_at TEXT,
                    logged_in INTEGER NOT NULL DEFAULT 0,
                    login_uid TEXT,
                    login_name TEXT,
                    verification_message TEXT,
                    cookie_expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS topics (
                    topic_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    remote_status TEXT NOT NULL DEFAULT 'unknown',
                    checkin_scheme TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_result TEXT
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    summary_json TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    run_time TEXT NOT NULL DEFAULT '09:00',
                    last_run_date TEXT
                );

                CREATE TABLE IF NOT EXISTS qq_openids (
                    user_openid TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'C2C_MESSAGE_CREATE'
                );

                INSERT OR IGNORE INTO schedule (id) VALUES (1);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(account)").fetchall()
            }
            if "cookie_expires_at" not in columns:
                conn.execute("ALTER TABLE account ADD COLUMN cookie_expires_at TEXT")

    def get_config(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_config(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def delete_config(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM app_config WHERE key = ?", (key,))

    def get_json_config(self, key: str) -> dict[str, Any] | None:
        value = self.get_config(key)
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def set_json_config(self, key: str, value: dict[str, Any]) -> None:
        self.set_config(key, json.dumps(value, ensure_ascii=False))

    def has_admin_password(self) -> bool:
        return bool(self.get_config("admin_password_hash"))

    def set_admin_password(self, password_hash: str) -> None:
        self.set_config("admin_password_hash", password_hash)

    def get_account(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
            return dict(row) if row else None

    def save_cookie(self, ciphertext: str, expires_at: str | None = None) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account (
                    id, cookie_ciphertext, imported_at, logged_in, verification_message,
                    cookie_expires_at
                ) VALUES (1, ?, ?, 0, '尚未验证', ?)
                ON CONFLICT(id) DO UPDATE SET
                    cookie_ciphertext = excluded.cookie_ciphertext,
                    imported_at = excluded.imported_at,
                    last_verified_at = NULL,
                    logged_in = 0,
                    login_uid = NULL,
                    login_name = NULL,
                    verification_message = excluded.verification_message,
                    cookie_expires_at = excluded.cookie_expires_at
                """,
                (ciphertext, now, expires_at),
            )

    def restore_cookie(
        self,
        ciphertext: str,
        imported_at: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Persist a cookie ciphertext coming from a configuration backup."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account (
                    id, cookie_ciphertext, imported_at, logged_in, verification_message,
                    cookie_expires_at
                ) VALUES (1, ?, ?, 0, '已从备份导入，待验证', ?)
                ON CONFLICT(id) DO UPDATE SET
                    cookie_ciphertext = excluded.cookie_ciphertext,
                    imported_at = excluded.imported_at,
                    last_verified_at = NULL,
                    logged_in = 0,
                    login_uid = NULL,
                    login_name = NULL,
                    verification_message = excluded.verification_message,
                    cookie_expires_at = excluded.cookie_expires_at
                """,
                (ciphertext, imported_at or utc_now(), expires_at),
            )

    def update_verification(
        self,
        logged_in: bool,
        message: str,
        uid: str | None = None,
        name: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE account
                SET last_verified_at = ?, logged_in = ?, login_uid = ?,
                    login_name = ?, verification_message = ?
                WHERE id = 1
                """,
                (utc_now(), int(logged_in), uid, name, message),
            )

    def clear_cookie(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM account WHERE id = 1")

    def upsert_topics(self, topics: list[dict[str, Any]]) -> None:
        if not topics:
            return
        now = utc_now()
        with self._lock, self._connect() as conn:
            for topic in topics:
                conn.execute(
                    """
                    INSERT INTO topics (
                        topic_key, name, description, enabled, remote_status,
                        checkin_scheme, first_seen_at, last_seen_at, last_result
                    ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic_key) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        remote_status = excluded.remote_status,
                        checkin_scheme = excluded.checkin_scheme,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        topic["topic_key"],
                        topic["name"],
                        topic.get("description", ""),
                        topic.get("remote_status", "unknown"),
                        topic.get("checkin_scheme"),
                        now,
                        now,
                        topic.get("last_result"),
                    ),
                )

    def list_topics(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT topic_key, name, description, enabled, remote_status,
                       checkin_scheme, first_seen_at, last_seen_at, last_result
                FROM topics ORDER BY name COLLATE NOCASE, topic_key
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_topic(self, topic_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT topic_key, name, description, enabled, remote_status,
                       checkin_scheme, first_seen_at, last_seen_at, last_result
                FROM topics WHERE topic_key = ?
                """,
                (topic_key,),
            ).fetchone()
            return dict(row) if row else None

    def set_topics_enabled(self, topic_keys: list[str], enabled: bool) -> int:
        if not topic_keys:
            return 0
        with self._lock, self._connect() as conn:
            cursor = conn.executemany(
                "UPDATE topics SET enabled = ? WHERE topic_key = ?",
                [(int(enabled), key) for key in topic_keys],
            )
            return max(0, cursor.rowcount)

    def update_topic_enabled(self, topic_key: str, enabled: bool) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE topics SET enabled = ? WHERE topic_key = ?",
                (int(enabled), topic_key),
            )
            return cursor.rowcount == 1

    def update_topic_result(
        self,
        topic_key: str,
        remote_status: str,
        last_result: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE topics
                SET remote_status = ?, last_result = ?, last_seen_at = ?
                WHERE topic_key = ?
                """,
                (remote_status, last_result, utc_now(), topic_key),
            )

    def create_run(self, kind: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runs(kind, status, created_at)
                VALUES (?, 'queued', ?)
                """,
                (kind, utc_now()),
            )
            return int(cursor.lastrowid)

    def start_run(self, run_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = 'running', started_at = ? WHERE id = ?",
                (utc_now(), run_id),
            )

    def finish_run(
        self,
        run_id: int,
        status: str,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, summary_json = ?, error = ?
                WHERE id = ?
                """,
                (status, utc_now(), json.dumps(summary or {}, ensure_ascii=False), error, run_id),
            )

    def recover_running_runs(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?, error = '服务重启时任务未完成'
                WHERE status IN ('queued', 'running')
                """,
                (utc_now(),),
            )

    def add_log(self, run_id: int, level: str, message: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_logs(run_id, created_at, level, message)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, utc_now(), level, message),
            )

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["summary"] = json.loads(result.pop("summary_json") or "{}")
            result["logs"] = [
                dict(log)
                for log in conn.execute(
                    """
                    SELECT created_at, level, message
                    FROM run_logs WHERE run_id = ? ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
            ]
            return result

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, kind, status, created_at, started_at, finished_at,
                       summary_json, error
                FROM runs ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["summary"] = json.loads(item.pop("summary_json") or "{}")
                results.append(item)
            return results

    def current_run(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM runs
                WHERE status IN ('queued', 'running')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return self.get_run(int(row["id"])) if row else None

    def prune_history(self, retention_days: int) -> int:
        """Delete runs (and cascaded logs) older than the retention window."""
        if retention_days <= 0:
            return 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM runs WHERE created_at < ?", (cutoff,))
            return max(0, cursor.rowcount)

    def compute_stats(self, timezone_name: str, days: int = 7) -> dict[str, Any]:
        """Aggregate recent check-in outcomes and the current sign-in streak."""
        try:
            zone = ZoneInfo(timezone_name)
        except Exception:
            zone = timezone.utc
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, created_at, summary_json FROM runs
                WHERE created_at >= ? AND kind IN ('checkin', 'scheduled')
                """,
                (cutoff,),
            ).fetchall()
        totals = {"success": 0, "already": 0, "failed": 0}
        completed_runs = 0
        run_days: set[str] = set()
        for row in rows:
            if row["status"] != "completed":
                continue
            completed_runs += 1
            try:
                summary = json.loads(row["summary_json"] or "{}")
            except json.JSONDecodeError:
                summary = {}
            for key in totals:
                totals[key] += max(0, int(summary.get(key) or 0))
            try:
                local_day = (
                    datetime.fromisoformat(row["created_at"]).astimezone(zone).date()
                )
            except ValueError:
                continue
            run_days.add(local_day.isoformat())
        attempted = totals["success"] + totals["already"] + totals["failed"]
        streak = 0
        day = datetime.now(zone).date()
        if day.isoformat() not in run_days:
            day -= timedelta(days=1)
        while day.isoformat() in run_days and streak < 366:
            streak += 1
            day -= timedelta(days=1)
        return {
            "days": days,
            "completed_runs": completed_runs,
            "success": totals["success"],
            "already": totals["already"],
            "failed": totals["failed"],
            "success_rate": (totals["success"] + totals["already"]) / attempted if attempted else None,
            "streak_days": streak,
        }

    def get_schedule(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM schedule WHERE id = 1").fetchone()
            return dict(row)

    def save_schedule(self, enabled: bool, run_time: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE schedule SET enabled = ?, run_time = ? WHERE id = 1
                """,
                (int(enabled), run_time),
            )

    def mark_schedule_run(self, run_date: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE schedule SET last_run_date = ? WHERE id = 1", (run_date,))

    def save_qq_openid(self, user_openid: str, source: str = "C2C_MESSAGE_CREATE") -> None:
        user_openid = user_openid.strip()
        source = source.strip() or "C2C_MESSAGE_CREATE"
        if not user_openid or len(user_openid) > 256:
            return
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO qq_openids(user_openid, first_seen_at, last_seen_at, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_openid) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    source = excluded.source
                """,
                (user_openid, now, now, source[:128]),
            )

    def list_qq_openids(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_openid, first_seen_at, last_seen_at, source
                FROM qq_openids
                ORDER BY last_seen_at DESC, user_openid
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
