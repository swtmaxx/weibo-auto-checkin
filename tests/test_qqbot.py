import json

import httpx

from app.qqbot import QQBotClient


def test_qqbot_gets_token_and_sends_private_message():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 7200}, request=request)
        return httpx.Response(200, json={"id": "message-1"}, request=request)

    client = QQBotClient(
        "app-1",
        "secret-1",
        "openid/1",
        transport=httpx.MockTransport(handler),
        base_url="https://qq.test",
    )
    try:
        client.send_text("测试通知")
        client.send_text("第二条")
    finally:
        client.close()

    assert len(calls) == 3
    assert calls[0].url.path == "/app/getAppAccessToken"
    assert calls[1].url.raw_path == b"/v2/users/openid%2F1/messages"
    assert calls[1].headers["Authorization"] == "QQBot token-1"
    assert json.loads(calls[1].content) == {"msg_type": 0, "content": "测试通知"}
    assert calls[2].url.path == calls[1].url.path


def test_qqbot_refreshes_token_once_after_401():
    token_calls = 0
    message_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, message_calls
        if request.url.path == "/app/getAppAccessToken":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{token_calls}", "expires_in": 7200},
                request=request,
            )
        message_calls += 1
        if message_calls == 1:
            return httpx.Response(401, json={"message": "expired"}, request=request)
        return httpx.Response(200, json={}, request=request)

    client = QQBotClient(
        "app-1",
        "secret-1",
        "openid-1",
        transport=httpx.MockTransport(handler),
        base_url="https://qq.test",
    )
    try:
        client.send_text("测试通知")
    finally:
        client.close()

    assert token_calls == 2
    assert message_calls == 2
