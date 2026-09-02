from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .chaoxing_client import EPHEMERAL_SELECT_KEYS, normalize_select_context_path, parse_select_context_url
from . import clock, notify
from .db import Account, PlanSeat, ReservationPlan, ReservationRun, get_db, init_db
from .scheduler import refresh_jobs, scheduler, start_scheduler, stop_scheduler
from .security import encrypt_password
from .service import active_run_count, enqueue_plan, recover_interrupted_runs
from .validation import normalize_time, validate_reservation_time_range

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
CSRF_TOKEN = secrets.token_urlsafe(24)
WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
_clock_status_cache: tuple[float, str | None] = (0.0, None)


def _clock_warning() -> str | None:
    """Expose a non-fatal warning when the local Windows clock will drift."""
    global _clock_status_cache
    checked_at, warning = _clock_status_cache
    if time.monotonic() - checked_at < 60:
        return warning
    if os.name != "nt":
        _clock_status_cache = (time.monotonic(), None)
        return None
    try:
        result = subprocess.run(
            ["sc.exe", "query", "W32Time"],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        warning = None if result.returncode == 0 and "RUNNING" in result.stdout.upper() else "Windows 时间服务未运行；请同步系统时间，开抢时可能产生偏差"
    except (OSError, subprocess.SubprocessError):
        warning = "无法确认 Windows 时间服务状态；请在开抢前同步系统时间"
    _clock_status_cache = (time.monotonic(), warning)
    return warning


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    recover_interrupted_runs()
    clock.warm_start()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="ChaoXing Local Reserve", lifespan=lifespan)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    if host not in {"127.0.0.1", "localhost"}:
        return JSONResponse(status_code=400, content={"detail": "local access only"})
    if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.url.path.startswith("/api/"):
        if request.headers.get("x-csrf-token") != CSRF_TOKEN:
            return JSONResponse(status_code=403, content={"detail": "invalid CSRF token"})
    return await call_next(request)


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)
    enabled: bool = True


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class PlanData(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=120)
    room_id: str = Field(min_length=1, max_length=64)
    seats: list[str] = Field(min_length=1, max_length=30)
    start_time: str
    end_time: str
    run_time: str = "06:59"
    day_offset: int = Field(default=1, ge=0, le=7)
    weekdays: list[str] = Field(default_factory=lambda: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    slider_enabled: bool = False
    # Risk-control cap: existing plans storing larger values are clamped here
    # and again at run time instead of being rejected.
    max_attempts: int = Field(default=3, ge=1, le=3)
    enabled: bool = True
    select_params: dict[str, str] | None = None
    select_context_path: str | None = None
    select_context_source: str | None = None

    @field_validator("max_attempts", mode="before")
    @classmethod
    def clamp_attempts(cls, value):
        try:
            return max(1, min(int(value), 3))
        except (TypeError, ValueError):
            raise ValueError("尝试次数必须是数字")

    @field_validator("select_params", mode="before")
    @classmethod
    def clean_select_params(cls, value):
        if value is None or value == {} or value == "":
            return None
        if not isinstance(value, dict):
            raise ValueError("选座链接参数无效")
        cleaned: dict[str, str] = {}
        for key, val in value.items():
            clean_key = str(key).strip()
            clean_value = str(val).strip()
            # Param names are case-sensitive on the server (deptIdEnc,
            # backLevel); transient tokens and captcha values must never be
            # persisted through a direct API request.
            if not clean_key or clean_key.lower() in EPHEMERAL_SELECT_KEYS:
                continue
            if not clean_key.replace("_", "").isalnum() or len(clean_key) > 32 or len(clean_value) > 512:
                raise ValueError("选座链接参数过长")
            cleaned[clean_key] = clean_value
        if not cleaned:
            return None
        if len(cleaned) > 12:
            raise ValueError("选座链接参数过多")
        return dict(sorted(cleaned.items()))

    @field_validator("select_context_path")
    @classmethod
    def clean_select_context_path(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        try:
            path = normalize_select_context_path(str(value))
        except ValueError:
            raise ValueError("选座页路径无效")
        if len(path) > 255:
            raise ValueError("选座页路径无效")
        return path

    @field_validator("select_context_source")
    @classmethod
    def clean_select_context_source(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        source = str(value).strip()
        if source not in {"advanced_manual", "auto_context", "room_context", "stored_auto_context"}:
            raise ValueError("选座参数来源无效")
        return source

    @field_validator("start_time", "end_time", "run_time")
    @classmethod
    def normalize_times(cls, value: str) -> str:
        return normalize_time(value)

    @field_validator("seats")
    @classmethod
    def normalize_seats(cls, value: list[str]) -> list[str]:
        """Keep one canonical, ordered candidate list at the API boundary."""
        normalized: list[str] = []
        seen: set[str] = set()
        for seat in value:
            raw = str(seat).strip()
            if not raw.isdigit():
                raise ValueError(f"座位号只能包含数字：{seat}")
            if len(raw) > 16:
                raise ValueError(f"座位号不能超过 16 位：{seat}")
            seat_num = raw.zfill(3)
            if seat_num in seen:
                raise ValueError(f"候选座位不能重复：{seat_num}")
            seen.add(seat_num)
            normalized.append(seat_num)
        return normalized

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[str]) -> list[str]:
        ordered = [day for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday") if day in set(value)]
        if not ordered:
            raise ValueError("至少选择一个任务执行星期")
        if len(ordered) != len(set(value)) or any(day not in WEEKDAYS for day in value):
            raise ValueError("星期设置无效")
        return ordered

    @model_validator(mode="after")
    def validate_time_range(self) -> "PlanData":
        validate_reservation_time_range(self.start_time, self.end_time)
        if len(self.seats) > self.max_attempts:
            raise ValueError("候选座位数不能超过总尝试次数；这样每个候选都能至少尝试一次")
        bound_room = str((self.select_params or {}).get("id", "")).strip()
        if bound_room and bound_room != self.room_id.strip():
            raise ValueError("选座参数中的阅览室 ID 与计划阅览室 ID 不一致；请重新自动发现参数")
        return self


class PlanPatch(PlanData):
    pass


# Backward-compatible import name used by earlier local tests and integrations.
PlanIn = PlanData


def _account_json(account: Account) -> dict:
    return {"id": account.id, "name": account.name, "username": account.username, "enabled": account.enabled}


def _plan_json(plan: ReservationPlan) -> dict:
    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "name": plan.name,
        "room_id": plan.room_id,
        "seats": [seat.seat_num for seat in plan.seats],
        "start_time": plan.start_time,
        "end_time": plan.end_time,
        "run_time": plan.run_time,
        "day_offset": plan.day_offset,
        "weekdays": plan.weekdays,
        "slider_enabled": plan.slider_enabled,
        "max_attempts": plan.max_attempts,
        "enabled": plan.enabled,
        "select_params": plan.select_params,
        "context_status": "ready" if plan.select_params else "not_checked",
        # Request and response use the same canonical names.  Keep the old
        # aliases temporarily for older local browser tabs.
        "select_context_source": plan.select_context_source,
        "select_context_path": plan.select_context_path,
        "select_context_checked_at": _utc_iso(plan.select_context_checked_at),
        "context_source": plan.select_context_source,
        "context_path": plan.select_context_path,
        "context_checked_at": _utc_iso(plan.select_context_checked_at),
    }


def _apply_plan_data(plan: ReservationPlan, data: PlanData) -> None:
    plan.account_id = data.account_id
    plan.name = data.name.strip()
    plan.room_id = data.room_id.strip()
    plan.start_time = data.start_time
    plan.end_time = data.end_time
    plan.run_time = data.run_time
    plan.day_offset = data.day_offset
    plan.weekdays_json = json.dumps(data.weekdays)
    plan.slider_enabled = data.slider_enabled
    plan.max_attempts = data.max_attempts
    plan.enabled = data.enabled
    plan.select_params_json = json.dumps(data.select_params, ensure_ascii=False) if data.select_params else None
    plan.select_context_path = data.select_context_path if data.select_params else None
    plan.select_context_source = (data.select_context_source or "advanced_manual") if data.select_params else None
    plan.select_context_checked_at = None
    plan.seats.clear()
    plan.seats.extend(PlanSeat(seat_num=seat, priority=index) for index, seat in enumerate(data.seats))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "csrf": CSRF_TOKEN})


@app.get("/api/csrf")
def csrf():
    return {"token": CSRF_TOKEN}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Liveness/readiness probe used by the local watchdog."""
    try:
        database_ok = db.scalar(select(1)) == 1
    except Exception:
        database_ok = False
    scheduler_ok = scheduler.running
    clock_info = clock.status()
    payload = {
        "status": "ok" if database_ok and scheduler_ok else "unhealthy",
        "database": database_ok,
        "scheduler": scheduler_ok,
        "active_runs": active_run_count() if database_ok else None,
        "clock_warning": _clock_warning(),
        "server_clock": clock_info,
    }
    if not database_ok or not scheduler_ok:
        return JSONResponse(status_code=503, content=payload)
    return payload


class SettingsIn(BaseModel):
    notify_type: str = "none"
    notify_key: str = ""


@app.get("/api/settings")
def get_settings():
    return notify.load_settings()


@app.put("/api/settings")
def put_settings(payload: SettingsIn):
    if payload.notify_type not in notify.NOTIFY_TYPES:
        raise HTTPException(422, "不支持的通知类型")
    try:
        notify.save_settings(payload.notify_type, payload.notify_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return notify.load_settings()


@app.post("/api/settings/test")
def test_settings():
    error = notify.send("抢座助手测试", "通知渠道配置成功；此后的预约结果都会推送到这里。")
    if error:
        raise HTTPException(400, f"通知发送失败：{error}")
    return {"ok": True}


@app.get("/api/accounts")
def accounts(db: Session = Depends(get_db)):
    return [_account_json(account) for account in db.scalars(select(Account).order_by(Account.id))]


@app.post("/api/accounts")
def create_account(data: AccountIn, db: Session = Depends(get_db)):
    if db.scalar(select(Account).where(Account.username == data.username)):
        raise HTTPException(409, "超星账号已经存在")
    account = Account(name=data.name, username=data.username, password_blob=encrypt_password(data.password), enabled=data.enabled)
    db.add(account)
    db.commit()
    db.refresh(account)
    return _account_json(account)


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    db.delete(account)
    db.commit()
    refresh_jobs()
    return {"ok": True}


@app.patch("/api/accounts/{account_id}")
def patch_account(account_id: int, data: AccountPatch, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    if data.name is not None:
        account.name = data.name
    if data.password is not None:
        account.password_blob = encrypt_password(data.password)
    if data.enabled is not None:
        account.enabled = data.enabled
    db.commit()
    refresh_jobs()
    return _account_json(account)


@app.get("/api/plans")
def plans(db: Session = Depends(get_db)):
    return [_plan_json(plan) for plan in db.scalars(select(ReservationPlan).order_by(ReservationPlan.id))]


@app.post("/api/plans")
def create_plan(data: PlanData, db: Session = Depends(get_db)):
    if not db.get(Account, data.account_id):
        raise HTTPException(404, "账号不存在")
    plan = ReservationPlan(account_id=data.account_id, name=data.name, room_id=data.room_id, start_time=data.start_time, end_time=data.end_time)
    _apply_plan_data(plan, data)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    refresh_jobs()
    return _plan_json(plan)


@app.patch("/api/plans/{plan_id}")
def patch_plan(plan_id: int, data: PlanPatch, db: Session = Depends(get_db)):
    plan = db.get(ReservationPlan, plan_id)
    if not plan:
        raise HTTPException(404, "计划不存在")
    if not db.get(Account, data.account_id):
        raise HTTPException(404, "账号不存在")
    _apply_plan_data(plan, data)
    db.commit()
    db.refresh(plan)
    refresh_jobs()
    return _plan_json(plan)


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(ReservationPlan, plan_id)
    if not plan:
        raise HTTPException(404, "计划不存在")
    db.delete(plan)
    db.commit()
    refresh_jobs()
    return {"ok": True}


@app.post("/api/plans/{plan_id}/probe")
def probe_plan(plan_id: int, db: Session = Depends(get_db)):
    if not db.get(ReservationPlan, plan_id):
        raise HTTPException(404, "计划不存在")
    return {"accepted": True, "mode": "probe", "run_id": enqueue_plan(plan_id, "probe", probe_only=True)}


@app.post("/api/plans/{plan_id}/discover")
def discover_plan_context(plan_id: int, db: Session = Depends(get_db)):
    """Run the read-only target-date discovery check without submitting."""
    if not db.get(ReservationPlan, plan_id):
        raise HTTPException(404, "计划不存在")
    return {"accepted": True, "mode": "discover", "run_id": enqueue_plan(plan_id, "discover", probe_only=True)}


class SelectUrlIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


@app.post("/api/tools/parse-select-url")
def parse_select_link(data: SelectUrlIn):
    """Parse a pasted seat-select page link; nothing is fetched or submitted."""
    try:
        params, select_path = parse_select_context_url(data.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"params": params, "room_id": params.get("id", ""), "select_context_path": select_path}


@app.post("/api/plans/{plan_id}/run")
def run_plan(plan_id: int, db: Session = Depends(get_db)):
    if not db.get(ReservationPlan, plan_id):
        raise HTTPException(404, "计划不存在")
    return {"accepted": True, "mode": "reserve", "run_id": enqueue_plan(plan_id, "manual", probe_only=False)}


@app.post("/api/runs/{run_id}/confirm-duplicate")
def confirm_duplicate_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(ReservationRun, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    if run.status != "NEEDS_VERIFICATION" or run.error_code not in {"POSSIBLE_DUPLICATE", "LEGACY_SUCCESS"}:
        raise HTTPException(409, "这条记录不需要重复预约确认")
    if not run.plan_id or not db.get(ReservationPlan, run.plan_id):
        raise HTTPException(409, "原计划已不存在，无法确认重试")
    run.status = "SKIPPED"
    run.error_code = "DUPLICATE_CONFIRMATION_ACCEPTED"
    run.message = f"{run.message}；用户已确认，已创建一次新的预约任务"
    db.commit()
    confirmed_run_id = enqueue_plan(run.plan_id, "manual_confirmed", probe_only=False, override_duplicate=True, duplicate_of_run_id=run.duplicate_of_run_id)
    return {"accepted": True, "run_id": confirmed_run_id}


def _utc_iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")


def _run_json(run: ReservationRun) -> dict:
    return {
        "id": run.id,
        "plan_id": run.plan_id,
        "plan_name": run.plan_name or "已删除计划",
        "account_name": run.account_name or "已删除账号",
        "target_date": run.target_date,
        "request_fingerprint": run.request_fingerprint,
        "candidate_seats": run.candidate_seats,
        "selected_seat": run.selected_seat,
        "trigger": run.trigger,
        "status": run.status,
        "error_code": run.error_code,
        "duplicate_of_run_id": run.duplicate_of_run_id,
        "duplicate_override": run.duplicate_override,
        "message": run.message,
        "probe_results": run.probe_results,
        "parameter_source": run.parameter_source,
        "attempt_details": run.attempt_details,
        "started_at": _utc_iso(run.started_at),
        "finished_at": _utc_iso(run.finished_at),
    }


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(ReservationRun, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    return _run_json(run)


@app.get("/api/runs")
def runs(db: Session = Depends(get_db)):
    return [_run_json(run) for run in db.scalars(select(ReservationRun).order_by(desc(ReservationRun.id)).limit(100))]
