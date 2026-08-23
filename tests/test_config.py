from pathlib import Path

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
