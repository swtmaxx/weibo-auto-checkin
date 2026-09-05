from __future__ import annotations

import json
import random
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import RuntimePolicy, Settings, RuntimeState
from app.db import Database, utc_now
from app.main import create_app
from app.security import LoginThrottle, decrypt_cookie, encrypt_cookie
from app.tasks import Scheduler, TaskManager, jittered_delay, local_day_window
from app.weibo import CheckinResult, LoginStatus, TopicSnapshot, parse_cookie_expiry


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


# ---------------------------------------------------------------------------
# Cooldown configuration
# ---------------------------------------------------------------------------


def make_runtime_state(tmp_path: Path, **policy_overrides) -> RuntimeState:
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    state = RuntimeState(database, settings)
    if policy_overrides:
        policy, notification = state.snapshot()
        state.save(replace(policy, **policy_overrides), notification)
    return state


def test_set_cooldown_next_midnight_by_default(tmp_path: Path):
    state = make_runtime_state(tmp_path)
    status = state.set_cooldown("429")
    assert status["active"] is True
    local_until = datetime.fromisoformat(status["until"]).astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    assert (local_until.hour, local_until.minute) == (0, 0)


def test_set_cooldown_fixed_hours(tmp_path: Path):
    state = make_runtime_state(tmp_path, cooldown_hours=2)
    before = datetime.now(timezone.utc)
    status = state.set_cooldown("429")
    until = datetime.fromisoformat(status["until"])
    delta = until - before
    assert timedelta(hours=1, minutes=55) < delta < timedelta(hours=2, minutes=5)


def test_policy_rejects_out_of_range_cooldown_hours(tmp_path: Path):
    state = make_runtime_state(tmp_path)
    policy, _ = state.snapshot()
    try:
        RuntimePolicy.from_mapping({"cooldown_hours": 500}, fallback=policy)
    except ValueError as exc:
        assert "冷却时长" in str(exc)
    else:
        raise AssertionError("cooldown_hours=500 should be rejected")


def test_cooldown_hours_env_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_COOLDOWN_HOURS", "6")
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    assert RuntimePolicy.defaults(settings).cooldown_hours == 6


def test_clear_cooldown_endpoint(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    with make_test_client(settings, database) as client:
        csrf = setup_admin(client)
        app = client.app
        runtime_state = app.state.runtime_state
        runtime_state.set_cooldown("429")
        assert runtime_state.is_cooling_down() is True

        response = client.post(
            "/api/cooldown/clear",
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert response.json()["cooldown"]["active"] is False
        assert runtime_state.is_cooling_down() is False


# ---------------------------------------------------------------------------
# Random delays
# ---------------------------------------------------------------------------


def test_jittered_delay_zero_percent_is_exact():
    assert jittered_delay(10.0, 0) == 10.0
    assert jittered_delay(10.0, -5) == 10.0


def test_jittered_delay_uses_symmetric_range(monkeypatch):
    seen = {}
    monkeypatch.setattr(random, "uniform", lambda a, b: (seen.update(a=a, b=b), 0.9)[1])
    assert jittered_delay(10.0, 25) == 9.0
    assert (seen["a"], seen["b"]) == (0.75, 1.25)


def test_scheduler_jittered_fire_time(monkeypatch):
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 29, 8, 0, tzinfo=zone)
    monkeypatch.setattr(random, "uniform", lambda a, b: 300.0)
    fire_at = Scheduler.jittered_fire_time(now, "09:00", 10)
    assert fire_at == datetime(2026, 8, 29, 9, 5, tzinfo=zone)


def test_policy_rejects_out_of_range_random_delays(tmp_path: Path):
    state = make_runtime_state(tmp_path)
    policy, _ = state.snapshot()
    for field, value, keyword in (
        ("delay_jitter_percent", 150, "抖动"),
        ("schedule_jitter_minutes", 500, "随机延迟"),
    ):
        try:
            RuntimePolicy.from_mapping({field: value}, fallback=policy)
        except ValueError as exc:
            assert keyword in str(exc)
        else:
            raise AssertionError(f"{field}={value} should be rejected")


# ---------------------------------------------------------------------------
# Cookie expiry countdown
# ---------------------------------------------------------------------------


def test_parse_cookie_expiry():
    assert parse_cookie_expiry("SUB=a; ALF=1790526381; MLOGIN=1") == 1790526381
    assert parse_cookie_expiry("SUB=a") is None
    assert parse_cookie_expiry("ALF=not-a-number") is None
    assert parse_cookie_expiry("alf=123") is None  # 大小写敏感,与真实字段一致


def test_save_cookie_stores_expiry(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    database.save_cookie(
        encrypt_cookie("SUB=a; ALF=1790526381", settings.secret_key),
        "2026-09-28T02:26:21+00:00",
    )
    account = database.get_account()
    assert account["cookie_expires_at"] == "2026-09-28T02:26:21+00:00"

    database.save_cookie(encrypt_cookie("SUB=b", settings.secret_key), None)
    assert database.get_account()["cookie_expires_at"] is None


def test_account_api_returns_expiry(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    with make_test_client(settings, database) as client:
        csrf = setup_admin(client)
        response = client.post(
            "/api/account/cookie",
            headers={"X-CSRF-Token": csrf},
            json={"cookie": "SUB=a; ALF=1790526381"},
        )
        assert response.status_code == 200
        account = response.json()["account"]
        expected = datetime.fromtimestamp(1790526381, tz=timezone.utc).isoformat(timespec="seconds")
        assert account["expires_at"] == expected


# ---------------------------------------------------------------------------
# Auto make-up
# ---------------------------------------------------------------------------


def test_run_summary_records_failed_keys(tmp_path: Path):
    settings, database = make_single_task_database(tmp_path)
    database.upsert_topics(
        [
            {
                "topic_key": f"failure-{index}",
                "name": f"失败超话 {index}",
                "remote_status": "unknown",
                "checkin_scheme": f"/api/container/button?x={index}",
            }
            for index in range(2)
        ]
    )
    for index in range(2):
        database.update_topic_enabled(f"failure-{index}", True)

    class FailAllClient:
        def __init__(self, cookie: str):
            pass

        def verify_login(self):
            return LoginStatus(True, "1", "用户", "Cookie 有效")

        def list_topics(self, cancel_event):
            return [
                TopicSnapshot(f"failure-{i}", f"失败超话 {i}", "", "available", f"/scheme/{i}")
                for i in range(2)
            ]

        def checkin(self, scheme: str):
            return CheckinResult("failed", "签到失败", {})

    manager = TaskManager(database, settings, client_factory=FailAllClient)
    policy, notification = manager.runtime_state.snapshot()
    manager.runtime_state.save(replace(policy, checkin_delay_seconds=3.0), notification)
    run_id = manager.start("checkin")
    run = wait_for_run(database, run_id, timeout=8)

    assert run["status"] == "completed"
    assert run["summary"]["failed"] == 2
    assert run["summary"]["failed_keys"] == ["failure-0", "failure-1"]


def test_get_failed_keys_between(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    _insert_run(database.path, "checkin", "completed", old, {"failed_keys": ["stale"]})
    _insert_run(database.path, "checkin", "completed", utc_now(), {"failed_keys": ["a", "b"]})
    _insert_run(database.path, "checkin", "completed", utc_now(), {"failed_keys": ["b", "c"]})
    _insert_run(database.path, "sync", "completed", utc_now(), {"failed_keys": ["ignored"]})

    start, end = local_day_window("Asia/Shanghai")
    assert database.get_failed_keys_between(start, end) == ["a", "b", "c"]


def test_makeup_run_retries_only_failed_topics(tmp_path: Path):
    settings, database = make_single_task_database(tmp_path)
    database.upsert_topics(
        [
            {
                "topic_key": "t1",
                "name": "失败过",
                "remote_status": "unknown",
                "checkin_scheme": "/api/container/button?id=1",
            },
            {
                "topic_key": "t2",
                "name": "没失败",
                "remote_status": "unknown",
                "checkin_scheme": "/api/container/button?id=2",
            },
        ]
    )
    for key in ("t1", "t2"):
        database.update_topic_enabled(key, True)
    _insert_run(database.path, "checkin", "completed", utc_now(), {"failed_keys": ["t1"]})

    checkins: list[str] = []

    class MakeupClient:
        def __init__(self, cookie: str):
            pass

        def verify_login(self):
            return LoginStatus(True, "1", "用户", "Cookie 有效")

        def list_topics(self, cancel_event):
            return [
                TopicSnapshot("t1", "失败过", "", "available", "/api/container/button?id=1"),
                TopicSnapshot("t2", "没失败", "", "available", "/api/container/button?id=2"),
            ]

        def checkin(self, scheme: str):
            checkins.append(scheme)
            return CheckinResult("success", "补签成功", {})

    manager = TaskManager(database, settings, client_factory=MakeupClient)
    run_id = manager.start("makeup")
    run = wait_for_run(database, run_id)

    assert run["status"] == "completed"
    assert run["kind"] == "makeup"
    assert run["summary"]["success"] == 1
    assert checkins == ["/api/container/button?id=1"]


def test_makeup_run_without_failures_completes_empty(tmp_path: Path):
    settings, database = make_single_task_database(tmp_path)
    database.upsert_topics(
        [
            {
                "topic_key": "t1",
                "name": "正常",
                "remote_status": "unknown",
                "checkin_scheme": "/api/container/button?id=1",
            }
        ]
    )
    database.update_topic_enabled("t1", True)

    class NoopClient:
        def __init__(self, cookie: str):
            self.called = False

        def verify_login(self):
            return LoginStatus(True, "1", "用户", "Cookie 有效")

        def list_topics(self, cancel_event):
            self.called = True
            return [TopicSnapshot("t1", "正常", "", "available", "/api/container/button?id=1")]

        def checkin(self, scheme: str):
            raise AssertionError("makeup should not check in anything")

    client = NoopClient("SUB=abc")
    manager = TaskManager(database, settings, client_factory=lambda cookie: client)
    run_id = manager.start("makeup")
    run = wait_for_run(database, run_id)

    assert run["status"] == "completed"
    assert run["summary"]["selected"] == 0
    assert client.called is True


# ---------------------------------------------------------------------------
# Health score & adaptive delay
# ---------------------------------------------------------------------------


def test_compute_health_scores_recent_runs(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    now = utc_now()
    rows = [
        ("checkin", "completed", {"success": 8, "failed": 0}, None),
        ("checkin", "completed", {"success": 4, "already": 2, "failed": 2}, None),
        ("checkin", "failed", {"risk_status": 429}, "微博返回 HTTP 429"),
        ("sync", "failed", {}, "Cookie 已失效或未登录"),
        ("checkin", "failed", {"auth_failed": True}, "Cookie 已失效或未登录"),
    ]
    for index, (kind, status, summary, error) in enumerate(rows, start=1):
        _insert_run(database.path, kind, status, now, summary)
        if error:
            conn = sqlite3.connect(database.path)
            conn.execute("UPDATE runs SET error = ? WHERE id = ?", (error, index))
            conn.commit()
            conn.close()

    health = database.compute_health("Asia/Shanghai")

    # attempted = 16, rate = (8+4+2)/16 = 0.875
    assert health["success_rate"] == (8 + 4 + 2) / 16
    assert health["risk_count"] == 1
    assert health["auth_failures"] == 1
    expected = round(health["success_rate"] * 70) - 10 - 10
    assert health["score"] == max(0, min(100, expected))
    assert health["grade"] in {"优", "良", "差"}


def test_compute_health_empty_database(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    health = database.compute_health("Asia/Shanghai")
    assert health["score"] is None
    assert health["grade"] == "暂无数据"


def test_adaptive_delay_bump_and_decay_bounds(tmp_path: Path):
    state = make_runtime_state(tmp_path)
    assert state.current_delay_multiplier() == 1.0

    assert state.bump_delay() == 1.5
    assert state.bump_delay() == 2.25
    for _ in range(10):
        state.bump_delay()
    assert state.current_delay_multiplier() == 4.0

    assert state.decay_delay() == 3.2
    for _ in range(10):
        state.decay_delay()
    assert state.current_delay_multiplier() == 1.0
