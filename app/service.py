from __future__ import annotations

import datetime as dt
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import chaoxing_client as _chaoxing_client
from . import clock as app_clock
from .chaoxing_client import CAPTCHA_TYPES, ChaoxingClient, ProbeResult, ReservationError
from .notify import submit_async as notify_async
from .db import Account, ReservationPlan, ReservationRun, SessionLocal
from .security import decrypt_password, redact
from .validation import normalize_time, validate_reservation_time_range

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUN_LIMIT_SECONDS = 60
MAX_ACCOUNT_WORKERS = 4
DUPLICATE_COOLDOWN_SECONDS = 5 * 60
# Risk-control cap: a whole plan tries at most this many submit rounds no
# matter what older plans stored (GitHub issue #89: high-frequency requests
# triggered account risk warnings). Since the no-repeat rotation upgrade the
# budget buys one shot per *distinct* candidate seat, so the cap also limits
# how large a candidate pool can be fully swept in a single run.
MAX_SUBMIT_ATTEMPTS = 6
# Errors that make another candidate (or a retry after the opening switch)
# worth trying instead of stopping the run. TARGET_*_UNCOVERABLE/NOT_OPEN are
# what the platform answers in the milliseconds before the window really
# switches, so an opening-moment shot must rotate/retry rather than die.
_RETRYABLE_CODES = {"SEAT_UNAVAILABLE", "TARGET_CONTEXT_UNAVAILABLE", "TARGET_DAY_NOT_OPEN"}
# Opening-window discipline, bot-race edition: the scheduler wakes LEAD
# seconds early, login and browsing warm up, one poller re-fetches the
# target-day page ever more densely through the opening moment (a single
# early fetch only ever sees the not-open page), and the submit fires on the
# platform's own clock with only token jitter left.
FIRE_JITTER_RANGE = (0.0, 0.05)
PREFETCH_LEAD_SECONDS = 2.0
# Poll cadence of the opening race: relaxed while the moment is far away,
# dense only around the switch itself so total traffic stays bounded.
OPENING_POLL_RELAXED = 0.2
OPENING_POLL_DENSE = 0.06
DENSE_WINDOW_BEFORE_FIRE = 0.3
OPENING_GRACE_SECONDS = 3.0
FIRE_BUSY_STEP = 0.005
FIRST_PASS_WAIT_RANGE = (0.05, 0.15)
RETRY_WAIT_RANGE = (0.3, 0.6)
# How many candidate seats submit simultaneously at the opening moment.
PARALLEL_SEAT_LIMIT = 4
_NOTIFY_STATUSES = {"SUCCESS", "FAILED", "SKIPPED", "NEEDS_VERIFICATION", "BLOCKED_BY_RISK"}
_executor = ThreadPoolExecutor(max_workers=MAX_ACCOUNT_WORKERS, thread_name_prefix="reserve")
_account_locks: dict[int, threading.Lock] = {}
_account_locks_guard = threading.Lock()


def now_shanghai() -> dt.datetime:
    return dt.datetime.now(SHANGHAI)


def _account_lock(account_id: int) -> threading.Lock:
    with _account_locks_guard:
        return _account_locks.setdefault(account_id, threading.Lock())


def _target_day(plan: ReservationPlan, now: dt.datetime | None = None) -> dt.date:
    return (now or now_shanghai()).date() + dt.timedelta(days=plan.day_offset)


def _today_enabled(plan: ReservationPlan, now: dt.datetime | None = None) -> bool:
    return (now or now_shanghai()).strftime("%A") in plan.weekdays


def _opening_at(plan: ReservationPlan, now: dt.datetime | None = None) -> dt.datetime | None:
    """Today's configured platform-opening time in Shanghai."""
    current = now or now_shanghai()
    try:
        hour, minute = map(int, plan.run_time.split(":", 1))
    except (AttributeError, TypeError, ValueError):
        return None
    return current.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _target_page_is_expected_to_wait(plan: ReservationPlan, target_day: dt.date, now: dt.datetime | None = None) -> bool:
    current = now or now_shanghai()
    opening = _opening_at(plan, current)
    return bool(opening and target_day > current.date() and current < opening)


def _run_snapshot(plan: ReservationPlan, trigger: str) -> ReservationRun:
    target_day = _target_day(plan)
    request_snapshot = {
        "room_id": plan.room_id.strip(),
        "start_time": normalize_time(plan.start_time),
        "end_time": normalize_time(plan.end_time),
        "seats": [seat.seat_num for seat in plan.seats],
        "select_params": plan.select_params,
        "select_context_path": plan.select_context_path,
        "select_context_source": plan.select_context_source,
    }
    return ReservationRun(
        plan_id=plan.id,
        account_id=plan.account_id,
        plan_name=plan.name,
        account_name=plan.account.name,
        target_date=target_day.isoformat(),
        request_fingerprint=_reservation_fingerprint(plan, target_day),
        candidate_seats_json=json.dumps(request_snapshot["seats"]),
        request_snapshot_json=json.dumps(request_snapshot, ensure_ascii=False),
        trigger=trigger,
        status="PENDING",
        message="等待执行",
    )


def _reservation_fingerprint(plan: ReservationPlan, target_day: dt.date) -> str:
    """Identify one reservation intent without tying it to a fallback seat."""
    return "|".join((target_day.isoformat(), plan.room_id.strip(), normalize_time(plan.start_time), normalize_time(plan.end_time)))


def enqueue_plan(
    plan_id: int,
    trigger: str = "manual",
    *,
    probe_only: bool = False,
    override_duplicate: bool = False,
    duplicate_of_run_id: int | None = None,
) -> int:
    """Create a durable pending run then hand it to the bounded local worker pool."""
    db = SessionLocal()
    try:
        plan = db.get(ReservationPlan, plan_id)
        if not plan:
            raise ValueError("plan not found")
        run = _run_snapshot(plan, trigger)
        run.duplicate_override = override_duplicate
        run.duplicate_of_run_id = duplicate_of_run_id
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    _executor.submit(execute_plan, plan_id, trigger, probe_only=probe_only, run_id=run_id, override_duplicate=override_duplicate)
    return run_id


def _set_failure(run: ReservationRun, code: str, message: str, *, status: str = "FAILED") -> None:
    run.status = status
    run.error_code = code
    run.message = redact(message)


def _append_attempt(run: ReservationRun, *, seat: str, source: str, submitted: bool, code: str | None, message: str) -> None:
    """Persist a small, secret-free account of what actually happened."""
    details = run.attempt_details
    details.append(
        {
            "seat": seat,
            "source": source or "unknown",
            "submitted": bool(submitted),
            "code": code,
            "message": redact(message),
        }
    )
    run.attempt_details_json = json.dumps(details, ensure_ascii=False)


def _persist_discovered_context(plan: ReservationPlan, client: ChaoxingClient) -> None:
    params = getattr(client, "last_discovered_select_params", None)
    source = getattr(client, "last_parameter_source", "")
    path = getattr(client, "last_discovered_select_path", None)
    # A manually pasted link remains an explicit override.  Automatic context
    # discovery, however, must be allowed to refresh an older stored automatic
    # path/parameter set (including the historic absolute-URL bug).
    automatic_sources = {"auto_context", "room_context", "stored_auto_context"}
    if params and (not plan.select_params or source in automatic_sources):
        plan.select_params_json = json.dumps(params, ensure_ascii=False)
        plan.select_context_source = "auto_context" if source == "stored_auto_context" else (source or "auto_context")
        plan.select_context_path = path
    if source:
        plan.select_context_checked_at = datetime.now(dt.UTC).replace(tzinfo=None)


def _find_successful_duplicate(db, run: ReservationRun) -> ReservationRun | None:
    if not run.account_id or not run.request_fingerprint:
        return None
    return db.scalars(
        select(ReservationRun).where(
            ReservationRun.id != run.id,
            ReservationRun.account_id == run.account_id,
            ReservationRun.request_fingerprint == run.request_fingerprint,
            ReservationRun.status == "SUCCESS",
            ReservationRun.trigger != "probe",
        ).order_by(ReservationRun.finished_at.desc(), ReservationRun.id.desc()).limit(1)
    ).first()


def _find_legacy_success(db, run: ReservationRun) -> ReservationRun | None:
    """Old runs lack a room/time fingerprint, so they can only be a warning."""
    if not run.account_id or not run.target_date:
        return None
    return db.scalars(
        select(ReservationRun).where(
            ReservationRun.id != run.id,
            ReservationRun.account_id == run.account_id,
            ReservationRun.target_date == run.target_date,
            ReservationRun.request_fingerprint.is_(None),
            ReservationRun.status == "SUCCESS",
            ReservationRun.trigger != "probe",
        ).order_by(ReservationRun.finished_at.desc(), ReservationRun.id.desc()).limit(1)
    ).first()


def _is_recent_success(run: ReservationRun) -> bool:
    completed_at = run.finished_at or run.started_at
    if completed_at is None:
        return False
    age_seconds = (datetime.now(dt.UTC).replace(tzinfo=None) - completed_at).total_seconds()
    return 0 <= age_seconds <= DUPLICATE_COOLDOWN_SECONDS


def _duplicate_summary(run: ReservationRun) -> str:
    seat = run.selected_seat or "未记录座位"
    return f"本地成功记录 #{run.id}：目标日 {run.target_date}，座位 {seat}"


def _server_reservation_brief(match: dict) -> str:
    tz = dt.timezone(dt.timedelta(hours=8))

    def fmt(value) -> str:
        try:
            return dt.datetime.fromtimestamp(int(value) / 1000, tz).strftime("%H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            return "??:??"

    return f"座位 {match.get('seatNum', '?')}，{fmt(match.get('startTime'))}–{fmt(match.get('endTime'))}"


def _verify_duplicate_on_server(client: ChaoxingClient, request_values: dict, target_day: dt.date) -> tuple[str, str]:
    """Cross-check a local duplicate against the platform's reservation list.

    Returns ``(state, detail)`` where state is ``found`` (the platform itself
    still holds an overlapping reservation), ``absent`` (verified: nothing on
    the platform overlaps this slot) or ``unavailable`` (could not verify —
    callers must keep the safe stop behaviour). Only a positive, well-formed
    server answer may clear a local duplicate.
    """
    try:
        reservations = client.fetch_reservations(request_values.get("select_params"), request_values.get("select_context_path"))
        # Always the real matcher: tests may fake the client, but matching
        # logic must stay the production one.
        match = _chaoxing_client.ChaoxingClient.find_reservation(
            reservations, target_day, request_values["room_id"], request_values["start_time"], request_values["end_time"]
        )
    except ReservationError as exc:
        return "unavailable", f"核实失败（{exc.code}：{exc.message}）"
    except Exception as exc:  # verification must never crash the run itself
        return "unavailable", f"核实异常（{exc.__class__.__name__}）"
    if match is None:
        return "absent", ""
    return "found", _server_reservation_brief(match)


def _failure_status(code: str) -> str:
    if code in {"NEEDS_VERIFICATION", "CAPTCHA_REQUIRED", "SECURITY_CHALLENGE", "LOGIN_REQUIRED", "INTERRUPTED_NEEDS_VERIFICATION", "SUBMIT_OUTCOME_UNKNOWN", "SUBMIT_REJECTED"}:
        return "NEEDS_VERIFICATION"
    if code == "BLOCKED_BY_RISK":
        return "BLOCKED_BY_RISK"
    return "FAILED"


def _wait_between_attempts(deadline: float) -> None:
    # A short, jittered spacing: dense enough for an opening race against
    # other programs, sparse enough not to look like a hammer.
    delay = random.uniform(*RETRY_WAIT_RANGE)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReservationError("DEADLINE_EXCEEDED", "任务超过 60 秒运行上限")
    time.sleep(min(delay, remaining))


def _wait_first_pass(deadline: float) -> None:
    # Switching between distinct candidate seats on the first rotation mimics a
    # quick human re-click. Later rounds keep the conservative spacing above.
    delay = random.uniform(*FIRST_PASS_WAIT_RANGE)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReservationError("DEADLINE_EXCEEDED", "任务超过 60 秒运行上限")
    time.sleep(min(delay, remaining))


def _scheduled_fire_epoch(plan: ReservationPlan) -> float | None:
    """Server-clock epoch when a scheduled run may submit.

    The plan's run time is treated as the platform's wall-clock opening moment;
    a dense clock calibration runs right before the decisive wait (see
    ``clock.refresh(dense=True)``). A small random jitter keeps every day's
    firing time from being bit-identical. Runs that already missed the moment
    fire immediately instead of waiting a day.
    """
    try:
        hour, minute = map(int, plan.run_time.split(":", 1))
    except ValueError:
        return None
    app_clock.refresh(dense=True)
    now_epoch = app_clock.server_now()
    now_server = dt.datetime.fromtimestamp(now_epoch, SHANGHAI)
    target = now_server.replace(hour=hour, minute=minute, second=0, microsecond=0) + dt.timedelta(seconds=random.uniform(*FIRE_JITTER_RANGE))
    target_epoch = target.timestamp()
    return target_epoch if target_epoch > now_epoch else now_epoch


def _hold_until_fire(fire_epoch: float) -> None:
    """Busy-wait the last sliver so the submit lands just past the moment."""
    while True:
        remaining = fire_epoch - app_clock.server_now()
        if remaining <= 0:
            break
        time.sleep(min(remaining, FIRE_BUSY_STEP))


def _poll_until_open(client: ChaoxingClient, fire_epoch: float, request_values: dict, target_day: dt.date) -> ProbeResult | None:
    """Re-fetch the target-day select page through the opening moment.

    Before the window opens the platform answers a perfectly valid request
    with its not-open error page, so one early fetch is worthless: the race is
    decided by whoever re-fetches within milliseconds of the switch. Poll with
    one cheap GET, relaxed early and dense only around the decisive moment.
    Returns the first usable page, or None when the window never opened within
    the grace period (the serial submit path then re-resolves on its own).
    """
    # Wait quietly until the prefetch lead window begins.
    while True:
        remaining = fire_epoch - app_clock.server_now()
        if remaining <= PREFETCH_LEAD_SECONDS:
            break
        time.sleep(min(remaining - PREFETCH_LEAD_SECONDS, 0.2))
    context = request_values["select_params"] or {"id": request_values["room_id"].strip()}
    select_path = request_values["select_context_path"]
    while app_clock.server_now() <= fire_epoch + OPENING_GRACE_SECONDS:
        try:
            result = client.fetch_target_day_page(target_day, dict(context), select_path)
        except ReservationError:
            result = None
        if result is not None and (result.ok or result.captcha_type in CAPTCHA_TYPES):
            return result
        remaining = fire_epoch - app_clock.server_now()
        interval = OPENING_POLL_DENSE if remaining <= DENSE_WINDOW_BEFORE_FIRE else OPENING_POLL_RELAXED
        time.sleep(interval)
    return None


def _await_fire_and_prefetch(
    client: ChaoxingClient,
    fire_epoch: float,
    request_values: dict,
    target_day: dt.date,
    seat: str,
):
    """Block until the window opens, then hand the caller a fresh page.

    Returns the resolved ProbeResult for the first submit, or None when the
    window did not open in time — the submit path then resolves itself.
    """
    pre = _poll_until_open(client, fire_epoch, request_values, target_day)
    if pre is None:
        return None
    # The window may have opened before the planned moment; never submit early.
    _hold_until_fire(fire_epoch)
    return pre


def _parallel_opening_shot(
    client: ChaoxingClient,
    request_values: dict,
    target_day: dt.date,
    seats: list[str],
    fire_epoch: float,
    deadline: float,
):
    """Opening-moment race: one dense poller, then every candidate submits.

    The poller detects the switch; each racer then resolves its own fresh page
    on a cloned session (one GET, in parallel) and submits its seat the moment
    the server clock allows. First success stops the rest. Returns
    ``(outcomes, winner)`` where outcomes audit every racer and winner is the
    successful outcome dict, or ``( [], None )`` when the window never opened
    and the serial fallback must take over.
    """
    room_id = request_values["room_id"]
    start_time = request_values["start_time"]
    end_time = request_values["end_time"]
    select_params = request_values["select_params"]
    select_path = request_values["select_context_path"]
    select_source = request_values["select_context_source"]
    workers = seats[:PARALLEL_SEAT_LIMIT]

    pre = _poll_until_open(client, fire_epoch, request_values, target_day)
    if pre is None:
        return [], None

    outcomes: list[dict] = []
    winner: dict | None = None
    guard = threading.Lock()
    stop = threading.Event()

    def race(seat: str) -> None:
        nonlocal winner
        if stop.is_set():
            return
        racer = client.clone_authenticated()
        resolved = None
        try:
            resolved = racer.resolve_submission_page(
                room_id, seat, target_day,
                select_params=select_params, select_path=select_path, select_source=select_source,
            )
        except ReservationError:
            resolved = None
        if resolved is None:
            # Last resort: the poller's page may still carry a usable token.
            if not (pre is not None and pre.ok and pre.captcha_type not in CAPTCHA_TYPES):
                return
            resolved = pre
        _hold_until_fire(fire_epoch)
        if stop.is_set():
            return
        try:
            message = racer.submit_once(
                room_id, seat, start_time, end_time, target_day,
                select_params=select_params, select_path=select_path, select_source=select_source,
                pre_resolved=resolved,
            )
        except ReservationError as exc:
            with guard:
                outcomes.append({
                    "seat": seat, "code": exc.code, "message": exc.message,
                    "submitted": bool(getattr(racer, "last_submitted", False)),
                    "source": getattr(racer, "last_parameter_source", "") or "unknown",
                })
            return
        with guard:
            outcomes.append({"seat": seat, "code": None, "message": message, "submitted": True,
                             "source": getattr(racer, "last_parameter_source", "") or "unknown"})
            if winner is None:
                winner = outcomes[-1]
                stop.set()

    threads = [threading.Thread(target=race, args=(seat,), name=f"opening-{seat}", daemon=True) for seat in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return outcomes, winner


def recover_interrupted_runs() -> int:
    """Mark stale durable work for review instead of blindly re-submitting it.

    An external reservation can succeed just before a local process crashes, so
    replaying a pending row would risk duplicate reservations.
    """
    db = SessionLocal()
    try:
        cutoff = datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(seconds=RUN_LIMIT_SECONDS + 15)
        stale = db.scalars(
            select(ReservationRun).where(
                ReservationRun.status.in_(("PENDING", "RUNNING")),
                ReservationRun.started_at < cutoff,
            )
        ).all()
        now = datetime.now(dt.UTC).replace(tzinfo=None)
        for run in stale:
            run.status = "NEEDS_VERIFICATION"
            run.error_code = "INTERRUPTED_NEEDS_VERIFICATION"
            run.message = "服务在该任务完成前中断；为避免重复预约，请先在超星端确认"
            run.finished_at = now
        db.commit()
        return len(stale)
    finally:
        db.close()


def active_run_count() -> int:
    db = SessionLocal()
    try:
        return int(
            db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ReservationRun).where(ReservationRun.status.in_(("PENDING", "RUNNING"))))
            or 0
        )
    finally:
        db.close()


def _request_values(plan: ReservationPlan, run: ReservationRun) -> dict:
    """Return immutable queued values, with a safe fallback for pre-v7 runs."""
    snapshot = run.request_snapshot
    seats = snapshot.get("seats") if isinstance(snapshot.get("seats"), list) else None
    room_id = str(snapshot.get("room_id") or "").strip()
    start_time = str(snapshot.get("start_time") or "").strip()
    end_time = str(snapshot.get("end_time") or "").strip()
    if room_id and start_time and end_time and seats:
        params = snapshot.get("select_params")
        return {
            "room_id": room_id,
            "start_time": normalize_time(start_time),
            "end_time": normalize_time(end_time),
            "seats": [str(seat) for seat in seats],
            "select_params": params if isinstance(params, dict) and params else None,
            "select_context_path": snapshot.get("select_context_path") or None,
            "select_context_source": snapshot.get("select_context_source") or None,
        }
    return {
        "room_id": plan.room_id,
        "start_time": plan.start_time,
        "end_time": plan.end_time,
        "seats": [seat.seat_num for seat in plan.seats],
        "select_params": plan.select_params,
        "select_context_path": plan.select_context_path,
        "select_context_source": plan.select_context_source,
    }


def execute_plan(
    plan_id: int,
    trigger: str = "manual",
    *,
    probe_only: bool = False,
    run_id: int | None = None,
    override_duplicate: bool = False,
) -> int:
    """Run one plan synchronously. Public callers should normally use enqueue_plan()."""
    db = SessionLocal()
    lock: threading.Lock | None = None
    lock_acquired = False
    started_monotonic = time.monotonic()
    deadline = started_monotonic + RUN_LIMIT_SECONDS
    run: ReservationRun | None = None
    try:
        plan = db.get(ReservationPlan, plan_id)
        if run_id:
            run = db.get(ReservationRun, run_id)
        if run is None:
            if not plan:
                run = ReservationRun(plan_id=plan_id, trigger=trigger, status="RUNNING", message="计划不存在")
            else:
                run = _run_snapshot(plan, trigger)
                run.status = "RUNNING"
                run.message = "开始执行"
                run.duplicate_override = override_duplicate
            db.add(run)
            db.commit()
        if not plan:
            _set_failure(run, "PLAN_NOT_FOUND", "计划不存在")
            return run.id
        account = db.get(Account, run.account_id or plan.account_id)
        if account is None:
            _set_failure(run, "ACCOUNT_NOT_FOUND", "账号不存在或已删除")
            return run.id
        if (not plan.enabled and trigger in {"scheduled", "scheduled_catchup"}) or not account.enabled:
            _set_failure(run, "PLAN_OR_ACCOUNT_DISABLED", "计划或账号已停用")
            return run.id
        request_values = _request_values(plan, run)
        if not request_values["seats"]:
            _set_failure(run, "NO_CANDIDATE_SEAT", "未配置候选座位")
            return run.id
        if trigger in {"scheduled", "scheduled_catchup"} and not _today_enabled(plan):
            _set_failure(run, "WEEKDAY_NOT_ENABLED", "今天不在该计划的执行星期内")
            return run.id
        if not probe_only:
            try:
                validate_reservation_time_range(request_values["start_time"], request_values["end_time"])
            except ValueError as exc:
                _set_failure(run, "INVALID_TIME_RANGE", str(exc))
                return run.id
        lock = _account_lock(account.id)
        remaining = deadline - time.monotonic()
        lock_acquired = lock.acquire(timeout=max(0, remaining))
        if not lock_acquired:
            _set_failure(run, "ACCOUNT_BUSY_TIMEOUT", "同一账号的前一个任务在 60 秒内未结束")
            return run.id
        client = ChaoxingClient(
            account.username,
            decrypt_password(account.password_blob),
            slider_enabled=plan.slider_enabled,
            deadline=deadline,
        )
        target_day = dt.date.fromisoformat(run.target_date) if run.target_date else _target_day(plan)
        duplicate: ReservationRun | None = None
        if not probe_only:
            duplicate = _find_successful_duplicate(db, run)
            if duplicate:
                run.duplicate_of_run_id = duplicate.id
                if _is_recent_success(duplicate):
                    run.status = "SKIPPED"
                    run.error_code = "RECENT_DUPLICATE"
                    run.message = f"{_duplicate_summary(duplicate)}；5 分钟内重复点击，已跳过"
                    return run.id
                # Verify against the platform before stopping: the local record
                # may be stale (the user cancelled on the server side) or may
                # still be the live reservation this plan should keep.
                try:
                    client.login()
                except ReservationError as exc:
                    state, detail = "unavailable", f"核实前登录失败（{exc.code}：{exc.message}）"
                else:
                    state, detail = _verify_duplicate_on_server(client, request_values, target_day)
                if state == "found":
                    run.status = "SKIPPED"
                    run.error_code = "ALREADY_BOOKED_ON_SERVER"
                    run.message = f"超星端已存在本场次预约（{detail}），无需重复预约；对应{_duplicate_summary(duplicate)}"
                    return run.id
                if state == "absent":
                    run.message = f"{_duplicate_summary(duplicate)}已不在超星端（可能已取消），继续重新预约"
                else:
                    if trigger == "scheduled":
                        _set_failure(run, "POSSIBLE_DUPLICATE", f"{_duplicate_summary(duplicate)}；无法核实超星端状态（{detail}），已停止等待人工检查", status="NEEDS_VERIFICATION")
                        return run.id
                    if not override_duplicate:
                        _set_failure(run, "POSSIBLE_DUPLICATE", f"{_duplicate_summary(duplicate)}；无法核实超星端状态（{detail}），请在页面确认后再执行", status="NEEDS_VERIFICATION")
                        return run.id
                    run.message = f"已确认忽略 {_duplicate_summary(duplicate)}，正在继续预约"
            else:
                duplicate = _find_legacy_success(db, run)
                if duplicate:
                    run.duplicate_of_run_id = duplicate.id
                    try:
                        client.login()
                    except ReservationError as exc:
                        state, detail = "unavailable", f"核实前登录失败（{exc.code}：{exc.message}）"
                    else:
                        state, detail = _verify_duplicate_on_server(client, request_values, target_day)
                    if state == "found":
                        run.status = "SKIPPED"
                        run.error_code = "ALREADY_BOOKED_ON_SERVER"
                        run.message = f"超星端已存在本场次预约（{detail}），无需重复预约；对应旧成功记录 #{duplicate.id}"
                        return run.id
                    if state == "absent":
                        run.message = f"旧成功记录 #{duplicate.id}已不在超星端（可能已取消），继续重新预约"
                    else:
                        if trigger == "scheduled":
                            _set_failure(run, "LEGACY_SUCCESS", f"发现未带完整预约信息的旧成功记录 #{duplicate.id}；无法核实超星端状态（{detail}），已停止等待人工检查", status="NEEDS_VERIFICATION")
                            return run.id
                        if not override_duplicate:
                            _set_failure(run, "LEGACY_SUCCESS", f"发现旧成功记录 #{duplicate.id}，但缺少房间或时间快照，无法确认是否仍有效；请在页面确认后再执行", status="NEEDS_VERIFICATION")
                            return run.id
                        run.message = f"已确认忽略旧成功记录 #{duplicate.id}，正在继续预约"
        run.status = "RUNNING"
        if run.message in {"", "等待执行", "开始执行"}:
            run.message = "正在登录并准备预约"
        db.commit()
        client.login()
        seats = request_values["seats"]
        messages: list[str] = []
        if probe_only:
            probe_results: list[dict] = []
            target_day = dt.date.fromisoformat(run.target_date or _target_day(plan).isoformat())
            select_params = request_values["select_params"]
            select_path = request_values["select_context_path"]
            select_source = request_values["select_context_source"]
            page_result = None
            try:
                page_result = client.resolve_submission_page(
                    request_values["room_id"], seats[0], target_day,
                    select_params=select_params, select_path=select_path, select_source=select_source,
                    require_target_day=True,
                )
                _persist_discovered_context(plan, client)
                run.parameter_source = getattr(client, "last_parameter_source", "") or "unknown"
            except ReservationError as exc:
                if exc.code == "TARGET_DAY_NOT_OPEN" and _target_page_is_expected_to_wait(plan, target_day):
                    opening = _opening_at(plan)
                    opening_text = opening.strftime("%Y-%m-%d %H:%M") if opening else plan.run_time
                    wait_message = (
                        f"计划配置有效；目标日 {target_day.isoformat()} 的预约窗口尚未开放。"
                        f"系统将在 {opening_text} 按计划重新获取目标日参数并执行，无需现在取得 token"
                    )
                    probe_results.extend(
                        {
                            "seat": seat,
                            "page_state": "TARGET_DAY_NOT_OPEN",
                            "ok": True,
                            "message": wait_message,
                        }
                        for seat in seats
                    )
                    run.status = "PROBE_DONE"
                    run.error_code = "PROBE_WAITING_OPEN"
                    run.parameter_source = "target_day_at_opening"
                    run.probe_results_json = json.dumps(probe_results, ensure_ascii=False)
                    run.message = redact(wait_message)
                    return run.id
                probe_results.append({"seat": "-", "page_state": exc.code, "ok": False, "message": redact(f"{exc.code}：{exc.message}")})
                run.probe_results_json = json.dumps(probe_results, ensure_ascii=False)
                _set_failure(run, exc.code, f"目标日页面检查失败：{exc.message}", status=_failure_status(exc.code))
                return run.id
            if page_result is not None:
                room_bound = str((getattr(client, "last_discovered_select_params", None) or select_params or {}).get("id", "")).strip()
                if room_bound and room_bound != request_values["room_id"].strip():
                    messages.append(f"警告：选座链接房间 {room_bound} 与计划房间 {request_values['room_id']} 不一致")
                for seat in seats:
                    if page_result.captcha_type in {"slider", "point_click"}:
                        message = f"目标日页面要求{page_result.captcha_type}验证码；已安全停止，不会提交"
                        probe_ok = False
                    elif page_result.ok:
                        source = getattr(client, "last_parameter_source", "") or "unknown"
                        message = f"目标日 {target_day.isoformat()}：参数就绪（来源：{source}），可进入提交"
                        probe_ok = True
                    elif page_result.page_state == "CURRENTLY_OCCUPIED":
                        message = f"目标日页面出现状态提示：{page_result.message}"
                        probe_ok = False
                    else:
                        message = f"目标日页面异常：{page_result.message}"
                        probe_ok = False
                    probe_results.append({"seat": seat, "page_state": page_result.page_state, "ok": probe_ok, "message": redact(message)})
            run.status = "PROBE_DONE"
            run.error_code = "PROBE_COMPLETE"
            run.probe_results_json = json.dumps(probe_results, ensure_ascii=False)
            summary = "；".join(f"座位 {item['seat']}：{item['message']}" for item in probe_results if item["seat"] != "-")
            run.message = redact("；".join(filter(None, [*(message for message in messages), summary])))
            return run.id
        target_day = dt.date.fromisoformat(run.target_date or _target_day(plan).isoformat())
        pre_resolved = None
        parallel_outcomes: list[dict] = []
        winner: dict | None = None
        if trigger in {"scheduled", "scheduled_catchup"}:
            fire_epoch = _scheduled_fire_epoch(plan)
            if fire_epoch is not None:
                # Warm the session like a person already sitting on the page,
                # then hold the submit until the platform's own clock says so.
                run.message = "已提前唤醒，预热会话并等待开抢时刻"
                db.commit()
                client.browse(request_values["room_id"], seats[0])
                if len(seats) > 1:
                    parallel_outcomes, winner = _parallel_opening_shot(client, request_values, target_day, seats, fire_epoch, deadline)
                    for outcome in parallel_outcomes:
                        _append_attempt(
                            run,
                            seat=outcome["seat"],
                            source=outcome["source"],
                            submitted=outcome["submitted"],
                            code=outcome["code"],
                            message=outcome["message"],
                        )
                        messages.append(f"开抢并行，座位 {outcome['seat']}：{outcome['code'] or 'SUCCESS'} {outcome['message']}")
                    if winner is not None:
                        _persist_discovered_context(plan, client)
                        run.selected_seat = winner["seat"]
                        run.parameter_source = winner["source"]
                        run.status = "SUCCESS"
                        run.error_code = None
                        run.message = redact(f"座位 {winner['seat']}：{winner['message']}")
                        return run.id
                else:
                    pre_resolved = _await_fire_and_prefetch(client, fire_epoch, request_values, target_day, seats[0])
        attempts = min(max(plan.max_attempts, len(seats)), MAX_SUBMIT_ATTEMPTS)
        if plan.max_attempts > MAX_SUBMIT_ATTEMPTS:
            messages.append(f"按安全策略将尝试次数从 {plan.max_attempts} 钳制为 {MAX_SUBMIT_ATTEMPTS}")
        # A confirmed-unavailable seat is dead: never spend another submit on
        # it. The parallel opening shot may already have consumed candidates.
        dead_seats = {outcome["seat"] for outcome in parallel_outcomes if outcome["code"] == "SEAT_UNAVAILABLE"}
        used_attempts = sum(1 for outcome in parallel_outcomes if outcome["submitted"])
        for attempt in range(used_attempts, attempts):
            if time.monotonic() >= deadline:
                raise ReservationError("DEADLINE_EXCEEDED", "任务超过 60 秒运行上限")
            seat = next((candidate for candidate in seats if candidate not in dead_seats), None)
            if seat is None:
                break  # every candidate is confirmed taken; stop hammering
            run.selected_seat = seat
            try:
                message = client.submit_once(
                    request_values["room_id"], seat, request_values["start_time"], request_values["end_time"], target_day,
                    select_params=request_values["select_params"], select_path=request_values["select_context_path"], select_source=request_values["select_context_source"],
                    pre_resolved=pre_resolved,
                )
                pre_resolved = None  # only the opening attempt rides the pre-fetch
                _persist_discovered_context(plan, client)
                run.parameter_source = getattr(client, "last_parameter_source", "") or "unknown"
                _append_attempt(
                    run,
                    seat=seat,
                    source=run.parameter_source,
                    submitted=bool(getattr(client, "last_submitted", True)),
                    code=None,
                    message=message,
                )
                run.status = "SUCCESS"
                run.error_code = None
                run.message = redact(f"座位 {seat}：{message}")
                return run.id
            except ReservationError as exc:
                source = getattr(client, "last_parameter_source", "") or "unknown"
                _persist_discovered_context(plan, client)
                run.parameter_source = source
                _append_attempt(
                    run,
                    seat=seat,
                    source=source,
                    submitted=bool(getattr(client, "last_submitted", False)),
                    code=exc.code,
                    message=exc.message,
                )
                messages.append(f"第 {attempt + 1} 次，座位 {seat}：{exc.code} {exc.message}")
                if exc.code == "SEAT_UNAVAILABLE":
                    dead_seats.add(seat)
                if exc.code == "SUBMIT_OUTCOME_UNKNOWN":
                    # The POST may have landed; ask the platform instead of a human.
                    state, detail = _verify_duplicate_on_server(client, request_values, target_day)
                    if state == "found":
                        run.status = "SUCCESS"
                        run.error_code = None
                        run.message = redact(f"座位 {seat}：提交响应丢失，经超星端核实预约已生效（{detail}）")
                        return run.id
                    if detail:
                        messages.append(f"自动核实：{detail}")
                if exc.code in _RETRYABLE_CODES:
                    live_seats_remain = any(candidate not in dead_seats for candidate in seats)
                    if live_seats_remain:
                        if attempt + 1 < attempts:
                            if attempt + 1 < len(seats):
                                _wait_first_pass(deadline)
                            else:
                                _wait_between_attempts(deadline)
                        continue
                    break  # every candidate confirmed taken; stop hammering
                _set_failure(run, exc.code, "; ".join(messages), status=_failure_status(exc.code))
                return run.id
        _set_failure(run, "SEAT_UNAVAILABLE", "; ".join(messages) or "所有候选座位均不可预约")
        return run.id
    except ReservationError as exc:
        if run is None:
            raise
        _set_failure(run, exc.code, exc.message, status=_failure_status(exc.code))
        return run.id
    except Exception as exc:
        logger.exception("reservation run failed")
        if run is None:
            raise
        _set_failure(run, "UNEXPECTED_ERROR", str(exc))
        return run.id
    finally:
        if lock is not None and lock_acquired:
            lock.release()
        if run is not None:
            run.finished_at = datetime.now(dt.UTC).replace(tzinfo=None)
            db.commit()
            if run.status in _NOTIFY_STATUSES and run.trigger not in {"probe", "discover"}:
                title = f"抢座 {run.status}：{run.plan_name or run.plan_id or ''} {run.target_date or ''}".strip()
                body = f"账号 {run.account_name or '-'}；座位 {run.selected_seat or '、'.join(run.candidate_seats) or '-'}\n{run.message or ''}"
                notify_async(title, body)
        db.close()
