from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.security import decrypt_secret


class RecordingNotification:
    def __init__(self):
        self.calls = 0

    def send_test(self):
        self.calls += 1


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    app = create_app(settings, db=database, start_scheduler=False)
    return TestClient(app)


def login(client: TestClient) -> str:
    response = client.post("/api/auth/setup", json={"password": "a-long-admin-password"})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_settings_requires_auth_and_keeps_client_secret_hidden(tmp_path: Path):
    client = make_client(tmp_path)
    with client:
        assert client.get("/api/settings").status_code == 401
        csrf = login(client)
        initial = client.get("/api/settings")
        assert initial.status_code == 200
        assert "client_secret" not in initial.json()["notifications"]

        payload = initial.json()
        payload["runtime"]["checkin_delay_seconds"] = 12
        payload["notifications"].update(
            {
                "enabled": True,
                "app_id": "app-1",
                "user_openid": "openid-1",
                "client_secret": "first-secret",
                "clear_client_secret": False,
            }
        )
        saved = client.put("/api/settings", headers={"X-CSRF-Token": csrf}, json=payload)
        assert saved.status_code == 200
        assert saved.json()["runtime"]["checkin_delay_seconds"] == 12
        assert saved.json()["notifications"]["client_secret_configured"] is True
        assert "first-secret" not in saved.text

        payload = saved.json()
        payload["notifications"]["client_secret"] = None
        updated = client.put("/api/settings", headers={"X-CSRF-Token": csrf}, json=payload)
        assert updated.status_code == 200
        stored = Database(tmp_path / "test.sqlite3").get_json_config("notification_settings")
        assert stored is not None
        assert decrypt_secret(stored["client_secret_ciphertext"], "test-secret") == "first-secret"

        payload = updated.json()
        payload["notifications"]["enabled"] = False
        payload["notifications"]["client_secret"] = None
        payload["notifications"]["clear_client_secret"] = True
        cleared = client.put("/api/settings", headers={"X-CSRF-Token": csrf}, json=payload)
        assert cleared.status_code == 200
        assert cleared.json()["notifications"]["client_secret_configured"] is False

        conflict = updated.json()
        conflict["notifications"]["client_secret"] = "new-secret"
        conflict["notifications"]["clear_client_secret"] = True
        assert client.put("/api/settings", headers={"X-CSRF-Token": csrf}, json=conflict).status_code == 422


def test_settings_write_requires_csrf(tmp_path: Path):
    client = make_client(tmp_path)
    with client:
        csrf = login(client)
        payload = client.get("/api/settings").json()
        response = client.put("/api/settings", json=payload)
        assert response.status_code == 403
        assert csrf


def test_test_notification_requires_csrf_and_calls_service(tmp_path: Path):
    client = make_client(tmp_path)
    notification = RecordingNotification()
    client.app.state.notification_service = notification
    with client:
        csrf = login(client)
        assert client.post("/api/notifications/test").status_code == 403
        response = client.post("/api/notifications/test", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200
        assert notification.calls == 1


def test_event_listener_can_start_before_target_openid_is_known(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    app = create_app(settings, db=database, start_scheduler=False, start_qq_listener=False)
    client = TestClient(app)
    with client:
        csrf = login(client)
        payload = client.get("/api/settings").json()
        payload["notifications"].update(
            {
                "enabled": False,
                "app_id": "app-1",
                "user_openid": "",
                "client_secret": "listener-secret",
                "listen_events": True,
            }
        )
        response = client.put("/api/settings", headers={"X-CSRF-Token": csrf}, json=payload)
        assert response.status_code == 200
        assert response.json()["notifications"]["listen_events"] is True
        assert response.json()["notifications"]["user_openid"] == ""
        assert response.json()["notifications"]["client_secret_configured"] is True


def test_qq_discovery_endpoint_starts_listener_without_full_settings_payload(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    app = create_app(settings, db=database, start_scheduler=False, start_qq_listener=False)
    client = TestClient(app)
    with client:
        csrf = login(client)
        assert client.post("/api/qq/discovery", json={"app_id": "app-1"}).status_code == 403
        response = client.post(
            "/api/qq/discovery",
            headers={"X-CSRF-Token": csrf},
            json={"app_id": "app-1", "client_secret": "discovery-secret"},
        )
        assert response.status_code == 200
        notification = response.json()["notifications"]
        assert notification["listen_events"] is True
        assert notification["enabled"] is False
        assert notification["user_openid"] == ""
        stored = database.get_json_config("notification_settings")
        assert stored is not None
        assert decrypt_secret(stored["client_secret_ciphertext"], "test-secret") == "discovery-secret"
