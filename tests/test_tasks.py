from __future__ import annotations

import time
from pathlib import Path

from app.config import NotificationSettings, RuntimePolicy, Settings
from app.db import Database
from app.security import decrypt_cookie, encrypt_cookie
from app.tasks import RunBusyError, TaskManager
from app.weibo import CheckinResult, LoginStatus, TopicSnapshot, WeiboRiskError


class FakeClient:
    def __init__(self, cookie: str):
        assert cookie == "SUB=abc"
        self.checkins: list[str] = []

    def verify_login(self):
        return LoginStatus(True, "1", "用户", "Cookie 有效")

    def list_topics(self, cancel_event):
        return [
            TopicSnapshot("topic-1", "可签到超话", "", "available", "/api/container/button?x=1"),
            TopicSnapshot("topic-2", "已签到超话", "", "signed", None),
        ]

    def checkin(self, scheme: str):
        self.checkins.append(scheme)
        return CheckinResult("success", "签到成功", {"data": {"ok": 1}})


def wait_for_run(database: Database, run_id: int, timeout: float = 3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = database.get_run(run_id)
        if run and run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def test_task_manager_persists_summary_and_respects_enabled_topics(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
        checkin_delay_seconds=0,
    )
    database = Database(settings.db_path)
    database.save_cookie(encrypt_cookie("SUB=abc", settings.secret_key))
    database.upsert_topics(
        [
            {
                "topic_key": "topic-1",
                "name": "可签到超话",
                "remote_status": "unknown",
                "checkin_scheme": None,
            },
            {
                "topic_key": "topic-2",
                "name": "已签到超话",
                "remote_status": "unknown",
                "checkin_scheme": None,
            },
        ]
    )
    assert database.update_topic_enabled("topic-1", True)
    manager = TaskManager(database, settings, client_factory=FakeClient)
    run_id = manager.start("checkin")
    run = wait_for_run(database, run_id, timeout=5)

    assert run["status"] == "completed"
    assert run["summary"]["success"] == 1
    assert run["summary"]["already"] == 0
    assert run["summary"]["skipped"] == 1
    assert any("签到成功" in log["message"] for log in run["logs"])
    assert database.get_account()["logged_in"] == 1


class RiskClient:
    def __init__(self, cookie: str):
        assert cookie == "SUB=abc"

    def verify_login(self):
        return LoginStatus(True, "1", "用户", "Cookie 有效")

    def list_topics(self, cancel_event):
        return [TopicSnapshot("risk-topic", "风险超话", "", "available", "/api/container/button?x=1")]

    def checkin(self, scheme: str):
        raise WeiboRiskError("HTTP 429", 429)


class RecordingNotification:
    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self.error = error

    def notify_run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


class FailingClient:
    def __init__(self, cookie: str):
        assert cookie == "SUB=abc"

    def verify_login(self):
        return LoginStatus(True, "1", "用户", "Cookie 有效")

    def list_topics(self, cancel_event):
        return [
            TopicSnapshot(f"failure-{index}", f"失败超话 {index}", "", "available", f"/scheme/{index}")
            for index in range(4)
        ]

    def checkin(self, scheme: str):
        return CheckinResult("failed", "签到接口返回失败", {})


def make_task_database(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    database.save_cookie(encrypt_cookie("SUB=abc", settings.secret_key))
    return settings, database


def test_task_manager_cools_down_after_weibo_risk_and_notifies(tmp_path: Path):
    settings, database = make_task_database(tmp_path)
    database.upsert_topics(
        [{"topic_key": "risk-topic", "name": "风险超话", "remote_status": "unknown", "checkin_scheme": None}]
    )
    assert database.update_topic_enabled("risk-topic", True)
    notification = RecordingNotification()
    manager = TaskManager(
        database,
        settings,
        client_factory=RiskClient,
        notification_service=notification,
    )
    run_id = manager.start("checkin")
    run = wait_for_run(database, run_id)

    assert run["status"] == "failed"
    assert run["summary"]["risk_status"] == 429
    assert notification.calls[0]["event"] == "risk"
    assert manager.runtime_state.cooldown_status()["active"] is True
    try:
        manager.start("checkin")
    except RunBusyError as exc:
        assert "冷却" in str(exc)
    else:
        raise AssertionError("cooldown should block check-in")


def test_worker_survives_cooldown_write_failure(tmp_path: Path):
    settings, database = make_task_database(tmp_path)
    database.upsert_topics(
        [{"topic_key": "risk-topic", "name": "风险超话", "remote_status": "unknown", "checkin_scheme": None}]
    )
    assert database.update_topic_enabled("risk-topic", True)
    manager = TaskManager(database, settings, client_factory=RiskClient)

    def broken_set_cooldown(reason: str):
        raise RuntimeError("cooldown write exploded")

    manager.runtime_state.set_cooldown = broken_set_cooldown
    run_id = manager.start("checkin")
    run = wait_for_run(database, run_id)

    assert run["status"] == "failed"
    assert run["summary"]["risk_status"] == 429
    assert "cooldown_until" not in run["summary"]
    assert any("写入冷却状态失败" in log["message"] for log in run["logs"])
    assert manager.runtime_state.cooldown_status()["active"] is False


def test_notification_failure_does_not_change_completed_run(tmp_path: Path):
    settings, database = make_task_database(tmp_path)
    database.upsert_topics(
        [{"topic_key": "topic-1", "name": "可签到超话", "remote_status": "unknown", "checkin_scheme": None}]
    )
    assert database.update_topic_enabled("topic-1", True)
    notification = RecordingNotification(RuntimeError("QQ unavailable"))
    manager = TaskManager(
        database,
        settings,
        client_factory=FakeClient,
        notification_service=notification,
    )
    run_id = manager.start("checkin")
    run = wait_for_run(database, run_id)

    assert run["status"] == "completed"
    assert notification.calls
    assert any("QQ 通知发送失败" in log["message"] for log in run["logs"])


class RenewingClient(FakeClient):
    def renewed_cookies(self):
        return {"SUB": "renewed-value", "NEW-TOKEN": "xyz"}


def test_task_manager_renews_cookie_and_keeps_verification_state(tmp_path: Path):
    settings, database = make_task_database(tmp_path)
    database.upsert_topics(
        [{"topic_key": "topic-1", "name": "可签到超话", "remote_status": "unknown", "checkin_scheme": None}]
    )
    assert database.update_topic_enabled("topic-1", True)
    manager = TaskManager(database, settings, client_factory=RenewingClient)
    run_id = manager.start("checkin")
    run = wait_for_run(database, run_id)

    assert run["status"] == "completed"
    assert any("Cookie 已续期" in log["message"] for log in run["logs"])
    assert run["summary"]["renewed_cookies"] == ["NEW-TOKEN", "SUB"]
    account = database.get_account()
    assert account["logged_in"] == 1
    merged = decrypt_cookie(account["cookie_ciphertext"], settings.secret_key)
    assert "SUB=renewed-value" in merged
    assert "NEW-TOKEN=xyz" in merged


def test_consecutive_failures_stop_the_run(tmp_path: Path):
    settings, database = make_task_database(tmp_path)
    database.upsert_topics(
        [
            {
                "topic_key": f"failure-{index}",
                "name": f"失败超话 {index}",
                "remote_status": "unknown",
                "checkin_scheme": None,
            }
            for index in range(4)
        ]
    )
    for index in range(4):
        assert database.update_topic_enabled(f"failure-{index}", True)
    manager = TaskManager(database, settings, client_factory=FailingClient)
    policy, notification = manager.runtime_state.snapshot()
    manager.runtime_state.save(
        RuntimePolicy(
            checkin_delay_seconds=3,
            max_topics_per_run=0,
            max_consecutive_failures=2,
            request_timeout_seconds=policy.request_timeout_seconds,
            read_retry_count=policy.read_retry_count,
            cooldown_on_rate_limit=policy.cooldown_on_rate_limit,
        ),
        notification,
    )
    run_id = manager.start("checkin")
    run = wait_for_run(database, run_id, timeout=5)

    assert run["status"] == "completed"
    assert run["summary"]["failed"] == 2
    assert run["summary"]["stopped_reason"] == "连续失败达到阈值"
