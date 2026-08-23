from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.config import NotificationSettings, RuntimeState, Settings
from app.db import Database
from app.qqevents import QQEventListener


class FakeWebSocket:
    def __init__(self, messages: list[dict]):
        self.messages = iter(json.dumps(message) for message in messages)
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def send(self, message: str):
        self.sent.append(json.loads(message))


def make_listener(tmp_path: Path) -> tuple[QQEventListener, Database]:
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        secret_key="test-secret",
    )
    database = Database(settings.db_path)
    runtime_state = RuntimeState(database, settings)
    runtime_state.save(
        runtime_state.snapshot()[0],
        NotificationSettings(app_id="app-1", client_secret="secret-1", listen_events=True),
    )
    return QQEventListener(database, runtime_state), database


def test_c2c_event_saves_only_user_openid(tmp_path: Path):
    listener, database = make_listener(tmp_path)
    websocket = FakeWebSocket(
        [
            {"op": 10, "d": {"heartbeat_interval": 60000}},
            {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "session-1"}},
            {
                "op": 0,
                "s": 2,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "author": {"user_openid": "openid-1"},
                    "content": "这段私聊正文不能被保存",
                },
            },
        ]
    )

    asyncio.run(listener._consume(websocket, "access-token"))

    assert websocket.sent[0]["op"] == 2
    assert websocket.sent[0]["d"]["token"] == "QQBot access-token"
    assert websocket.sent[0]["d"]["intents"] == 1 << 25
    openids = database.list_qq_openids()
    assert [item["user_openid"] for item in openids] == ["openid-1"]
    assert "content" not in openids[0]
    assert database.get_json_config("qq_message") is None


def test_openid_is_deduplicated(tmp_path: Path):
    listener, database = make_listener(tmp_path)
    assert listener._record_openid({"author": {"user_openid": "openid-1"}, "content": "one"})
    assert listener._record_openid({"author": {"user_openid": "openid-1"}, "content": "two"})
    assert len(database.list_qq_openids()) == 1


def test_resume_payload_uses_last_session_and_sequence(tmp_path: Path):
    listener, _ = make_listener(tmp_path)
    listener._session_id = "session-1"
    listener._sequence = 42

    payload = listener._auth_payload("access-token")

    assert payload == {
        "op": 6,
        "d": {
            "token": "QQBot access-token",
            "session_id": "session-1",
            "seq": 42,
        },
    }
