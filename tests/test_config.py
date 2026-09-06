from pathlib import Path

import pytest

from app.config import RuntimePolicy, RuntimeState, Settings
from app.db import Database
from app.security import decrypt_secret


def test_runtime_state_persists_policy_and_encrypted_notification_secret(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    state = RuntimeState(database, settings)
    policy, _ = state.snapshot()
    updated = RuntimePolicy(
        checkin_delay_seconds=14,
        max_topics_per_run=8,
        max_consecutive_failures=4,
        request_timeout_seconds=20,
        read_retry_count=2,
        cooldown_on_rate_limit=False,
    )
    from app.config import NotificationSettings

    state.save(
        updated,
        NotificationSettings(
            enabled=True,
            app_id="app-1",
            user_openid="openid-1",
            client_secret="secret-1",
        ),
    )
    persisted = database.get_json_config("notification_settings")
    assert persisted is not None
    assert persisted["client_secret_ciphertext"] != "secret-1"
    assert decrypt_secret(persisted["client_secret_ciphertext"], "test-secret") == "secret-1"

    reloaded = RuntimeState(database, settings)
    reloaded_policy, notification = reloaded.snapshot()
    assert reloaded_policy == updated
    assert notification.client_secret == "secret-1"
    assert policy != updated


def test_persisted_policy_takes_precedence_over_environment_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_CHECKIN_DELAY_SECONDS", "30")
    monkeypatch.setenv("APP_MAX_TOPICS_PER_RUN", "99")
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    database.set_json_config(
        "runtime_settings",
        {
            "checkin_delay_seconds": 7,
            "max_topics_per_run": 2,
            "max_consecutive_failures": 3,
            "request_timeout_seconds": 15,
            "read_retry_count": 1,
            "cooldown_on_rate_limit": True,
        },
    )

    policy, _ = RuntimeState(database, settings).snapshot()
    assert policy.checkin_delay_seconds == 7
    assert policy.max_topics_per_run == 2


def test_set_cooldown_survives_invalid_timezone(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
        timezone="Not/ARealZone",
    )
    state = RuntimeState(Database(settings.db_path), settings)

    status = state.set_cooldown("微博返回 HTTP 429")

    assert status["active"] is True
    assert status["until"]  # UTC fallback still produces a deadline
    state.clear_cooldown()
    assert state.cooldown_status()["active"] is False


def test_settings_default_host_is_loopback(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("APP_HOST", raising=False)
    settings = Settings.from_env(base_dir=tmp_path)
    assert settings.host == "127.0.0.1"


def test_runtime_policy_batch_fields_validation():
    assert RuntimePolicy().batch_size == 0
    assert RuntimePolicy().batch_interval_minutes == 60
    with pytest.raises(ValueError):
        RuntimePolicy(batch_size=10001).validate()
    with pytest.raises(ValueError):
        RuntimePolicy(batch_size=-1).validate()
    with pytest.raises(ValueError):
        RuntimePolicy(batch_interval_minutes=9).validate()
    with pytest.raises(ValueError):
        RuntimePolicy(batch_interval_minutes=721).validate()

    fallback = RuntimePolicy()
    updated = RuntimePolicy.from_mapping(
        {"batch_size": 25, "batch_interval_minutes": 90}, fallback=fallback
    )
    assert updated.batch_size == 25
    assert updated.batch_interval_minutes == 90
    # 旧备份没有 batch 字段时应回退到默认值
    legacy = RuntimePolicy.from_mapping({}, fallback=fallback)
    assert legacy.batch_size == 0
    assert legacy.batch_interval_minutes == 60
