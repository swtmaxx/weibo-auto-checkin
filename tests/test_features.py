from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database, utc_now
from app.main import create_app
from app.security import LoginThrottle, decrypt_cookie, encrypt_cookie
from app.tasks import Scheduler, TaskManager
from app.weibo import CheckinResult, LoginStatus


# ---------------------------------------------------------------------------
# History retention
# ---------------------------------------------------------------------------


def _insert_run(db_path: Path, kind: str, status: str, created_at: str, summary: dict) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO runs(kind, status, created_at, summary_json) VALUES (?, ?, ?, ?)",
            (kind, status, created_at, json.dumps(summary)),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def test_prune_history_deletes_only_old_runs(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    old_created = (
        datetime.now(timezone.utc) - timedelta(days=120)
    ).isoformat(timespec="seconds")
    old_id = _insert_run(database.path, "checkin", "completed", old_created, {"success": 1})
    recent_id = _insert_run(database.path, "checkin", "completed", utc_now(), {"success": 1})
    database.add_log(old_id, "INFO", "旧日志")

    assert database.prune_history(90) == 1
    assert database.get_run(old_id) is None
    assert database.get_run(recent_id) is not None
    assert database.prune_history(0) == 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def _local_created_at(zone: ZoneInfo, days_ago: int, hour: int) -> str:
    local_day = (datetime.now(zone) - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return local_day.astimezone(timezone.utc).isoformat(timespec="seconds")


def test_compute_stats_success_rate_and_streak(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    zone = ZoneInfo("Asia/Shanghai")
    rows = [
        ("checkin", "completed", _local_created_at(zone, 0, 9), {"success": 1, "failed": 1}),
        ("checkin", "completed", _local_created_at(zone, 0, 10), {"success": 2, "already": 1}),
        ("checkin", "completed", _local_created_at(zone, 1, 9), {"already": 2}),
        ("checkin", "completed", _local_created_at(zone, 3, 9), {"failed": 3}),
        ("checkin", "completed", _local_created_at(zone, 10, 9), {"success": 5}),
        ("checkin", "failed", _local_created_at(zone, 0, 8), {}),
        ("sync", "completed", _local_created_at(zone, 0, 8), {"discovered": 5}),
    ]
    for kind, status, created, summary in rows:
        _insert_run(database.path, kind, status, created, summary)

    stats = database.compute_stats("Asia/Shanghai")

    assert stats["success"] == 3
    assert stats["already"] == 3
    assert stats["failed"] == 4
    assert stats["success_rate"] == 0.6
    assert stats["streak_days"] == 2
    assert stats["completed_runs"] == 4


def test_compute_stats_without_runs(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    stats = database.compute_stats("Asia/Shanghai")
    assert stats["success_rate"] is None
    assert stats["streak_days"] == 0


# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------


def test_get_topic_and_bulk_enable(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_topics(
        [
            {"topic_key": "a", "name": "超话A", "remote_status": "available", "checkin_scheme": "/s"},
            {"topic_key": "b", "name": "超话B", "remote_status": "unknown", "checkin_scheme": None},
        ]
    )

    assert database.get_topic("a")["name"] == "超话A"
    assert database.get_topic("missing") is None
    assert database.set_topics_enabled(["a", "b", "missing"], True) == 2
    assert all(topic["enabled"] == 1 for topic in database.list_topics())
    assert database.set_topics_enabled([], True) == 0


# ---------------------------------------------------------------------------
# Login throttle
# ---------------------------------------------------------------------------


def test_login_throttle_increasing_delay_and_reset():
    clock = {"now": 0.0}
    throttle = LoginThrottle(clock=lambda: clock["now"])

    assert throttle.delay_for("ip") == 0.0
    throttle.record_failure("ip")
    assert throttle.delay_for("ip") == 1.0
    throttle.record_failure("ip")
    assert throttle.delay_for("ip") == 2.0

    throttle.reset("ip")
    assert throttle.delay_for("ip") == 0.0

    for _ in range(6):
        throttle.record_failure("ip")
    assert throttle.delay_for("ip") == 15.0

    clock["now"] += 901
    assert throttle.delay_for("ip") == 0.0


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def test_scheduler_should_fire():
    assert Scheduler.should_fire("09:15", "09:00", None, "2026-08-29", True) is True
    assert Scheduler.should_fire("08:59", "09:00", None, "2026-08-29", True) is False
    assert Scheduler.should_fire("09:15", "09:00", "2026-08-29", "2026-08-29", True) is False
    assert Scheduler.should_fire("10:00", "09:00", None, "2026-08-29", False) is False
    assert Scheduler.should_fire("09:00", "09:00", None, "2026-08-29", False) is True


# ---------------------------------------------------------------------------
# Single-topic check-in
# ---------------------------------------------------------------------------


class SingleClient:
    def __init__(self, cookie: str):
        self.checkins: list[str] = []

    def verify_login(self) -> LoginStatus:
        return LoginStatus(True, "1", "用户", "Cookie 有效")

    def checkin(self, scheme: str) -> CheckinResult:
        self.checkins.append(scheme)
        return CheckinResult("success", "签到成功", {})


def make_single_task_database(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    database.save_cookie(encrypt_cookie("SUB=abc", settings.secret_key))
    return settings, database


def wait_for_run(database: Database, run_id: int, timeout: float = 5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = database.get_run(run_id)
        if run and run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def test_single_topic_checkin_completes(tmp_path: Path):
    settings, database = make_single_task_database(tmp_path)
    database.upsert_topics(
        [
            {
                "topic_key": "t1",
                "name": "单超话",
                "remote_status": "unknown",
                "checkin_scheme": "/api/container/button?id=1",
            }
        ]
    )
    database.update_topic_enabled("t1", True)
    manager = TaskManager(database, settings, client_factory=SingleClient)

    run_id = manager.start("single", topic_key="t1")
    run = wait_for_run(database, run_id)

    assert run["status"] == "completed"
    assert run["kind"] == "single"
    assert run["summary"]["success"] == 1
    assert run["summary"]["selected"] == 1
    assert database.get_topic("t1")["remote_status"] == "signed"


def test_single_topic_without_scheme_fails(tmp_path: Path):
    settings, database = make_single_task_database(tmp_path)
    database.upsert_topics(
        [{"topic_key": "t2", "name": "无方案", "remote_status": "unknown", "checkin_scheme": None}]
    )
    manager = TaskManager(database, settings, client_factory=SingleClient)

    run_id = manager.start("single", topic_key="t2")
    run = wait_for_run(database, run_id)

    assert run["status"] == "failed"
    assert "scheme" in run["error"]
    assert database.get_topic("t2")["remote_status"] == "unknown"


def test_single_topic_missing_fails(tmp_path: Path):
    settings, database = make_single_task_database(tmp_path)
    manager = TaskManager(database, settings, client_factory=SingleClient)

    run_id = manager.start("single", topic_key="ghost")
    run = wait_for_run(database, run_id)

    assert run["status"] == "failed"
    assert "超话不存在" in run["error"]


# ---------------------------------------------------------------------------
# Config export / import
# ---------------------------------------------------------------------------


def make_test_client(settings: Settings, database: Database) -> TestClient:
    app = create_app(
        settings,
        db=database,
        start_scheduler=False,
        start_qq_listener=False,
    )
    return TestClient(app)


def setup_admin(client: TestClient) -> str:
    response = client.post("/api/auth/setup", json={"password": "a-long-admin-password"})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_config_export_import_round_trip(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "source.sqlite3",
        secret_key="shared-secret",
    )
    source = Database(settings.db_path)
    source.save_cookie(encrypt_cookie("SUB=abc; SUBP=def", settings.secret_key))
    source.upsert_topics(
        [
            {"topic_key": "t1", "name": "超话一", "remote_status": "unknown", "checkin_scheme": None},
            {"topic_key": "t2", "name": "超话二", "remote_status": "unknown", "checkin_scheme": None},
        ]
    )
    source.update_topic_enabled("t1", True)
    source.save_schedule(True, "08:30")

    with make_test_client(settings, source) as client:
        setup_admin(client)
        exported = client.get("/api/config/export")
        assert exported.status_code == 200
        payload = exported.json()

    assert payload["version"] == 1
    assert payload["secret_fingerprint"]

    target_settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "target.sqlite3",
        secret_key="shared-secret",
    )
    target = Database(target_settings.db_path)
    with make_test_client(target_settings, target) as client:
        csrf = setup_admin(client)
        imported = client.post(
            "/api/config/import",
            headers={"X-CSRF-Token": csrf},
            json=payload,
        )
        assert imported.status_code == 200

    assert imported.json()["topics"] == 2
    assert target.get_topic("t1")["enabled"] == 1
    assert target.get_topic("t2")["enabled"] == 0
    assert target.get_schedule()["run_time"] == "08:30"
    assert target.get_schedule()["enabled"] == 1
    assert target.get_json_config("runtime_settings") == payload["runtime_policy"]
    restored = decrypt_cookie(target.get_account()["cookie_ciphertext"], "shared-secret")
    assert "SUB=abc" in restored


def test_config_import_rejects_secret_mismatch(tmp_path: Path):
    source_settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "source.sqlite3",
        secret_key="secret-a",
    )
    source = Database(source_settings.db_path)
    source.save_cookie(encrypt_cookie("SUB=abc", "secret-a"))
    with make_test_client(source_settings, source) as client:
        setup_admin(client)
        payload = client.get("/api/config/export").json()

    target_settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "target.sqlite3",
        secret_key="secret-b",
    )
    target = Database(target_settings.db_path)
    with make_test_client(target_settings, target) as client:
        csrf = setup_admin(client)
        response = client.post(
            "/api/config/import",
            headers={"X-CSRF-Token": csrf},
            json=payload,
        )
        assert response.status_code == 422
        assert "APP_SECRET_KEY" in response.json()["detail"]


def test_config_import_rejects_bad_schedule(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    with make_test_client(settings, database) as client:
        csrf = setup_admin(client)
        response = client.post(
            "/api/config/import",
            headers={"X-CSRF-Token": csrf},
            json={"version": 1, "schedule": {"enabled": True, "run_time": "9:00"}},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "配置文件中的执行时间格式错误"
