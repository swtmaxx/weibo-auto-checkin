from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable

from .config import RuntimePolicy, RuntimeState, Settings
from .db import Database
from .notifications import NotificationService
from .security import decrypt_cookie
from .weibo import WeiboAuthError, WeiboClient, WeiboRiskError

logger = logging.getLogger(__name__)

TASK_KINDS = {"sync", "checkin", "scheduled", "single", "makeup"}
COOLDOWN_KINDS = {"checkin", "scheduled", "single", "makeup"}


def jittered_delay(base_seconds: float, jitter_percent: int) -> float:
    """Base wait scaled by a symmetric random factor (0 percent = exact wait)."""
    if jitter_percent <= 0:
        return base_seconds
    factor = max(0, min(100, jitter_percent)) / 100.0
    return base_seconds * random.uniform(1.0 - factor, 1.0 + factor)


def local_day_window(timezone_name: str) -> tuple[str, str]:
    """UTC ISO window covering today in the given timezone."""
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = ZoneInfo("UTC")
    now = datetime.now(zone)
    start = datetime.combine(now.date(), time.min, tzinfo=zone).astimezone(timezone.utc)
    return (
        start.isoformat(timespec="seconds"),
        (start + timedelta(days=1)).isoformat(timespec="seconds"),
    )


class RunBusyError(RuntimeError):
    pass


class RiskCooldownError(RuntimeError):
    def __init__(self, message: str, cooldown: dict[str, Any]):
        super().__init__(message)
        self.cooldown = cooldown


class RunLogger:
    def __init__(self, db: Database, run_id: int):
        self.db = db
        self.run_id = run_id

    def info(self, message: str) -> None:
        self.db.add_log(self.run_id, "INFO", message)

    def success(self, message: str) -> None:
        self.db.add_log(self.run_id, "SUCCESS", message)

    def warning(self, message: str) -> None:
        self.db.add_log(self.run_id, "WARNING", message)

    def error(self, message: str) -> None:
        self.db.add_log(self.run_id, "ERROR", message)


class TaskManager:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        client_factory: Callable[[str], Any] | None = None,
        runtime_state: RuntimeState | None = None,
        notification_service: NotificationService | None = None,
    ):
        self.db = db
        self.settings = settings
        self.client_factory = client_factory
        self.runtime_state = runtime_state or RuntimeState(db, settings)
        self.notification_service = notification_service or NotificationService(self.runtime_state)
        self._operation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_run_id: int | None = None
        self._cancel_events: dict[int, threading.Event] = {}
        self.db.recover_running_runs()

    @staticmethod
    def _close_client(client: Any | None) -> None:
        if client is None or not hasattr(client, "close"):
            return
        try:
            client.close()
        except Exception:
            pass

    def start(self, kind: str, *, topic_key: str | None = None) -> int:
        if kind not in TASK_KINDS:
            raise ValueError(f"unsupported task kind: {kind}")
        if kind in COOLDOWN_KINDS and self.runtime_state.is_cooling_down():
            status = self.runtime_state.cooldown_status()
            raise RunBusyError(f"微博风控冷却中，截止 {status['until']}")
        if not self._operation_lock.acquire(blocking=False):
            raise RunBusyError("已有任务正在运行")

        try:
            run_id = self.db.create_run(kind)
        except Exception:
            self._operation_lock.release()
            raise
        cancel_event = threading.Event()
        with self._state_lock:
            self._active_run_id = run_id
            self._cancel_events[run_id] = cancel_event
        try:
            thread = threading.Thread(
                target=self._worker,
                args=(run_id, kind, cancel_event, topic_key),
                name=f"weibo-run-{run_id}",
                daemon=True,
            )
            thread.start()
        except Exception:
            with self._state_lock:
                self._active_run_id = None
                self._cancel_events.pop(run_id, None)
            self._operation_lock.release()
            self.db.finish_run(run_id, "failed", error="无法启动任务线程")
            raise
        return run_id

    def cancel(self, run_id: int | None = None) -> bool:
        with self._state_lock:
            active_id = self._active_run_id
            target = run_id or active_id
            event = self._cancel_events.get(target) if target is not None else None
            if event is None:
                return False
            event.set()
            return True

    def current(self) -> dict[str, Any] | None:
        return self.db.current_run()

    def verify_cookie(self, cookie: str) -> Any:
        if not self._operation_lock.acquire(blocking=False):
            raise RunBusyError("已有任务正在运行")
        client = None
        try:
            policy, _ = self.runtime_state.snapshot()
            client = self._make_client(cookie, policy)
            return client.verify_login()
        finally:
            self._close_client(client)
            self._operation_lock.release()

    def _worker(
        self,
        run_id: int,
        kind: str,
        cancel_event: threading.Event,
        topic_key: str | None = None,
    ) -> None:
        logger = RunLogger(self.db, run_id)
        client = None
        summary: dict[str, Any] = {}
        policy, _ = self.runtime_state.snapshot()
        try:
            self.db.start_run(run_id)
            account = self.db.get_account()
            if not account:
                raise RuntimeError("请先导入 Cookie")
            cookie = decrypt_cookie(account["cookie_ciphertext"], self.settings.secret_key)
            client = self._make_client(cookie, policy)
            logger.info("开始验证微博登录状态")
            login = client.verify_login()
            self.db.update_verification(login.logged_in, login.message, login.uid, login.name)
            if not login.logged_in:
                summary["auth_failed"] = True
                raise WeiboAuthError(login.message)
            logger.success(login.message)

            if kind == "single":
                summary = self._run_single_checkin(client, topic_key, logger)
                status = "cancelled" if cancel_event.is_set() else "completed"
                self.db.finish_run(run_id, status, summary)
                if status == "completed":
                    self._decay_delay()
                return

            logger.info("正在读取关注的超话列表")
            snapshots = client.list_topics(cancel_event)
            self.db.upsert_topics([self._topic_dict(item) for item in snapshots])
            logger.info(f"读取到 {len(snapshots)} 个超话")

            if kind == "sync":
                summary = {"discovered": len(snapshots)}
                status = "cancelled" if cancel_event.is_set() else "completed"
                self.db.finish_run(run_id, status, summary)
                if status == "completed":
                    self._decay_delay()
                return

            if kind == "makeup":
                start_iso, end_iso = self._local_day_window()
                failed_keys = set(self.db.get_failed_keys_between(start_iso, end_iso))
                snapshots = [item for item in snapshots if item.topic_key in failed_keys]
                if not snapshots:
                    logger.info("今天没有需要补签的超话")
                    self.db.finish_run(run_id, "completed", {"selected": 0})
                    self._decay_delay()
                    return

            summary = self._run_checkins(client, snapshots, cancel_event, logger, policy)
            status = "cancelled" if cancel_event.is_set() else "completed"
            self._notify(
                logger,
                event="completed",
                run_id=run_id,
                kind=kind,
                status=status,
                summary=summary,
            )
            self.db.finish_run(run_id, status, summary)
            if status == "completed":
                self._decay_delay()
        except WeiboRiskError as exc:
            cooldown = (
                self._safe_set_cooldown(str(exc), logger)
                if policy.cooldown_on_rate_limit
                else None
            )
            self.runtime_state.bump_delay()
            summary["risk_status"] = exc.status_code
            if cooldown:
                summary["cooldown_until"] = cooldown["until"]
                logger.error(f"{exc}；已冷却至 {cooldown['until']}")
            else:
                logger.error(str(exc))
            self.db.finish_run(run_id, "failed", summary, str(exc))
            if kind != "single":
                self._notify(
                    logger,
                    event="risk" if cooldown else "failed",
                    run_id=run_id,
                    kind=kind,
                    status="failed",
                    summary=summary,
                    error=str(exc),
                    cooldown=cooldown,
                )
        except RiskCooldownError as exc:
            self.db.finish_run(run_id, "failed", summary, str(exc))
            self._notify(
                logger,
                event="risk",
                run_id=run_id,
                kind=kind,
                status="failed",
                summary=summary,
                error=str(exc),
                cooldown=exc.cooldown,
            )
        except Exception as exc:
            logger.error(str(exc))
            self.db.finish_run(
                run_id,
                "cancelled" if cancel_event.is_set() else "failed",
                summary,
                str(exc)[:500],
            )
            if kind != "single":
                self._notify(
                    logger,
                    event="failed",
                    run_id=run_id,
                    kind=kind,
                    status="failed",
                    summary=summary,
                    error=str(exc),
                )
        finally:
            self._close_client(client)
            with self._state_lock:
                self._cancel_events.pop(run_id, None)
                if self._active_run_id == run_id:
                    self._active_run_id = None
            self._operation_lock.release()

    def _local_day_window(self) -> tuple[str, str]:
        return local_day_window(self.settings.timezone)

    def _make_client(self, cookie: str, policy: RuntimePolicy) -> Any:
        if self.client_factory is not None:
            return self.client_factory(cookie)
        return WeiboClient(
            cookie,
            timeout=policy.request_timeout_seconds,
            read_retry_count=policy.read_retry_count,
        )

    def _notify(self, logger: RunLogger, **kwargs: Any) -> None:
        try:
            self.notification_service.notify_run(**kwargs)
        except Exception as exc:
            logger.warning(f"QQ 通知发送失败: {str(exc)[:300]}")

    def _safe_set_cooldown(self, reason: str, logger: RunLogger) -> dict[str, Any] | None:
        """Cooldown bookkeeping must never raise, or the worker dies with the run stuck."""
        try:
            return self.runtime_state.set_cooldown(reason)
        except Exception as exc:
            logger.error(f"写入冷却状态失败: {exc}")
            return None

    @staticmethod
    def _topic_dict(snapshot: Any) -> dict[str, Any]:
        return {
            "topic_key": snapshot.topic_key,
            "name": snapshot.name,
            "description": snapshot.description,
            "remote_status": snapshot.remote_status,
            "checkin_scheme": snapshot.checkin_scheme,
        }

    def _run_checkins(
        self,
        client: Any,
        snapshots: list[Any],
        cancel_event: threading.Event,
        logger: RunLogger,
        policy: RuntimePolicy,
    ) -> dict[str, Any]:
        remote = {snapshot.topic_key: snapshot for snapshot in snapshots}
        selected = [
            topic
            for topic in self.db.list_topics()
            if topic["enabled"] and topic["topic_key"] in remote
        ]
        if policy.max_topics_per_run:
            selected = selected[: policy.max_topics_per_run]
        summary = {
            "discovered": len(snapshots),
            "selected": len(selected),
            "success": 0,
            "already": 0,
            "failed": 0,
            "skipped": max(0, len(snapshots) - len(selected)),
            "limited": bool(policy.max_topics_per_run and len(selected) >= policy.max_topics_per_run),
        }
        consecutive_failures = 0
        failed_keys: list[str] = []
        try:
            zone = ZoneInfo(self.settings.timezone)
        except Exception:
            zone = ZoneInfo("UTC")
        heatmap_day = datetime.now(zone).date().isoformat()
        for index, topic in enumerate(selected, start=1):
            if cancel_event.is_set():
                break
            snapshot = remote[topic["topic_key"]]
            logger.info(f"处理 {index}/{len(selected)}: {snapshot.name}")
            if snapshot.remote_status == "signed":
                summary["already"] += 1
                self.db.update_topic_result(topic["topic_key"], "signed", "今日已签到")
                self.db.record_topic_daily(topic["topic_key"], heatmap_day, success=1)
                logger.info(f"{snapshot.name}: 今日已签到")
                consecutive_failures = 0
                continue
            if not snapshot.checkin_scheme:
                summary["failed"] += 1
                consecutive_failures += 1
                failed_keys.append(topic["topic_key"])
                self.db.update_topic_result(
                    topic["topic_key"],
                    snapshot.remote_status,
                    "没有可执行的签到 scheme",
                )
                logger.warning(f"{snapshot.name}: 没有可执行的签到 scheme")
                if (
                    policy.max_consecutive_failures
                    and consecutive_failures >= policy.max_consecutive_failures
                ):
                    summary["stopped_reason"] = "连续失败达到阈值"
                    break
                continue
            try:
                result = client.checkin(snapshot.checkin_scheme)
                if result.status == "success":
                    summary["success"] += 1
                    consecutive_failures = 0
                    self.db.update_topic_result(topic["topic_key"], "signed", result.message)
                    self.db.record_topic_daily(topic["topic_key"], heatmap_day, success=1)
                    logger.success(f"{snapshot.name}: {result.message}")
                elif result.status == "already":
                    summary["already"] += 1
                    consecutive_failures = 0
                    self.db.update_topic_result(topic["topic_key"], "signed", result.message)
                    self.db.record_topic_daily(topic["topic_key"], heatmap_day, success=1)
                    logger.info(f"{snapshot.name}: {result.message}")
                else:
                    summary["failed"] += 1
                    consecutive_failures += 1
                    failed_keys.append(topic["topic_key"])
                    self.db.update_topic_result(topic["topic_key"], "available", result.message)
                    self.db.record_topic_daily(topic["topic_key"], heatmap_day, failed=1)
                    logger.warning(f"{snapshot.name}: {result.message}")
                    if (
                        policy.max_consecutive_failures
                        and consecutive_failures >= policy.max_consecutive_failures
                    ):
                        summary["stopped_reason"] = "连续失败达到阈值"
                        break
            except WeiboRiskError:
                raise
            except Exception as exc:
                summary["failed"] += 1
                consecutive_failures += 1
                failed_keys.append(topic["topic_key"])
                self.db.update_topic_result(topic["topic_key"], "available", str(exc))
                logger.warning(f"{snapshot.name}: {exc}")
                if (
                    policy.max_consecutive_failures
                    and consecutive_failures >= policy.max_consecutive_failures
                ):
                    summary["stopped_reason"] = "连续失败达到阈值"
                    break
            if index < len(selected):
                delay = jittered_delay(policy.checkin_delay_seconds, policy.delay_jitter_percent)
                cancel_event.wait(delay * self.runtime_state.current_delay_multiplier())
        summary["cancelled"] = cancel_event.is_set()
        if failed_keys:
            summary["failed_keys"] = failed_keys[:200]
        return summary

    def _decay_delay(self) -> None:
        try:
            self.runtime_state.decay_delay()
        except Exception:
            pass


    def _run_single_checkin(
        self,
        client: Any,
        topic_key: str | None,
        logger: RunLogger,
    ) -> dict[str, Any]:
        if not topic_key:
            raise RuntimeError("缺少超话标识")
        topic = self.db.get_topic(topic_key)
        if not topic:
            raise RuntimeError("超话不存在，请先同步超话列表")
        summary = {"selected": 1, "success": 0, "already": 0, "failed": 0}
        if topic["remote_status"] == "signed":
            summary["already"] = 1
            self.db.update_topic_result(topic["topic_key"], "signed", "今日已签到")
            logger.info(f"{topic['name']}: 今日已签到")
            return summary
        scheme = topic["checkin_scheme"]
        if not scheme:
            summary["failed"] = 1
            raise RuntimeError("该超话没有可执行的签到 scheme，请先同步超话")
        result = client.checkin(scheme)
        if result.status == "success":
            summary["success"] = 1
            self.db.update_topic_result(topic["topic_key"], "signed", result.message)
            logger.success(f"{topic['name']}: {result.message}")
        elif result.status == "already":
            summary["already"] = 1
            self.db.update_topic_result(topic["topic_key"], "signed", result.message)
            logger.info(f"{topic['name']}: {result.message}")
        else:
            summary["failed"] = 1
            self.db.update_topic_result(topic["topic_key"], "available", result.message)
            logger.warning(f"{topic['name']}: {result.message}")
        return summary


class Scheduler:
    def __init__(self, db: Database, settings: Settings, manager: TaskManager):
        self.db = db
        self.settings = settings
        self.manager = manager
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune_date: str | None = None
        self._jitter_plan: tuple[str, str, int, datetime] | None = None
        self._makeup_plan: tuple[str, datetime] | None = None

    def _auto_makeup_enabled(self) -> bool:
        runtime_state = getattr(self.manager, "runtime_state", None)
        if runtime_state is None:
            return False
        policy, _ = runtime_state.snapshot()
        return bool(policy.auto_makeup)

    @staticmethod
    def should_fire(
        now_hhmm: str,
        run_time: str,
        last_run_date: str | None,
        today: str,
        catch_up: bool,
    ) -> bool:
        if last_run_date == today:
            return False
        if catch_up:
            return now_hhmm >= run_time
        return now_hhmm == run_time

    @staticmethod
    def jittered_fire_time(now: datetime, run_time: str, jitter_minutes: float) -> datetime:
        """Scheduled moment pushed forward by a random 0..N minutes (same tz as now)."""
        hour, minute = (int(part) for part in run_time.split(":", 1))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delay = random.uniform(0.0, max(0.0, jitter_minutes) * 60.0)
        return scheduled + timedelta(seconds=delay)

    def _schedule_jitter_minutes(self) -> int:
        runtime_state = getattr(self.manager, "runtime_state", None)
        if runtime_state is None:
            return 0
        policy, _ = runtime_state.snapshot()
        return max(0, policy.schedule_jitter_minutes)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="weibo-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        try:
            zone = ZoneInfo(self.settings.timezone)
        except Exception:
            zone = ZoneInfo("UTC")
        while not self._stop_event.is_set():
            try:
                schedule = self.db.get_schedule()
                now = datetime.now(zone)
                today = now.date().isoformat()
                should = schedule["enabled"] and self.should_fire(
                    now.strftime("%H:%M"),
                    schedule["run_time"],
                    schedule["last_run_date"],
                    today,
                    self.settings.schedule_catch_up,
                )
                jitter_minutes = self._schedule_jitter_minutes()
                plan = self._jitter_plan
                if plan and (
                    plan[0] != today
                    or plan[1] != schedule["run_time"]
                    or plan[2] != jitter_minutes
                ):
                    plan = None
                if should and jitter_minutes > 0:
                    if plan is None:
                        plan = (
                            today,
                            schedule["run_time"],
                            jitter_minutes,
                            self.jittered_fire_time(now, schedule["run_time"], jitter_minutes),
                        )
                        self._jitter_plan = plan
                    should = now >= plan[3]
                if should:
                    try:
                        self.manager.start("scheduled")
                    except RunBusyError:
                        pass
                    else:
                        self.db.mark_schedule_run(today)
                        self._jitter_plan = None
                        if self._auto_makeup_enabled():
                            delay = max(0, self.settings.makeup_delay_minutes) * 60
                            self._makeup_plan = (today, now + timedelta(seconds=delay))
                makeup_plan = self._makeup_plan
                if makeup_plan:
                    if makeup_plan[0] != today:
                        self._makeup_plan = None
                    elif now >= makeup_plan[1]:
                        start_iso, end_iso = local_day_window(self.settings.timezone)
                        if self.db.get_failed_keys_between(start_iso, end_iso):
                            try:
                                self.manager.start("makeup")
                            except RunBusyError:
                                pass
                            else:
                                self._makeup_plan = None
                        else:
                            self._makeup_plan = None
                if (
                    self.settings.history_retention_days
                    and self._last_prune_date != today
                ):
                    pruned = self.db.prune_history(self.settings.history_retention_days)
                    if pruned:
                        logger.info(
                            "已清理 %d 条超过 %d 天的任务记录",
                            pruned,
                            self.settings.history_retention_days,
                        )
                    self._last_prune_date = today
            except Exception:
                logger.exception("签到调度循环出错")
            self._stop_event.wait(self.settings.scheduler_poll_seconds)
