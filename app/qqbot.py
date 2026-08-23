from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import httpx


class QQBotError(RuntimeError):
    """A QQ Bot API error that is safe to show in task logs."""


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: float


class QQBotClient:
    api_base_url = "https://api.bot.qq.com"

    def __init__(
        self,
        app_id: str,
        client_secret: str,
        user_openid: str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str | None = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ):
        self.app_id = app_id.strip()
        self.client_secret = client_secret
        self.user_openid = user_openid.strip()
        self.timeout = timeout
        self.clock = clock
        self._token: AccessToken | None = None
        self._token_lock = threading.Lock()
        self._client = httpx.Client(
            base_url=base_url or self.api_base_url,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "weibo-checkin-web/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def _error(self, response: httpx.Response, fallback: str) -> QQBotError:
        code = ""
        message = ""
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("retcode") or "")
            message = str(payload.get("message") or payload.get("msg") or "")
        detail = f"{fallback} HTTP {response.status_code}"
        if code:
            detail += f" code={code}"
        if message:
            detail += f": {message[:200]}"
        return QQBotError(detail)

    def _get_access_token(self, *, force_refresh: bool = False) -> str:
        now = self.clock()
        with self._token_lock:
            if (
                not force_refresh
                and self._token is not None
                and self._token.expires_at - 60 > now
            ):
                return self._token.value
            try:
                response = self._client.post(
                    "/app/getAppAccessToken",
                    json={"appId": self.app_id, "clientSecret": self.client_secret},
                    timeout=self.timeout,
                )
            except httpx.RequestError as exc:
                raise QQBotError(f"获取 QQ access_token 失败: {exc}") from exc
            if response.status_code >= 400:
                raise self._error(response, "获取 QQ access_token 失败")
            try:
                payload = response.json()
            except ValueError as exc:
                raise QQBotError("QQ access_token 响应不是 JSON") from exc
            token = str(payload.get("access_token") or "")
            if not token:
                raise QQBotError("QQ access_token 响应缺少 access_token")
            try:
                expires_in = max(120.0, float(payload.get("expires_in", 7200)))
            except (TypeError, ValueError):
                expires_in = 7200.0
            self._token = AccessToken(token, now + expires_in)
            return token

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached QQ access token for API and Gateway calls."""
        if not self.app_id or not self.client_secret:
            raise QQBotError("QQ Bot 凭证不完整")
        return self._get_access_token(force_refresh=force_refresh)

    def send_text(self, content: str) -> None:
        content = content.strip()
        if not content:
            raise QQBotError("QQ 通知内容不能为空")
        if len(content) > 4000:
            content = content[:3990] + "\n（内容已截断）"
        if not self.app_id or not self.client_secret or not self.user_openid:
            raise QQBotError("QQ 通知凭证不完整")

        encoded_openid = quote(self.user_openid, safe="")
        refreshed = False
        for attempt in range(2):
            token = self._get_access_token(force_refresh=refreshed)
            try:
                response = self._client.post(
                    f"/v2/users/{encoded_openid}/messages",
                    headers={
                        "Authorization": f"QQBot {token}",
                        "Content-Type": "application/json",
                    },
                    json={"msg_type": 0, "content": content},
                    timeout=self.timeout,
                )
            except httpx.RequestError as exc:
                if attempt == 0:
                    continue
                raise QQBotError(f"发送 QQ 通知失败: {exc}") from exc
            if 200 <= response.status_code < 300:
                return
            if response.status_code == 401 and attempt == 0:
                with self._token_lock:
                    self._token = None
                refreshed = True
                continue
            if response.status_code >= 500 and attempt == 0:
                continue
            raise self._error(response, "发送 QQ 通知失败")
        raise QQBotError("发送 QQ 通知失败")
