from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.weibo import LoginStatus


class FakeManager:
    def __init__(self, database: Database):
        self.database = database
        self.next_run_id = 41

    def verify_cookie(self, cookie: str) -> LoginStatus:
        assert cookie == "SUB=abc; SUBP=def"
        return LoginStatus(True, "100", "测试用户", "Cookie 有效")

    def start(self, kind: str) -> int:
        assert kind in {"sync", "checkin"}
        self.next_run_id += 1
        return self.next_run_id

    def current(self):
        return None

    def cancel(self):
        return False


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
        cookie_secure=False,
    )
    database = Database(settings.db_path)
    app = create_app(
        settings,
        db=database,
        manager=FakeManager(database),
        start_scheduler=False,
    )
    return TestClient(app)


def test_setup_login_cookie_and_csrf(tmp_path: Path):
    client = make_client(tmp_path)
    with client:
        assert client.get("/").status_code == 200
        setup = client.post("/api/auth/setup", json={"password": "a-long-admin-password"})
        assert setup.status_code == 200
        csrf = setup.json()["csrf_token"]

        status = client.get("/api/status").json()
        assert status["authenticated"] is True
        assert status["csrf_token"] == csrf

        missing_csrf = client.post(
            "/api/account/cookie",
            json={"cookie": "SUB=abc; SUBP=def"},
        )
        assert missing_csrf.status_code == 403

        saved = client.post(
            "/api/account/cookie",
            headers={"X-CSRF-Token": csrf},
            json={"cookie": "Cookie: SUB=abc; SUBP=def"},
        )
        assert saved.status_code == 200
        assert saved.json()["account"]["configured"] is True

        verified = client.post(
            "/api/account/verify",
            headers={"X-CSRF-Token": csrf},
        )
        assert verified.status_code == 200
        assert verified.json()["account"]["login_name"] == "测试用户"

        account = client.get("/api/account").json()
        assert account["logged_in"] is True


def test_protected_endpoint_requires_login(tmp_path: Path):
    client = make_client(tmp_path)
    with client:
        response = client.get("/api/topics")
        assert response.status_code == 401
