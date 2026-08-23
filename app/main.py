from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .config import NotificationSettings, RuntimePolicy, RuntimeState, Settings
from .db import Database
from .notifications import NotificationService
from .qqbot import QQBotError
from .qqevents import QQEventListener
from .security import (
    encrypt_cookie,
    hash_password,
    new_csrf_token,
    same_secret,
    verify_password,
)
from .tasks import RunBusyError, Scheduler, TaskManager
from .weibo import CookieFormatError, WeiboError, normalize_cookie


class PasswordPayload(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
    confirm_password: str = Field(min_length=12, max_length=256)


class CookiePayload(BaseModel):
    cookie: str = Field(min_length=1, max_length=16384)


class TopicTogglePayload(BaseModel):
    enabled: bool


class SchedulePayload(BaseModel):
    enabled: bool
    run_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class RuntimePolicyPayload(BaseModel):
    checkin_delay_seconds: float = Field(ge=3, le=60)
    max_topics_per_run: int = Field(ge=0, le=10000)
    max_consecutive_failures: int = Field(ge=0, le=100)
    request_timeout_seconds: float = Field(ge=5, le=60)
    read_retry_count: int = Field(ge=0, le=2)
    cooldown_on_rate_limit: bool


class NotificationPayload(BaseModel):
    enabled: bool
    app_id: str = Field(default="", max_length=128)
    user_openid: str = Field(default="", max_length=256)
    client_secret: str | None = Field(default=None, max_length=512)
    clear_client_secret: bool = False
    notify_completed: bool = True
    notify_failed: bool = True
    notify_risk: bool = True
    listen_events: bool = False


class QQDiscoveryPayload(BaseModel):
    app_id: str = Field(default="", max_length=128)
    client_secret: str | None = Field(default=None, max_length=512)


class SettingsPayload(BaseModel):
    runtime: RuntimePolicyPayload
    notifications: NotificationPayload


def _format_account(account: dict[str, Any] | None) -> dict[str, Any]:
    if not account:
        return {
            "configured": False,
            "logged_in": False,
            "imported_at": None,
            "last_verified_at": None,
            "login_uid": None,
            "login_name": None,
            "verification_message": None,
        }
    return {
        "configured": True,
        "logged_in": bool(account["logged_in"]),
        "imported_at": account["imported_at"],
        "last_verified_at": account["last_verified_at"],
        "login_uid": account["login_uid"],
        "login_name": account["login_name"],
        "verification_message": account["verification_message"],
    }


def _run_payload(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    return run


def _format_runtime_settings(
    runtime_state: RuntimeState,
    timezone_name: str,
    *,
    qq_listener: QQEventListener | None = None,
    qq_openids: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    policy, notification = runtime_state.snapshot()
    result = {
        "runtime": policy.to_dict(),
        "notifications": notification.public_dict(),
        "cooldown": runtime_state.cooldown_status(),
        "timezone": timezone_name,
    }
    if qq_listener is not None:
        result["qq_listener"] = qq_listener.status()
    if qq_openids is not None:
        result["qq_openids"] = qq_openids
    return result


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    manager: TaskManager | None = None,
    start_scheduler: bool = True,
    start_qq_listener: bool = True,
) -> FastAPI:
    app_settings = settings or Settings.from_env()
    app_db = db or Database(app_settings.db_path)
    task_manager = manager or TaskManager(app_db, app_settings)
    runtime_state = getattr(task_manager, "runtime_state", None) or RuntimeState(app_db, app_settings)
    notification_service = getattr(task_manager, "notification_service", None) or NotificationService(
        runtime_state
    )
    scheduler = Scheduler(app_db, app_settings, task_manager)
    qq_listener = QQEventListener(app_db, runtime_state)
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            scheduler.start()
        if start_qq_listener:
            qq_listener.start()
        yield
        if start_qq_listener:
            qq_listener.stop()
        scheduler.stop()

    app = FastAPI(title="微博超话签到", lifespan=lifespan)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "static")),
        name="static",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.secret_key,
        max_age=app_settings.session_max_age,
        same_site="lax",
        https_only=app_settings.cookie_secure,
        session_cookie="weibo_session",
    )
    app.state.settings = app_settings
    app.state.db = app_db
    app.state.task_manager = task_manager
    app.state.runtime_state = runtime_state
    app.state.notification_service = notification_service
    app.state.scheduler = scheduler
    app.state.qq_event_listener = qq_listener
    app.state.templates = templates

    def require_auth(request: Request) -> None:
        if not request.session.get("authenticated"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")

    def require_csrf(request: Request) -> None:
        require_auth(request)
        expected = request.session.get("csrf_token")
        supplied = request.headers.get("X-CSRF-Token")
        if not same_secret(expected, supplied):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not app_db.has_admin_password():
            return templates.TemplateResponse(request, "setup.html")
        if not request.session.get("authenticated"):
            return templates.TemplateResponse(request, "login.html")
        return templates.TemplateResponse(request, "dashboard.html")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        require_auth(request)
        return templates.TemplateResponse(request, "settings.html")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/status")
    async def api_status(request: Request):
        return {
            "setup_required": not app_db.has_admin_password(),
            "authenticated": bool(request.session.get("authenticated")),
            "csrf_token": request.session.get("csrf_token") if request.session.get("authenticated") else None,
        }

    @app.post("/api/auth/setup")
    async def setup(payload: PasswordPayload, request: Request):
        if app_db.has_admin_password():
            raise HTTPException(status_code=409, detail="管理员密码已经设置")
        app_db.set_admin_password(hash_password(payload.password))
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = new_csrf_token()
        return {"ok": True, "csrf_token": request.session["csrf_token"]}

    @app.post("/api/auth/login")
    async def login(payload: PasswordPayload, request: Request):
        password_hash = app_db.get_config("admin_password_hash")
        if not password_hash or not verify_password(password_hash, payload.password):
            raise HTTPException(status_code=401, detail="密码错误")
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = new_csrf_token()
        return {"ok": True, "csrf_token": request.session["csrf_token"]}

    @app.post("/api/auth/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.post("/api/auth/password", dependencies=[Depends(require_csrf)])
    async def change_password(payload: ChangePasswordPayload, request: Request):
        password_hash = app_db.get_config("admin_password_hash")
        if not password_hash or not verify_password(password_hash, payload.current_password):
            raise HTTPException(status_code=400, detail="当前密码错误")
        if payload.new_password != payload.confirm_password:
            raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
        if payload.new_password == payload.current_password:
            raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
        app_db.set_admin_password(hash_password(payload.new_password))
        request.session.clear()
        return {"ok": True, "reauthenticate": True}

    @app.get("/api/account", dependencies=[Depends(require_auth)])
    async def account_status():
        return _format_account(app_db.get_account())

    @app.post("/api/account/cookie", dependencies=[Depends(require_csrf)])
    async def save_cookie(payload: CookiePayload):
        try:
            cookie = normalize_cookie(payload.cookie)
        except CookieFormatError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        app_db.save_cookie(encrypt_cookie(cookie, app_settings.secret_key))
        return {"ok": True, "account": _format_account(app_db.get_account())}

    @app.post("/api/account/verify", dependencies=[Depends(require_csrf)])
    async def verify_cookie():
        account = app_db.get_account()
        if not account:
            raise HTTPException(status_code=404, detail="请先导入 Cookie")
        try:
            result = await _run_sync(
                task_manager.verify_cookie,
                _decrypt_account_cookie(app_db, app_settings),
            )
        except RunBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WeiboError as exc:
            app_db.update_verification(False, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        app_db.update_verification(result.logged_in, result.message, result.uid, result.name)
        if not result.logged_in:
            raise HTTPException(status_code=401, detail=result.message)
        return {"ok": True, "account": _format_account(app_db.get_account())}

    @app.delete("/api/account/cookie", dependencies=[Depends(require_csrf)])
    async def delete_cookie():
        app_db.clear_cookie()
        return {"ok": True}

    @app.post("/api/topics/sync", dependencies=[Depends(require_csrf)])
    async def sync_topics():
        try:
            run_id = task_manager.start("sync")
        except RunBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "run_id": run_id}

    @app.get("/api/topics", dependencies=[Depends(require_auth)])
    async def topics():
        return {"topics": app_db.list_topics()}

    @app.patch("/api/topics/{topic_key}", dependencies=[Depends(require_csrf)])
    async def toggle_topic(topic_key: str, payload: TopicTogglePayload):
        if len(topic_key) > 512:
            raise HTTPException(status_code=422, detail="topic_key 过长")
        if not app_db.update_topic_enabled(topic_key, payload.enabled):
            raise HTTPException(status_code=404, detail="超话不存在")
        return {"ok": True}

    @app.post("/api/tasks/checkin", dependencies=[Depends(require_csrf)])
    async def start_checkin():
        try:
            run_id = task_manager.start("checkin")
        except RunBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "run_id": run_id}

    @app.get("/api/tasks/current", dependencies=[Depends(require_auth)])
    async def current_task():
        return {"run": _run_payload(task_manager.current())}

    @app.post("/api/tasks/cancel", dependencies=[Depends(require_csrf)])
    async def cancel_task():
        if not task_manager.cancel():
            raise HTTPException(status_code=404, detail="没有正在运行的任务")
        return {"ok": True}

    @app.get("/api/history", dependencies=[Depends(require_auth)])
    async def history(limit: int = 20):
        return {"runs": app_db.list_runs(max(1, min(limit, 100)))}

    @app.get("/api/history/{run_id}", dependencies=[Depends(require_auth)])
    async def history_detail(run_id: int):
        run = app_db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="任务记录不存在")
        return run

    @app.get("/api/schedule", dependencies=[Depends(require_auth)])
    async def get_schedule():
        schedule = app_db.get_schedule()
        return {
            "enabled": bool(schedule["enabled"]),
            "run_time": schedule["run_time"],
            "timezone": app_settings.timezone,
            "last_run_date": schedule["last_run_date"],
        }

    @app.put("/api/schedule", dependencies=[Depends(require_csrf)])
    async def put_schedule(payload: SchedulePayload):
        app_db.save_schedule(payload.enabled, payload.run_time)
        return await get_schedule()

    @app.get("/api/settings", dependencies=[Depends(require_auth)])
    async def get_settings():
        return _format_runtime_settings(
            runtime_state,
            app_settings.timezone,
            qq_listener=qq_listener,
            qq_openids=app_db.list_qq_openids(),
        )

    @app.post("/api/qq/discovery", dependencies=[Depends(require_csrf)])
    async def start_qq_discovery(payload: QQDiscoveryPayload):
        current_policy, current_notification = runtime_state.snapshot()
        client_secret = (payload.client_secret or "").strip() or current_notification.client_secret
        try:
            notification = NotificationSettings(
                enabled=current_notification.enabled,
                app_id=payload.app_id.strip(),
                user_openid=current_notification.user_openid,
                client_secret=client_secret,
                notify_completed=current_notification.notify_completed,
                notify_failed=current_notification.notify_failed,
                notify_risk=current_notification.notify_risk,
                listen_events=True,
            )
            runtime_state.save(current_policy, notification)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        qq_listener.wake()
        return await get_settings()

    @app.put("/api/settings", dependencies=[Depends(require_csrf)])
    async def put_settings(payload: SettingsPayload):
        current_policy, current_notification = runtime_state.snapshot()
        try:
            policy = RuntimePolicy.from_mapping(
                payload.runtime.model_dump(),
                fallback=current_policy,
            )
            incoming_secret = (payload.notifications.client_secret or "").strip()
            if payload.notifications.clear_client_secret:
                if incoming_secret:
                    raise ValueError("清除 ClientSecret 时不能同时填写新密钥")
                client_secret = ""
            elif incoming_secret:
                client_secret = incoming_secret
            else:
                client_secret = current_notification.client_secret
            notification = NotificationSettings(
                enabled=payload.notifications.enabled,
                app_id=payload.notifications.app_id.strip(),
                user_openid=payload.notifications.user_openid.strip(),
                client_secret=client_secret,
                notify_completed=payload.notifications.notify_completed,
                notify_failed=payload.notifications.notify_failed,
                notify_risk=payload.notifications.notify_risk,
                listen_events=payload.notifications.listen_events,
            )
            runtime_state.save(policy, notification)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        qq_listener.wake()
        return await get_settings()

    @app.post("/api/settings/reset", dependencies=[Depends(require_csrf)])
    async def reset_settings():
        runtime_state.reset()
        qq_listener.wake()
        return await get_settings()

    @app.get("/api/qq/openids", dependencies=[Depends(require_auth)])
    async def qq_openids():
        return {
            "openids": app_db.list_qq_openids(),
            "listener": qq_listener.status(),
        }

    @app.post("/api/notifications/test", dependencies=[Depends(require_csrf)])
    async def test_notification():
        try:
            await _run_sync(app.state.notification_service.send_test)
        except (RuntimeError, QQBotError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    return app


def _decrypt_account_cookie(db: Database, settings: Settings) -> str:
    from .security import decrypt_cookie

    account = db.get_account()
    if not account:
        raise HTTPException(status_code=404, detail="请先导入 Cookie")
    try:
        return decrypt_cookie(account["cookie_ciphertext"], settings.secret_key)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Cookie 解密失败，请重新导入") from exc


async def _run_sync(function: Any, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(function, *args)


app = create_app()
