from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx


class WeiboError(RuntimeError):
    """Base error for the Weibo adapter."""


class CookieFormatError(WeiboError):
    pass


class WeiboAuthError(WeiboError):
    pass


class WeiboRequestError(WeiboError):
    pass


class WeiboRiskError(WeiboRequestError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+$")
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def normalize_cookie(raw_cookie: str) -> str:
    """Accept a copied Cookie request header and return a canonical header value."""
    if not isinstance(raw_cookie, str):
        raise CookieFormatError("Cookie 必须是文本")

    raw = raw_cookie.strip()
    if not raw:
        raise CookieFormatError("Cookie 不能为空")
    cookie_line = None
    for line in raw.splitlines():
        if re.match(r"^\s*cookie\s*:", line, flags=re.IGNORECASE):
            cookie_line = re.split(r":", line, maxsplit=1)[1].strip()
            break
    if cookie_line is not None:
        raw = cookie_line
    elif "\n" in raw or "\r" in raw:
        raise CookieFormatError("未找到 Cookie 请求头")

    if any(ord(char) < 32 and char not in "\t" for char in raw):
        raise CookieFormatError("Cookie 包含非法控制字符")

    pairs: list[str] = []
    seen: set[str] = set()
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        name, separator, value = part.partition("=")
        name = name.strip()
        if not separator or not _COOKIE_NAME.fullmatch(name):
            raise CookieFormatError(f"Cookie 字段格式错误: {name or '空字段'}")
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise CookieFormatError(f"Cookie 字段包含非法字符: {name}")
        if name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")

    if not pairs:
        raise CookieFormatError("没有解析到 Cookie 字段")
    return "; ".join(pairs)


@dataclass(frozen=True)
class LoginStatus:
    logged_in: bool
    uid: str | None = None
    name: str | None = None
    message: str = ""


@dataclass(frozen=True)
class TopicSnapshot:
    topic_key: str
    name: str
    description: str
    remote_status: str
    checkin_scheme: str | None


@dataclass(frozen=True)
class CheckinResult:
    status: str
    message: str
    raw: dict[str, Any]


class WeiboClient:
    base_url = "https://m.weibo.cn"

    def __init__(
        self,
        cookie: str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
        retry_delay: float = 0.3,
        read_retry_count: int = 1,
    ):
        self.cookie = normalize_cookie(cookie)
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.read_retry_count = max(0, min(2, int(read_retry_count)))
        self._client = httpx.Client(
            base_url=base_url or self.base_url,
            transport=transport,
            follow_redirects=True,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/124.0 Safari/537.36"
                ),
            },
        )
        xsrf = self._cookie_value("XSRF-TOKEN")
        if xsrf:
            self._client.headers["X-XSRF-TOKEN"] = xsrf

    def close(self) -> None:
        self._client.close()

    def _cookie_value(self, name: str) -> str | None:
        for item in self.cookie.split("; "):
            key, _, value = item.partition("=")
            if key == name:
                return value
        return None

    def _request(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        referer: str = "https://m.weibo.cn/",
        retry: bool = True,
        risk_on_forbidden: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "Cookie": self.cookie,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }
        last_error: Exception | None = None
        max_retries = self.read_retry_count if retry else 0
        for attempt in range(max_retries + 1):
            try:
                response = self._client.get(
                    path,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= max_retries:
                    raise WeiboRequestError(f"微博请求失败: {exc}") from exc
                time.sleep(self.retry_delay * (attempt + 1))
                continue

            if response.status_code == 429:
                raise WeiboRiskError("微博返回 HTTP 429，触发限流保护", 429)
            if response.status_code == 403 and risk_on_forbidden:
                raise WeiboRiskError("微博返回 HTTP 403，触发访问保护", 403)
            if response.status_code in _RETRY_STATUSES and attempt < max_retries:
                time.sleep(self.retry_delay * (attempt + 1))
                continue
            if response.status_code in {401, 403}:
                raise WeiboAuthError(f"微博返回 HTTP {response.status_code}")
            if response.status_code >= 400:
                raise WeiboRequestError(f"微博返回 HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise WeiboRequestError("微博返回了无法解析的 JSON") from exc
            if not isinstance(payload, dict):
                raise WeiboRequestError("微博返回格式不是对象")
            return payload

        raise WeiboRequestError(f"微博请求失败: {last_error or '未知错误'}")

    @staticmethod
    def _is_true(value: Any) -> bool:
        return value is True or value == 1 or value == "1"

    def verify_login(self) -> LoginStatus:
        payload = self._request("/api/config")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        logged_in = self._is_true(data.get("login"))
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        uid = data.get("uid") or user.get("id") or user.get("idstr")
        name = data.get("screen_name") or user.get("screen_name") or user.get("name")
        message = "Cookie 有效" if logged_in else "Cookie 已失效或未登录"
        return LoginStatus(logged_in, str(uid) if uid else None, str(name) if name else None, message)

    def list_topics(self, cancel_event: Any | None = None) -> list[TopicSnapshot]:
        topics: list[TopicSnapshot] = []
        seen: set[str] = set()
        since_id: str | None = None

        for _page in range(100):
            if cancel_event is not None and cancel_event.is_set():
                break
            params = {"containerid": "100803_-_followsuper"}
            if since_id:
                params["since_id"] = since_id
            payload = self._request(
                "/api/container/getIndex",
                params=params,
                referer="https://m.weibo.cn/p/index?containerid=100803_-_followsuper",
            )
            if not self._is_true(payload.get("ok")):
                message = str(payload.get("msg") or payload.get("message") or "获取超话列表失败")
                if "登录" in message or "cookie" in message.lower():
                    raise WeiboAuthError(message)
                raise WeiboRequestError(message)

            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            cards = data.get("cards") if isinstance(data.get("cards"), list) else []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                group = card.get("card_group") if isinstance(card.get("card_group"), list) else []
                for item in group:
                    snapshot = self._parse_topic(item)
                    if snapshot and snapshot.topic_key not in seen:
                        seen.add(snapshot.topic_key)
                        topics.append(snapshot)

            info = data.get("cardlistInfo") if isinstance(data.get("cardlistInfo"), dict) else {}
            next_since = info.get("since_id")
            next_since = str(next_since) if next_since else None
            if not next_since or next_since == since_id:
                break
            since_id = next_since
            if self.retry_delay:
                time.sleep(min(self.retry_delay, 0.5))
        return topics

    @staticmethod
    def _parse_topic(item: Any) -> TopicSnapshot | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("title_sub") or item.get("title") or "").strip()
        if not name:
            return None

        oid = item.get("oid") or item.get("topic_id") or item.get("id")
        if oid:
            topic_key = str(oid).strip()
        else:
            topic_key = "name:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]

        buttons = item.get("buttons") if isinstance(item.get("buttons"), list) else []
        status = "unknown"
        scheme: str | None = None
        for button in buttons:
            if not isinstance(button, dict):
                continue
            button_name = str(button.get("name") or "").strip()
            if button_name == "签到":
                candidate = str(button.get("scheme") or "").strip()
                if candidate.startswith("/api/container/button"):
                    status = "available"
                    scheme = candidate
                break
            if button_name in {"已签", "已签到", "明日再来"} or "已签" in button_name:
                status = "signed"
                break

        description = str(item.get("desc1") or item.get("desc2") or "").strip()
        return TopicSnapshot(topic_key, name, description, status, scheme)

    def checkin(self, scheme: str) -> CheckinResult:
        if not scheme.startswith("/api/container/button"):
            raise WeiboRequestError("拒绝执行非微博容器签到 scheme")
        payload = self._request(
            scheme,
            referer="https://m.weibo.cn/p/index?containerid=100803_-_followsuper",
            retry=False,
            risk_on_forbidden=True,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message = str(
            data.get("msg")
            or data.get("tipMessage")
            or payload.get("msg")
            or payload.get("message")
            or ""
        )
        code = str(payload.get("code") or "")
        if self._is_true(payload.get("ok")) or self._is_true(data.get("ok")) or code in {"100000", "382010"}:
            return CheckinResult("success", message or "签到成功", payload)
        if code == "382004" or "已签" in message or "明日再来" in message:
            return CheckinResult("already", message or "今日已签到", payload)
        return CheckinResult("failed", message or "签到失败", payload)
