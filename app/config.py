from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

from .security import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

RUNTIME_SETTINGS_KEY = "runtime_settings"
NOTIFICATION_SETTINGS_KEY = "notification_settings"
COOLDOWN_UNTIL_KEY = "cooldown_until"
COOLDOWN_REASON_KEY = "cooldown_reason"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    db_path: Path
    secret_key: str
    host: str = "0.0.0.0"
    port: int = 8000
    cookie_secure: bool = False
    timezone: str = "Asia/Shanghai"
    checkin_delay_seconds: float = 10.0
    scheduler_poll_seconds: float = 15.0
    session_max_age: int = 60 * 60 * 24 * 30
    history_retention_days: int = 90
    schedule_catch_up: bool = True

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "Settings":
        project_dir = base_dir or Path(__file__).resolve().parents[1]
        data_dir = Path(os.getenv("APP_DATA_DIR", str(project_dir / "data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = (project_dir / data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        secret_key = os.getenv("APP_SECRET_KEY", "").strip()
        secret_file = data_dir / ".secret_key"
        if not secret_key and secret_file.exists():
            secret_key = secret_file.read_text(encoding="utf-8").strip()
        if not secret_key:
            secret_key = secrets.token_urlsafe(48)
            secret_file.write_text(secret_key, encoding="utf-8")
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass
            logger.warning(
                "APP_SECRET_KEY is not set; generated %s. Set APP_SECRET_KEY "
                "explicitly for multi-instance or backup-safe deployments.",
                secret_file,
            )

        db_path = Path(os.getenv("APP_DB_PATH", str(data_dir / "weibo-checkin.sqlite3"))).expanduser()
        if not db_path.is_absolute():
            db_path = (project_dir / db_path).resolve()

        return cls(
            data_dir=data_dir,
            db_path=db_path,
            secret_key=secret_key,
            host=os.getenv("APP_HOST", "0.0.0.0"),
            port=int(os.getenv("APP_PORT", "8000")),
            cookie_secure=_env_bool("APP_COOKIE_SECURE", False),
            timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
            checkin_delay_seconds=max(0.0, float(os.getenv("APP_CHECKIN_DELAY_SECONDS", "10.0"))),
            scheduler_poll_seconds=max(1.0, float(os.getenv("APP_SCHEDULER_POLL_SECONDS", "15"))),
            history_retention_days=max(0, int(os.getenv("APP_HISTORY_RETENTION_DAYS", "90"))),
            schedule_catch_up=_env_bool("APP_SCHEDULE_CATCH_UP", True),
        )


@dataclass(frozen=True)
class RuntimePolicy:
    checkin_delay_seconds: float = 10.0
    delay_jitter_percent: int = 25
    max_topics_per_run: int = 0
    max_consecutive_failures: int = 3
    request_timeout_seconds: float = 15.0
    read_retry_count: int = 1
    cooldown_on_rate_limit: bool = True
    cooldown_hours: int = 0
    schedule_jitter_minutes: int = 0

    @classmethod
    def defaults(cls, settings: Settings) -> "RuntimePolicy":
        return cls(
            checkin_delay_seconds=max(3.0, min(60.0, settings.checkin_delay_seconds or 10.0)),
            delay_jitter_percent=max(0, min(100, int(os.getenv("APP_DELAY_JITTER_PERCENT", "25")))),
            max_topics_per_run=max(0, int(os.getenv("APP_MAX_TOPICS_PER_RUN", "0"))),
            max_consecutive_failures=max(0, int(os.getenv("APP_MAX_CONSECUTIVE_FAILURES", "3"))),
            request_timeout_seconds=max(
                5.0,
                min(60.0, float(os.getenv("APP_REQUEST_TIMEOUT_SECONDS", "15"))),
            ),
            read_retry_count=max(0, min(2, int(os.getenv("APP_READ_RETRY_COUNT", "1")))),
            cooldown_on_rate_limit=_env_bool("APP_COOLDOWN_ON_RATE_LIMIT", True),
            cooldown_hours=max(0, min(168, int(os.getenv("APP_COOLDOWN_HOURS", "0")))),
            schedule_jitter_minutes=max(
                0, min(120, int(os.getenv("APP_SCHEDULE_JITTER_MINUTES", "0")))
            ),
        )

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any],
        *,
        fallback: "RuntimePolicy",
    ) -> "RuntimePolicy":
        values = {
            "checkin_delay_seconds": mapping.get(
                "checkin_delay_seconds", fallback.checkin_delay_seconds
            ),
            "delay_jitter_percent": mapping.get(
                "delay_jitter_percent", fallback.delay_jitter_percent
            ),
            "max_topics_per_run": mapping.get(
                "max_topics_per_run", fallback.max_topics_per_run
            ),
            "max_consecutive_failures": mapping.get(
                "max_consecutive_failures", fallback.max_consecutive_failures
            ),
            "request_timeout_seconds": mapping.get(
                "request_timeout_seconds", fallback.request_timeout_seconds
            ),
            "read_retry_count": mapping.get("read_retry_count", fallback.read_retry_count),
            "cooldown_on_rate_limit": mapping.get(
                "cooldown_on_rate_limit", fallback.cooldown_on_rate_limit
            ),
            "cooldown_hours": mapping.get("cooldown_hours", fallback.cooldown_hours),
            "schedule_jitter_minutes": mapping.get(
                "schedule_jitter_minutes", fallback.schedule_jitter_minutes
            ),
        }
        try:
            policy = cls(
                checkin_delay_seconds=float(values["checkin_delay_seconds"]),
                delay_jitter_percent=int(values["delay_jitter_percent"]),
                max_topics_per_run=int(values["max_topics_per_run"]),
                max_consecutive_failures=int(values["max_consecutive_failures"]),
                request_timeout_seconds=float(values["request_timeout_seconds"]),
                read_retry_count=int(values["read_retry_count"]),
                cooldown_on_rate_limit=bool(values["cooldown_on_rate_limit"]),
                cooldown_hours=int(values["cooldown_hours"]),
                schedule_jitter_minutes=int(values["schedule_jitter_minutes"]),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("运行配置格式错误") from exc
        policy.validate()
        return policy

    def validate(self) -> None:
        if not 3.0 <= self.checkin_delay_seconds <= 60.0:
            raise ValueError("签到间隔必须在 3-60 秒之间")
        if self.delay_jitter_percent < 0 or self.delay_jitter_percent > 100:
            raise ValueError("间隔抖动幅度必须在 0-100% 之间")
        if self.max_topics_per_run < 0 or self.max_topics_per_run > 10000:
            raise ValueError("单次超话上限必须在 0-10000 之间")
        if self.max_consecutive_failures < 0 or self.max_consecutive_failures > 100:
            raise ValueError("连续失败阈值必须在 0-100 之间")
        if not 5.0 <= self.request_timeout_seconds <= 60.0:
            raise ValueError("请求超时必须在 5-60 秒之间")
        if self.read_retry_count not in {0, 1, 2}:
            raise ValueError("读取接口重试次数必须在 0-2 次之间")
        if self.cooldown_hours < 0 or self.cooldown_hours > 168:
            raise ValueError("冷却时长必须在 0-168 小时之间")
        if self.schedule_jitter_minutes < 0 or self.schedule_jitter_minutes > 120:
            raise ValueError("计划随机延迟必须在 0-120 分钟之间")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NotificationSettings:
    enabled: bool = False
    app_id: str = ""
    user_openid: str = ""
    client_secret: str = ""
    notify_completed: bool = True
    notify_failed: bool = True
    notify_risk: bool = True
    listen_events: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.user_openid and self.client_secret)

    @property
    def bot_configured(self) -> bool:
        return bool(self.app_id and self.client_secret)

    def validate(self) -> None:
        if len(self.app_id) > 128:
            raise ValueError("QQ AppID 过长")
        if len(self.user_openid) > 256:
            raise ValueError("QQ user_openid 过长")
        if len(self.client_secret) > 512:
            raise ValueError("QQ ClientSecret 过长")
        if self.enabled and not self.configured:
            raise ValueError("启用 QQ 功能前必须填写完整凭证")
        if self.listen_events and not self.bot_configured:
            raise ValueError("监听 QQ 私聊事件前必须填写 AppID 和 ClientSecret")

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "app_id": self.app_id,
            "user_openid": self.user_openid,
            "client_secret_configured": bool(self.client_secret),
            "notify_completed": self.notify_completed,
            "notify_failed": self.notify_failed,
            "notify_risk": self.notify_risk,
            "listen_events": self.listen_events,
        }


class RuntimeState:
    """Thread-safe runtime policy and encrypted notification configuration."""

    def __init__(self, database: Any, settings: Settings):
        self.database = database
        self.settings = settings
        self._lock = threading.RLock()
        self.reload()

    def reload(self) -> None:
        """Re-read policy and notification settings from the database."""
        fallback = RuntimePolicy.defaults(self.settings)
        stored_policy = self.database.get_json_config(RUNTIME_SETTINGS_KEY) or {}
        try:
            policy = RuntimePolicy.from_mapping(stored_policy, fallback=fallback)
        except ValueError:
            logger.warning("Invalid persisted runtime settings; using safe defaults")
            policy = fallback
        notification = self._load_notification()
        with self._lock:
            self._policy = policy
            self._notification = notification

    def _load_notification(self) -> NotificationSettings:
        stored = self.database.get_json_config(NOTIFICATION_SETTINGS_KEY) or {}
        ciphertext = str(stored.get("client_secret_ciphertext") or "")
        client_secret = ""
        if ciphertext:
            try:
                client_secret = decrypt_secret(ciphertext, self.settings.secret_key)
            except ValueError:
                logger.warning("QQ notification secret could not be decrypted")
        notification = NotificationSettings(
            enabled=bool(stored.get("enabled", False)),
            app_id=str(stored.get("app_id") or ""),
            user_openid=str(stored.get("user_openid") or ""),
            client_secret=client_secret,
            notify_completed=bool(stored.get("notify_completed", True)),
            notify_failed=bool(stored.get("notify_failed", True)),
            notify_risk=bool(stored.get("notify_risk", True)),
            listen_events=bool(stored.get("listen_events", False)),
        )
        try:
            notification.validate()
        except ValueError:
            logger.warning("Invalid persisted notification settings; notifications disabled")
            return replace(notification, enabled=False)
        return notification

    def snapshot(self) -> tuple[RuntimePolicy, NotificationSettings]:
        with self._lock:
            return self._policy, self._notification

    def save(
        self,
        policy: RuntimePolicy,
        notification: NotificationSettings,
    ) -> None:
        policy.validate()
        notification.validate()
        stored_notification = {
            "enabled": notification.enabled,
            "app_id": notification.app_id,
            "user_openid": notification.user_openid,
            "client_secret_ciphertext": (
                encrypt_secret(notification.client_secret, self.settings.secret_key)
                if notification.client_secret
                else ""
            ),
            "notify_completed": notification.notify_completed,
            "notify_failed": notification.notify_failed,
            "notify_risk": notification.notify_risk,
            "listen_events": notification.listen_events,
        }
        with self._lock:
            self.database.set_json_config(RUNTIME_SETTINGS_KEY, policy.to_dict())
            self.database.set_json_config(NOTIFICATION_SETTINGS_KEY, stored_notification)
            self._policy = policy
            self._notification = notification

    def reset(self) -> None:
        policy = RuntimePolicy.defaults(self.settings)
        notification = NotificationSettings()
        self.save(policy, notification)

    def import_policy(self, policy: RuntimePolicy) -> None:
        """Replace the stored policy with one validated by the caller."""
        policy.validate()
        self.database.set_json_config(RUNTIME_SETTINGS_KEY, policy.to_dict())
        with self._lock:
            self._policy = policy

    def import_notification_raw(self, raw: dict[str, Any]) -> None:
        """Store a raw notification settings dict (from a backup) and re-read it."""
        self.database.set_json_config(NOTIFICATION_SETTINGS_KEY, raw)
        with self._lock:
            self._notification = self._load_notification()

    def cooldown_status(self) -> dict[str, Any]:
        with self._lock:
            until = self.database.get_config(COOLDOWN_UNTIL_KEY)
            reason = self.database.get_config(COOLDOWN_REASON_KEY)
            if not until:
                return {"active": False, "until": None, "reason": None}
            try:
                deadline = datetime.fromisoformat(until)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
            except ValueError:
                self.clear_cooldown()
                return {"active": False, "until": None, "reason": None}
            if datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc):
                self.clear_cooldown()
                return {"active": False, "until": None, "reason": None}
            return {"active": True, "until": until, "reason": reason or "微博返回限流或风控响应"}

    def set_cooldown(self, reason: str) -> dict[str, Any]:
        try:
            zone = ZoneInfo(self.settings.timezone)
        except Exception:
            # The fallback must never raise, or risk handling in TaskManager._worker dies.
            zone = timezone.utc
        with self._lock:
            cooldown_hours = self._policy.cooldown_hours
        if cooldown_hours > 0:
            until = (
                datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
            ).isoformat(timespec="seconds")
        else:
            now = datetime.now(zone)
            next_day = now.date() + timedelta(days=1)
            local_midnight = datetime.combine(next_day, time.min, tzinfo=zone)
            until = local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
        self.database.set_config(COOLDOWN_UNTIL_KEY, until)
        self.database.set_config(COOLDOWN_REASON_KEY, reason[:500])
        return self.cooldown_status()

    def clear_cooldown(self) -> None:
        self.database.delete_config(COOLDOWN_UNTIL_KEY)
        self.database.delete_config(COOLDOWN_REASON_KEY)

    def is_cooling_down(self) -> bool:
        return bool(self.cooldown_status()["active"])
