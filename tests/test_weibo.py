from __future__ import annotations

import httpx
import pytest

from app.weibo import (
    CookieFormatError,
    WeiboClient,
    WeiboRequestError,
    normalize_cookie,
)


def test_normalize_cookie_accepts_request_header_block():
    value = normalize_cookie(
        "Accept: application/json\r\n"
        "Cookie: SUB=abc; SUBP=def; SUB=duplicate\r\n"
        "Referer: https://m.weibo.cn/"
    )
    assert value == "SUB=abc; SUBP=def"


def test_normalize_cookie_rejects_invalid_input():
    with pytest.raises(CookieFormatError):
        normalize_cookie("Accept: application/json")
    with pytest.raises(CookieFormatError):
        normalize_cookie("SUB")
    with pytest.raises(CookieFormatError):
        normalize_cookie("SUB=ok;\nX-Injected: yes")


def test_weibo_client_lists_pages_and_accepts_data_ok():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/config":
            return httpx.Response(
                200,
                json={"data": {"login": True, "uid": "7", "screen_name": "测试用户"}},
                request=request,
            )
        if request.url.path == "/api/container/getIndex":
            if request.url.params.get("since_id") == "next":
                return httpx.Response(
                    200,
                    json={
                        "ok": 1,
                        "data": {
                            "cards": [
                                {
                                    "card_group": [
                                        {
                                            "oid": "topic-2",
                                            "title_sub": "第二个超话",
                                            "buttons": [{"name": "已签到"}],
                                        }
                                    ]
                                }
                            ],
                            "cardlistInfo": {},
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "ok": 1,
                    "data": {
                        "cards": [
                            {
                                "card_group": [
                                    {
                                        "oid": "topic-1",
                                        "title_sub": "第一个超话",
                                        "desc1": "等级 10",
                                        "buttons": [
                                            {
                                                "name": "签到",
                                                "scheme": "/api/container/button?path=%2Fcheckin",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                        "cardlistInfo": {"since_id": "next"},
                    },
                },
                request=request,
            )
        if request.url.path == "/api/container/button":
            return httpx.Response(
                200,
                json={"data": {"ok": 1, "msg": "签到成功"}},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = WeiboClient(
        "Cookie: SUB=abc; SUBP=def",
        transport=httpx.MockTransport(handler),
        base_url="https://m.weibo.cn",
        retry_delay=0,
    )
    try:
        login = client.verify_login()
        topics = client.list_topics()
        result = client.checkin(topics[0].checkin_scheme or "")
    finally:
        client.close()

    assert login.logged_in is True
    assert login.name == "测试用户"
    assert [topic.name for topic in topics] == ["第一个超话", "第二个超话"]
    assert topics[0].remote_status == "available"
    assert topics[1].remote_status == "signed"
    assert result.status == "success"
    assert calls[0].headers["Cookie"] == "SUB=abc; SUBP=def"


def test_weibo_client_retries_transient_status():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"data": {"login": False}}, request=request)

    client = WeiboClient(
        "SUB=abc",
        transport=httpx.MockTransport(handler),
        base_url="https://m.weibo.cn",
        retry_delay=0,
    )
    try:
        status = client.verify_login()
    finally:
        client.close()
    assert attempts == 2
    assert status.logged_in is False


def test_weibo_client_rejects_non_container_scheme():
    client = WeiboClient(
        "SUB=abc",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
        retry_delay=0,
    )
    try:
        with pytest.raises(WeiboRequestError):
            client.checkin("https://example.com/checkin")
    finally:
        client.close()


def test_client_sends_frozen_cookie_header():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": {"login": True}},
            headers={"Set-Cookie": "SUB=renewed-value; Domain=.weibo.cn; Path=/"},
            request=request,
        )

    client = WeiboClient(
        "SUB=old; SUBP=keep; XSRF-TOKEN=tok123",
        transport=httpx.MockTransport(handler),
        retry_delay=0,
    )
    try:
        client.verify_login()
        client.verify_login()
    finally:
        client.close()

    assert len(captured) == 2
    for request in captured:
        # 冻结头:两次请求的 Cookie 逐字节一致,不跟随 Set-Cookie
        assert request.headers["Cookie"] == "SUB=old; SUBP=keep; XSRF-TOKEN=tok123"
        assert request.headers["X-XSRF-TOKEN"] == "tok123"


def test_checkin_retries_once_with_fresh_st_on_verify_error():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/container/button":
            if "st" not in request.url.params:
                return httpx.Response(
                    200,
                    json={"ok": 0, "errno": "100015", "msg": "验签失败"},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"data": {"ok": 1, "msg": "签到成功"}},
                request=request,
            )
        if request.url.path == "/api/config":
            return httpx.Response(
                200,
                json={"data": {"login": True, "st": "st-token-1"}},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = WeiboClient("SUB=abc", transport=httpx.MockTransport(handler), retry_delay=0)
    try:
        result = client.checkin("/api/container/button?x=1")
    finally:
        client.close()

    assert result.status == "success"
    assert len(calls) == 3
    assert calls[2].url.params["st"] == "st-token-1"

