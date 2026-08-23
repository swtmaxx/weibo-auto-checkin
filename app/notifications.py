from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .config import NotificationSettings, RuntimeState
from .qqbot import QQBotClient


class NotificationService:
    def __init__(
        self,
        runtime_state: RuntimeState,
        *,
        client_factory: Callable[[NotificationSettings], Any] | None = None,
    ):
        self.runtime_state = runtime_state
        self.client_factory = client_factory or (
            lambda config: QQBotClient(
                config.app_id,
                config.client_secret,
                config.user_openid,
            )
        )

    def _client(self, config: NotificationSettings) -> Any:
        return self.client_factory(config)

    def send_test(self) -> None:
        _, config = self.runtime_state.snapshot()
        if not config.configured:
            raise RuntimeError("QQ 通知配置不完整")
        client = self._client(config)
        try:
            client.send_text("微博超话签到：这是一条测试通知。")
        finally:
            if hasattr(client, "close"):
                client.close()

    def notify_run(
        self,
        *,
        event: str,
        run_id: int,
        kind: str,
        status: str,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
        cooldown: dict[str, Any] | None = None,
    ) -> None:
        _, config = self.runtime_state.snapshot()
        if not config.enabled or not config.configured:
            return
        if event == "completed" and not config.notify_completed:
            return
        if event == "failed" and not config.notify_failed:
            return
        if event == "risk" and not config.notify_risk:
            return
        message = self._format_message(
            event=event,
            run_id=run_id,
            kind=kind,
            status=status,
            summary=summary or {},
            error=error,
            cooldown=cooldown,
        )
        client = self._client(config)
        try:
            client.send_text(message)
        finally:
            if hasattr(client, "close"):
                client.close()

    @staticmethod
    def _format_message(
        *,
        event: str,
        run_id: int,
        kind: str,
        status: str,
        summary: dict[str, Any],
        error: str | None,
        cooldown: dict[str, Any] | None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        labels = {
            "completed": "签到完成",
            "failed": "任务失败",
            "risk": "触发风控保护",
        }
        title = labels.get(event, "任务通知")
        lines = [
            f"微博超话签到 · {title}",
            f"任务：{kind} #{run_id}",
            f"状态：{status}",
            f"时间：{now}",
        ]
        if summary:
            lines.append(
                "结果：处理 {selected}，成功 {success}，已签到 {already}，失败 {failed}".format(
                    selected=summary.get("selected", summary.get("discovered", 0)),
                    success=summary.get("success", 0),
                    already=summary.get("already", 0),
                    failed=summary.get("failed", 0),
                )
            )
        if error:
            lines.append(f"原因：{str(error)[:300]}")
        if cooldown and cooldown.get("until"):
            lines.append(f"冷却至：{cooldown['until']}")
        return "\n".join(lines)

