from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets

from .config import NotificationSettings, RuntimeState
from .db import Database
from .qqbot import QQBotClient, QQBotError

logger = logging.getLogger(__name__)


class QQEventListener:
    """Receive QQ C2C events and retain only discovered user OpenIDs."""

    gateway_api_url = "https://api.bot.qq.com/gateway"
    c2c_intent = 1 << 25
    reconnect_delay = 5.0
    max_reconnect_delay = 60.0

    def __init__(
        self,
        database: Database,
        runtime_state: RuntimeState,
        *,
        http_client_factory: Callable[[], httpx.Client] | None = None,
        websocket_connect: Callable[..., Awaitable[Any]] | None = None,
        qq_client_factory: Callable[[NotificationSettings], QQBotClient] | None = None,
    ):
        self.database = database
        self.runtime_state = runtime_state
        self.http_client_factory = http_client_factory
        self.websocket_connect = websocket_connect or websockets.connect
        self.qq_client_factory = qq_client_factory or self._make_qq_client
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._active_websocket: Any | None = None
        self._status_lock = threading.Lock()
        self._connected = False
        self._last_error: str | None = None
        self._last_connected_at: str | None = None
        self._session_id: str | None = None
        self._sequence: int | None = None
        self._credential_fingerprint: tuple[str, str] | None = None

    @staticmethod
    def _make_qq_client(settings: NotificationSettings) -> QQBotClient:
        return QQBotClient(settings.app_id, settings.client_secret, settings.user_openid)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="qq-event-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        loop = self._loop
        wake_event = self._wake_event
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._wake_and_close)
        elif wake_event and loop:
            loop.call_soon_threadsafe(wake_event.set)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def wake(self) -> None:
        """Wake the background loop after a settings change."""
        loop = self._loop
        wake_event = self._wake_event
        if loop and wake_event and loop.is_running():
            loop.call_soon_threadsafe(wake_event.set)

    def status(self) -> dict[str, Any]:
        _, settings = self.runtime_state.snapshot()
        with self._status_lock:
            return {
                "enabled": settings.listen_events,
                "configured": settings.bot_configured,
                "running": bool(self._thread and self._thread.is_alive()),
                "connected": self._connected,
                "event": "C2C_MESSAGE_CREATE",
                "last_error": self._last_error,
                "last_connected_at": self._last_connected_at,
            }

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            logger.exception("QQ event listener stopped unexpectedly")
        finally:
            with self._status_lock:
                self._connected = False
            self._loop = None
            self._wake_event = None

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        delay = self.reconnect_delay
        bot_client: QQBotClient | None = None
        try:
            while not self._stop_event.is_set():
                _, settings = self.runtime_state.snapshot()
                fingerprint = (settings.app_id, settings.client_secret)
                if not settings.listen_events or not settings.bot_configured:
                    self._set_connected(False)
                    await self._wait_for_change(5.0)
                    continue
                if bot_client is None or fingerprint != self._credential_fingerprint:
                    if bot_client:
                        bot_client.close()
                    bot_client = self.qq_client_factory(settings)
                    self._credential_fingerprint = fingerprint
                    self._session_id = None
                    self._sequence = None
                try:
                    await self._connect_once(settings, bot_client)
                    delay = self.reconnect_delay
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._set_error(str(exc)[:300])
                    logger.warning("QQ 事件监听暂时不可用: %s", str(exc)[:300])
                    await self._wait_for_change(delay)
                    delay = min(self.max_reconnect_delay, delay * 2)
        finally:
            self._set_connected(False)
            if bot_client:
                bot_client.close()

    def _wake_and_close(self) -> None:
        if self._wake_event:
            self._wake_event.set()
        websocket = self._active_websocket
        if websocket:
            asyncio.create_task(self._close_websocket(websocket))

    async def _close_websocket(self, websocket: Any) -> None:
        try:
            await websocket.close()
        except Exception:
            pass

    async def _wait_for_change(self, timeout: float) -> None:
        if self._stop_event.is_set():
            return
        if not self._wake_event:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake_event.clear()

    async def _connect_once(self, settings: NotificationSettings, bot_client: QQBotClient) -> None:
        access_token = await asyncio.to_thread(bot_client.get_access_token)
        gateway = await asyncio.to_thread(self._get_gateway, access_token)
        url = str(gateway.get("url") or "").strip()
        if not url:
            raise QQBotError("QQ gateway 响应缺少 url")
        try:
            connection = self.websocket_connect(
                url,
                additional_headers={"User-Agent": "weibo-checkin-web/0.1"},
                ping_interval=None,
                open_timeout=15,
            )
        except TypeError:
            connection = self.websocket_connect(
                url,
                extra_headers={"User-Agent": "weibo-checkin-web/0.1"},
                ping_interval=None,
                open_timeout=15,
            )
        async with connection as websocket:
            self._active_websocket = websocket
            self._set_connected(True)
            try:
                await self._consume(websocket, access_token)
            finally:
                self._active_websocket = None
                self._set_connected(False)

    def _get_gateway(self, access_token: str) -> dict[str, Any]:
        client = self.http_client_factory() if self.http_client_factory else httpx.Client(timeout=15)
        try:
            response = client.get(
                self.gateway_api_url,
                headers={"Authorization": f"QQBot {access_token}"},
            )
            if response.status_code >= 400:
                raise QQBotError(f"获取 QQ gateway 失败 HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise QQBotError("QQ gateway 响应格式错误")
            return payload
        except httpx.RequestError as exc:
            raise QQBotError(f"获取 QQ gateway 失败: {exc}") from exc
        finally:
            client.close()

    async def _consume(self, websocket: Any, access_token: str) -> None:
        heartbeat_task: asyncio.Task[Any] | None = None
        try:
            async for raw_message in websocket:
                try:
                    payload = json.loads(raw_message)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if isinstance(payload.get("s"), int):
                    self._sequence = payload["s"]
                opcode = payload.get("op")
                event_type = payload.get("t")
                data = payload.get("d")
                if opcode == 10:
                    interval = self._heartbeat_interval(data)
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat(websocket, interval)
                    )
                    await websocket.send(json.dumps(self._auth_payload(access_token)))
                elif opcode == 1:
                    await websocket.send(json.dumps({"op": 11, "d": self._sequence}))
                elif opcode == 7:
                    return
                elif opcode == 9:
                    self._session_id = None
                    self._sequence = None
                    raise QQBotError("QQ gateway 鉴权会话无效")
                elif opcode == 0 and event_type == "READY" and isinstance(data, dict):
                    session_id = str(data.get("session_id") or "").strip()
                    if session_id:
                        self._session_id = session_id
                elif opcode == 0 and event_type == "C2C_MESSAGE_CREATE" and isinstance(data, dict):
                    self._record_openid(data)
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    def _auth_payload(self, access_token: str) -> dict[str, Any]:
        if self._session_id and self._sequence is not None:
            return {
                "op": 6,
                "d": {
                    "token": f"QQBot {access_token}",
                    "session_id": self._session_id,
                    "seq": self._sequence,
                },
            }
        return {
            "op": 2,
            "d": {
                "token": f"QQBot {access_token}",
                "intents": self.c2c_intent,
                "shard": [0, 1],
                "properties": {"$os": "linux", "$browser": "weibo-checkin-web", "$device": "weibo-checkin-web"},
            },
        }

    @staticmethod
    def _heartbeat_interval(data: Any) -> float:
        try:
            interval = float(data.get("heartbeat_interval", 45000)) / 1000 if isinstance(data, dict) else 45.0
        except (TypeError, ValueError):
            interval = 45.0
        return max(1.0, interval)

    async def _heartbeat(self, websocket: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await websocket.send(json.dumps({"op": 1, "d": self._sequence}))

    def _record_openid(self, data: dict[str, Any]) -> bool:
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        user_openid = str(author.get("user_openid") or "").strip()
        if not user_openid:
            return False
        self.database.save_qq_openid(user_openid, "C2C_MESSAGE_CREATE")
        logger.info("发现 QQ 私聊用户标识，可在设置页选择")
        return True

    def _set_connected(self, connected: bool) -> None:
        with self._status_lock:
            self._connected = connected
            if connected:
                self._last_connected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _set_error(self, message: str) -> None:
        with self._status_lock:
            self._last_error = message
