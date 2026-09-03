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
from .db import Account, AppSetting, ReservationPlan, ReservationRun, SessionLocal
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
# TOKEN_STALE (platform error 303) is an explicit "refresh and resubmit"
# rejection: the retry re-fetches the target-day page first — exactly what the
# platform asks for — and the request was already refused, so retrying
# carries no double-submit risk. HTTP_REDIRECT covers 30x hops the classifier
# does not recognise (frequently a benign session/page jump, not risk
# control); RATE_LIMITED is throttling ("操作频繁") — both retry after a
# back-off instead of a fatal verdict.
_RETRYABLE_CODES = {"SEAT_UNAVAILABLE", "TARGET_CONTEXT_UNAVAILABLE", "TARGET_DAY_NOT_OPEN", "TOKEN_STALE", "HTTP_REDIRECT", "RATE_LIMITED"}
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
# 2026-09-03 speed round: the opening switch is the platform's own busy
# moment and every observed volley already retried through it; 0.3–0.6s
# serial spacing only ceded the window. 0.2–0.45s keeps the jitter (no
# fingerprintable cadence) while halving the worst-case recovery gap.
RETRY_WAIT_RANGE = (0.2, 0.45)
# A cached opening page older than this at the fire moment gets one more
# fetch before it is submitted: a page that aged through failed re-fetches is
# exactly what the platform rejects with error 303 (TOKEN_STALE).
STALE_PAGE_TOLERANCE_SECONDS = 1.0
# 2026-09-03 race hardening. A 303-rejected racer heals itself with a fresh
# page fetch + resubmit (the platform's own 303 remedy) instead of falling
# back to the seconds-slower serial path; heals are capped so a persistently
# stale seat still hands over to the serial rotation quickly.
RACER_HEAL_ATTEMPTS = 2
# Racers fire staggered by priority order — the same account bursting N
# identical POSTs within one millisecond is the anti-burst pattern both
# 2026-09-03 volleys were wholly rejected with (3×303 + 1 occupied).
RACER_STAGGER_SECONDS = 0.05
# Per-request timeouts are floored at 1s, so a racer's POST may still be on
# the wire when the run deadline hits; a bounded grace lets it land and be
# audited (or server-verified) instead of silently dropped.
LATE_SUBMIT_GRACE_SECONDS = 2.5
# Server verification after the deadline needs a live session.
VERIFY_BUDGET_SECONDS = 8.0
# Fire-moment auto-calibration. Run #235 telemetry showed the platform
# answers 303 to EVERYTHING for the first ~1.5s after the configured moment —
# the true accept moment sits later than run_time. Each scheduled run records
# when its first submission got a real answer; an EMA of those offsets shifts
# later fires onto the accept moment (minus a small safety lead), so the
# parent shot lands instead of 303-ing into a still-switching door.
_CALIBRATION_SETTING_KEY = "fire_accept_calibration"
_CALIBRATION_ALPHA = 0.5
_CALIBRATION_MAX_SECONDS = 5.0
_CALIBRATION_SAFETY_LEAD = 0.2
# Codes that mean "the platform evaluated this booking" (a real answer, not
# the switch-window 303 nor a transport/page failure).
_ACCEPT_SIGNAL_CODES = {None, "SEAT_UNAVAILABLE", "SUBMIT_REJECTED", "RATE_LIMITED", "HTTP_REDIRECT"}
# How many candidate seats submit simultaneously at the opening moment. The
# web UI caps a plan's candidate pool at the same number, so a full pool can
# be covered by one all-at-once volley.
PARALLEL_SEAT_LIMIT = 6
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


def _append_attempt(run: ReservationRun, *, seat: str, source: str, submitted: bool, code: str | None, message: str, timing: dict | None = None) -> None:
    """Persist a small, secret-free account of what actually happened.

    ``timing`` carries race telemetry (page/submit/token-age milliseconds,
    heal counts) so real-world volley behaviour can be analysed from the run
    record instead of guesswork.
    """
    details = run.attempt_details
    entry = {
        "seat": seat,
        "source": source or "unknown",
        "submitted": bool(submitted),
        "code": code,
        "message": redact(message),
    }
    if timing:
        entry["timing"] = {key: (round(value, 1) if isinstance(value, float) else value) for key, value in timing.items()}
    details.append(entry)
    run.attempt_details_json = json.dumps(details, ensure_ascii=False)


def _persist_discovered_context(plan: ReservationPlan, client: ChaoxingClient) -> None:
    params = getattr(client, "last_discovered_select_params", None)
    source = getattr(client, "last_parameter_source", "")
    path = getattr(client, "last_discovered_select_path", None)
    # A manually pasted link remains an explicit override.  Automatic context
    # discovery, however, must be allowed to refresh an older stored automatic
    # path/parameter set (including the historic absolute-URL bug).
    automatic_sources = {"auto_context", "room_context", "stored_auto_context"}
    stored_source = (plan.select_context_source or "").strip()
    manual_override = bool(plan.select_params) and stored_source not in automatic_sources
    if params and not manual_override and (not plan.select_params or source in automatic_sources):
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


def _reconcile_parallel_successes(client: ChaoxingClient, request_values: dict, target_day: dt.date, success_seats: list[str]) -> str:
    """Describe what the platform actually holds after a multi-success volley.

    Parallel racers submit simultaneously; the stop flag can only take effect
    for racers that had not reached their POST yet, so several candidates may
    have succeeded at once. The run keeps the first winner, but the record
    must tell the user which seats the platform really holds — especially
    stray duplicates that need manual cancellation.
    """
    try:
        reservations = client.fetch_reservations(request_values.get("select_params"), request_values.get("select_context_path"))
        overlapping = _chaoxing_client.ChaoxingClient.find_overlapping_reservations(
            reservations, target_day, request_values["room_id"], request_values["start_time"], request_values["end_time"]
        )
    except Exception:  # reconciliation must never break the success path
        return "未能核实超星端实际持有的预约，请到超星端检查是否有重复预约"

    def norm_seat(value) -> str:
        raw = str(value or "").strip()
        return raw.zfill(3) if raw.isdigit() else raw

    held = {norm_seat(item.get("seatNum")) for item in overlapping}
    strays = [seat for seat in success_seats if norm_seat(seat) in held]
    if strays:
        held_display = "、".join(sorted(held)) if held else "、".join(strays)
        return f"经核实超星端同时持有 {held_display} 多个座位，请手动取消多余的预约"
    if held:
        return "经核实超星端仅保留一个本场次预约"
    return "经核实超星端当前没有本场次预约，请到超星端确认"


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


def _load_fire_calibration(db) -> tuple[float, int]:
    """(ema_offset_seconds, sample_count); (0.0, 0) when never measured."""
    try:
        row = db.get(AppSetting, _CALIBRATION_SETTING_KEY) if db is not None else None
        data = json.loads(row.value) if row and row.value else {}
        return min(max(float(data.get("ema", 0.0)), 0.0), _CALIBRATION_MAX_SECONDS), int(data.get("samples", 0))
    except Exception:
        return 0.0, 0


def _record_accept_offset(db, offset: float) -> float | None:
    """Fold one observed fire→accept offset into the stored EMA.

    Positive offsets (the platform kept answering 303 after our fire) push the
    next fire later. An accept at-or-before our fire (offset <= 0 — e.g. the
    platform switched to instant acceptance) pulls the EMA back DOWN, so a
    stale offset can never keep later runs firing late forever; the 0.2s
    safety lead keeps small noise inside a deadband. Absurd magnitudes are
    rejected either way, and a calibration failure must never break the run.
    """
    if not -_CALIBRATION_MAX_SECONDS <= offset <= _CALIBRATION_MAX_SECONDS * 2:
        return None
    ema, samples = _load_fire_calibration(db)
    ema = offset if samples == 0 else ema + _CALIBRATION_ALPHA * (offset - ema)
    ema = min(max(ema, 0.0), _CALIBRATION_MAX_SECONDS)
    row = db.get(AppSetting, _CALIBRATION_SETTING_KEY)
    payload = json.dumps({"ema": round(ema, 3), "samples": samples + 1})
    if row is None:
        db.add(AppSetting(key=_CALIBRATION_SETTING_KEY, value=payload))
    else:
        row.value = payload
    db.commit()
    return ema


def _scheduled_fire_epoch(plan: ReservationPlan, db=None) -> float | None:
    """Server-clock epoch when a scheduled run may submit.

    The plan's run time is treated as the platform's wall-clock opening moment;
    a dense clock calibration runs right before the decisive wait (see
    ``clock.refresh(dense=True)``). A small random jitter keeps every day's
    firing time from being bit-identical. Runs that already missed the moment
    fire immediately instead of waiting a day. Past runs' measured
    accept-moment offsets (see ``_record_accept_offset``) additionally shift
    the fire onto the platform's true accept moment; with no samples yet the
    behaviour is exactly the uncalibrated race.
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
    base = target_epoch if target_epoch > now_epoch else now_epoch
    owned_db = None
    if db is None:
        owned_db = SessionLocal()
        db = owned_db
    try:
        ema, samples = _load_fire_calibration(db)
    finally:
        if owned_db is not None:
            owned_db.close()
    if ema > 0:
        shift = max(0.0, ema - _CALIBRATION_SAFETY_LEAD)
        base += shift
        logger.info("fire calibration applied: +%.2fs (EMA of %d run(s))", shift, samples)
    return base


def _hold_until_fire(fire_epoch: float) -> None:
    """Busy-wait the last sliver so the submit lands just past the moment."""
    while True:
        remaining = fire_epoch - app_clock.server_now()
        if remaining <= 0:
            break
        time.sleep(min(remaining, FIRE_BUSY_STEP))


def _poll_until_open(client: ChaoxingClient, fire_epoch: float, request_values: dict, target_day: dt.date, deadline: float) -> ProbeResult | None:
    """Re-fetch the target-day select page through the opening moment.

    Before the window opens the platform answers a perfectly valid request
    with its not-open error page, so one early fetch is worthless: the race is
    decided by whoever re-fetches within milliseconds of the switch. Poll with
    one cheap GET, relaxed early and dense only around the decisive moment.
    When the page is already usable seconds before the planned moment (a plan
    tested outside the rush, or a school that opens early), keep refreshing at
    the relaxed cadence — like a waiting human reloading the page — so the
    parameters handed to the submit are fresh at the fire moment instead of
    aging on the wire into a platform error 303 (TOKEN_STALE) rejection.
    Returns the freshest usable page, or None when the window never opened
    within the grace period (the serial submit path then re-resolves on its
    own). Raises DEADLINE_EXCEEDED at ``deadline`` so a clamp-woken run (see
    scheduler._fire_time_of_day) cannot spin for half an hour holding the
    account lock.
    """
    # Wait quietly until the prefetch lead window begins.
    while True:
        remaining = fire_epoch - app_clock.server_now()
        if remaining <= PREFETCH_LEAD_SECONDS:
            break
        time.sleep(min(remaining - PREFETCH_LEAD_SECONDS, 0.2))
    context = request_values["select_params"] or {"id": request_values["room_id"].strip()}
    select_path = request_values["select_context_path"]
    latest: ProbeResult | None = None
    latest_at = 0.0
    while app_clock.server_now() <= fire_epoch + OPENING_GRACE_SECONDS:
        if time.monotonic() >= deadline:
            raise ReservationError("DEADLINE_EXCEEDED", "任务超过 60 秒运行上限")
        remaining = fire_epoch - app_clock.server_now()
        if latest is not None and remaining <= 0:
            if time.monotonic() - latest_at <= STALE_PAGE_TOLERANCE_SECONDS:
                # Fire moment reached with a fresh page in hand; the caller's
                # own hold gate is the only remaining wait.
                return latest
            # The cached page aged (recent re-fetches failed); one more fetch
            # right now is exactly the platform's own 303 remedy. Only fall
            # back to the aged page when this last fetch also fails.
            try:
                fresh = client.fetch_target_day_page(target_day, dict(context), select_path)
            except ReservationError:
                fresh = None
            if fresh is not None and (fresh.ok or fresh.captcha_type in CAPTCHA_TYPES):
                return fresh
            return latest
        try:
            result = client.fetch_target_day_page(target_day, dict(context), select_path)
        except ReservationError:
            result = None
        if result is not None and (result.ok or result.captcha_type in CAPTCHA_TYPES):
            latest = result
            latest_at = time.monotonic()
            if remaining > DENSE_WINDOW_BEFORE_FIRE:
                time.sleep(OPENING_POLL_RELAXED)
                continue
            return result
        interval = OPENING_POLL_DENSE if remaining <= DENSE_WINDOW_BEFORE_FIRE else OPENING_POLL_RELAXED
        time.sleep(interval)
    return latest


def _await_fire_and_prefetch(
    client: ChaoxingClient,
    fire_epoch: float,
    request_values: dict,
    target_day: dt.date,
    seat: str,
    deadline: float,
):
    """Block until the window opens, then hand the caller a fresh page.

    Returns the resolved ProbeResult for the first submit, or None when the
    window did not open in time — the submit path then resolves itself.
    """
    pre = _poll_until_open(client, fire_epoch, request_values, target_day, deadline)
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

    Racers are cloned *before* the window and each warms its own keep-alive
    connection with one cheap GET on the exact decisive URL, so the opening
    moment pays no DNS/TCP/TLS handshake. When the poller detects the switch
    it passes the verified select context to every racer, which then runs the
    shortest possible path: one redirect-following GET on that context plus
    the submit POST — no fallback chain, no context discovery. First success
    stops the rest. Returns ``(outcomes, winner)`` where outcomes audit every
    racer and winner is the successful outcome dict, or ``([], None)`` when
    the window never opened and the serial fallback must take over.
    """
    room_id = request_values["room_id"]
    start_time = request_values["start_time"]
    end_time = request_values["end_time"]
    select_params = request_values["select_params"]
    select_path = request_values["select_context_path"]
    select_source = request_values["select_context_source"]
    workers = seats[:PARALLEL_SEAT_LIMIT]
    verified_context = dict(select_params or {"id": room_id.strip()})
    opened = threading.Event()

    outcomes: list[dict] = []
    winner: dict | None = None
    guard = threading.Lock()
    stop = threading.Event()
    # Seats whose POST is on the wire right now (guarded by ``guard``); the
    # post-deadline grace uses this to audit/verify stragglers.
    submitting: set[str] = set()
    # Seats the parent shot already confirmed occupied (guarded by ``guard``);
    # the seat's own racer checks this on wake-up so a dead seat never gets
    # the extra staggered shot.
    volley_dead: set[str] = set()

    def race(seat: str, racer: ChaoxingClient, index: int) -> None:
        nonlocal winner
        # 1) Arrive at the lead window, then warm DNS/TCP/TLS with one cheap
        #    GET on the exact decisive URL — late enough that the pooled
        #    connection is still hot milliseconds later at the switch.
        while True:
            remaining = fire_epoch - app_clock.server_now()
            if remaining <= PREFETCH_LEAD_SECONDS:
                break
            time.sleep(min(remaining - PREFETCH_LEAD_SECONDS, 0.2))
        try:
            racer.fetch_target_day_page(target_day, dict(verified_context), select_path)
        except Exception:
            pass  # warm-up must never break the race itself
        if not opened.wait(timeout=max(0.0, deadline - time.monotonic())):
            return  # poller never saw the window; the serial fallback owns this
        if stop.is_set():
            return
        with guard:
            seat_already_dead = seat in volley_dead
        if seat_already_dead:
            return  # the parent shot already confirmed this seat occupied
        # Align with the platform's clock FIRST, resolve the page second. In
        # the true-race case the window only opens at/after the moment, so
        # this is timing-identical. But when the window is already open (a
        # plan tested outside the rush, or a school that opens early) the
        # poller wakes us ~PREFETCH_LEAD_SECONDS early, and a token fetched
        # that far before the submit is exactly what ChaoXing rejects with
        # error 303 (页面停留过久/安全验证已超时).
        # Racers then stagger by priority order: the same account firing N
        # identical POSTs within one millisecond is the anti-burst pattern
        # both 2026-09-03 volleys were wholly rejected with.
        _hold_until_fire(fire_epoch + index * RACER_STAGGER_SECONDS)
        if stop.is_set():
            return
        heals_left = RACER_HEAL_ATTEMPTS
        while True:
            # 2) Shortest path: one GET on the poller-verified context. Only an
            #    unusable answer falls back to the full resolver chain. The
            #    FINAL heal inverts the order: the full chain is the only path
            #    that ever landed submissions in the field (4/4 successes),
            #    so when the cheap retries are spent the racer goes heavy.
            resolve_started = time.monotonic()
            resolved = None
            if heals_left == 0:
                try:
                    resolved = racer.resolve_submission_page(
                        room_id, seat, target_day,
                        select_params=select_params, select_path=select_path, select_source=select_source,
                    )
                except Exception:
                    resolved = None  # any surprise falls through to the cheaper paths
            if resolved is None:
                try:
                    fast = racer.fetch_target_day_page(target_day, dict(verified_context), select_path)
                    if fast is not None and (fast.ok or fast.captcha_type in CAPTCHA_TYPES):
                        resolved = fast
                except Exception:
                    resolved = None  # any surprise falls back to the full resolver
            if resolved is None:
                try:
                    resolved = racer.resolve_submission_page(
                        room_id, seat, target_day,
                        select_params=select_params, select_path=select_path, select_source=select_source,
                    )
                except Exception:
                    # ReservationError is the expected refusal; anything else
                    # must still fall through instead of silently killing the
                    # racer (its seat would vanish from the audit).
                    resolved = None
            if resolved is None:
                # Last resort: the poller's page may still carry a usable token.
                page = pre_holder.get("page")
                if page is None or not page.ok or page.captcha_type in CAPTCHA_TYPES:
                    return
                resolved = page
            if stop.is_set():
                return
            resolved_at = time.monotonic()
            timing = {"page_ms": round((resolved_at - resolve_started) * 1000, 1)}
            heals_used = RACER_HEAL_ATTEMPTS - heals_left
            if heals_used:
                timing["heals"] = heals_used
            submit_started = time.monotonic()
            timing["at"] = round(app_clock.server_now(), 3)
            with guard:
                submitting.add(seat)
            try:
                message = racer.submit_once(
                    room_id, seat, start_time, end_time, target_day,
                    select_params=select_params, select_path=select_path, select_source=select_source,
                    pre_resolved=resolved,
                )
            except ReservationError as exc:
                timing["submit_ms"] = round((time.monotonic() - submit_started) * 1000, 1)
                timing["token_age_ms"] = round((submit_started - resolved_at) * 1000, 1)
                with guard:
                    submitting.discard(seat)
                    outcomes.append({
                        "seat": seat, "code": exc.code, "message": exc.message,
                        "submitted": bool(getattr(racer, "last_submitted", False)),
                        "source": getattr(racer, "last_parameter_source", "") or "unknown",
                        "timing": timing,
                    })
                if exc.code == "TOKEN_STALE" and heals_left > 0 and not stop.is_set():
                    # 303 self-heal: the platform definitively refused this
                    # token and itself asked for a refresh + resubmit, so a
                    # retry carries no double-submit risk. Healing here turns
                    # a seconds-long serial recovery into ~one round trip.
                    # Every other code keeps the one-shot-per-racer behaviour.
                    heals_left -= 1
                    continue
                return
            timing["submit_ms"] = round((time.monotonic() - submit_started) * 1000, 1)
            timing["token_age_ms"] = round((submit_started - resolved_at) * 1000, 1)
            with guard:
                submitting.discard(seat)
                outcomes.append({"seat": seat, "code": None, "message": message, "submitted": True,
                                 "source": getattr(racer, "last_parameter_source", "") or "unknown",
                                 "timing": timing})
                if winner is None:
                    winner = outcomes[-1]
                    stop.set()
            return

    # Referenced by race() as a last-resort page; defined before the threads
    # start so no scheduling surprise can race the assignment.
    pre_holder: dict = {"page": None}
    threads = [
        # Stagger slot 0 is reserved for the parent shot below; racers follow.
        threading.Thread(target=race, args=(seat, client.clone_authenticated(), index + 1), name=f"opening-{seat}")
        for index, seat in enumerate(workers)
    ]
    for thread in threads:
        thread.start()

    # 3) The single dense poller watches for the window; racers hold until it
    # fires (or the window never opens and the serial fallback takes over).
    pre = None
    try:
        pre = _poll_until_open(client, fire_epoch, request_values, target_day, deadline)
    finally:
        # Whatever the poller did — no window, or a raised deadline — the
        # racers must wake with the stop flag ALREADY set so none slips past
        # the stop check into a submit that outlives the account lock. When
        # the window did open, the wake-up is deferred until after the parent
        # shot below: the parent fires strictly first and the racers only
        # engage when it did not win.
        if pre is not None:
            pre_holder["page"] = pre
        else:
            stop.set()
            opened.set()
    if pre is None:
        return [], None
    logger.info(
        "opening window detected: server_epoch=%.3f fire=%.3f delta_ms=%.0f",
        app_clock.server_now(), fire_epoch, (app_clock.server_now() - fire_epoch) * 1000,
    )
    try:
        # 4) Parent shot: every field success so far (4/4) came from the
        # parent session's own page chain while the racers' shortcut drew
        # 303, so the parent fires FIRST — riding the poller's freshest page
        # of its own session (zero extra fetch, ~1 RTT after the opening
        # moment). Its outcome flows through the same outcomes list:
        # TOKEN_STALE stays budget-free, an occupied seat dies, first success
        # stops everything.
        pre_taken_at = time.monotonic()
        _hold_until_fire(fire_epoch)
        parent_timing = {
            "at": round(app_clock.server_now(), 3),
            "page_ms": 0.0,
            "token_age_ms": round((time.monotonic() - pre_taken_at) * 1000, 1),
        }
        parent_started = time.monotonic()
        try:
            message = client.submit_once(
                room_id, workers[0], start_time, end_time, target_day,
                select_params=select_params, select_path=select_path, select_source=select_source,
                pre_resolved=pre,
            )
        except ReservationError as exc:
            parent_timing["submit_ms"] = round((time.monotonic() - parent_started) * 1000, 1)
            outcomes.append({
                "seat": workers[0], "code": exc.code, "message": exc.message,
                "submitted": bool(getattr(client, "last_submitted", False)),
                "source": getattr(client, "last_parameter_source", "") or "unknown",
                "timing": parent_timing,
            })
            if exc.code == "SEAT_UNAVAILABLE":
                with guard:
                    volley_dead.add(workers[0])  # the seat's racer must not add a staggered shot
        else:
            parent_timing["submit_ms"] = round((time.monotonic() - parent_started) * 1000, 1)
            outcomes.append({"seat": workers[0], "code": None, "message": message, "submitted": True,
                             "source": getattr(client, "last_parameter_source", "") or "unknown",
                             "timing": parent_timing})
            winner = outcomes[-1]
            stop.set()
    except BaseException:
        # An unexpected parent-shot failure must stop the racers BEFORE they
        # wake: this exception unwinds through execute_plan, which releases
        # the account lock — no submit may outlive that (stop-first contract).
        stop.set()
        raise
    finally:
        opened.set()
    if winner is not None:
        for thread in threads:
            thread.join(timeout=max(0.0, deadline + LATE_SUBMIT_GRACE_SECONDS - time.monotonic()))
        return outcomes, winner
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    # A racer's POST may still be on the wire past the deadline (per-request
    # timeouts are floored at 1s). A bounded grace lets it land and be audited
    # instead of silently dropping a possibly-successful reservation.
    grace_deadline = deadline + LATE_SUBMIT_GRACE_SECONDS
    for thread in threads:
        if thread.is_alive():
            thread.join(timeout=max(0.0, grace_deadline - time.monotonic()))
    if outcomes:
        logger.info(
            "volley outcomes: %s",
            "; ".join(f"{item['seat']}={item['code'] or 'SUCCESS'}" for item in outcomes),
        )
    # No winner: nothing new may leave the wire after this point. A racer
    # still mid-resolve (not yet in `submitting`) must not fire alongside —
    # or after — the serial fallback, whose account lock must never shelter
    # an unaccounted submit.
    stop.set()
    with guard:
        stuck = sorted(submitting)
        for seat in stuck:
            outcomes.append({
                "seat": seat, "code": "SUBMIT_OUTCOME_UNKNOWN",
                "message": "提交已发出但未在运行时限内返回；等待超星端核实",
                "submitted": True, "source": "unknown",
            })
    if stuck:
        # A POST left the wire but never came back within the grace: the
        # caller's server-verification branch decides — found becomes
        # SUCCESS, unverifiable becomes NEEDS_VERIFICATION. Verification needs
        # live HTTP and the run deadline has passed; the serial fallback
        # checks its own local deadline variable, so this extension cannot
        # buy any extra submit.
        client.deadline = time.monotonic() + VERIFY_BUDGET_SECONDS
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
    fire_epoch: float | None = None
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
                        message = f"目标日 {target_day.isoformat()}：参数就绪（来源：{source}），可进入提交；探测为只读，不检查时段是否已被占用"
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
            ema, samples = _load_fire_calibration(db)
            if ema > 0:
                messages.append(f"开火校准：按最近 {samples} 次实测把开抢时刻后移 {max(0.0, ema - _CALIBRATION_SAFETY_LEAD):.1f} 秒")
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
                            timing=outcome.get("timing"),
                        )
                        messages.append(f"开抢并行，座位 {outcome['seat']}：{outcome['code'] or 'SUCCESS'} {outcome['message']}")
                    if winner is not None:
                        _persist_discovered_context(plan, client)
                        run.selected_seat = winner["seat"]
                        run.parameter_source = winner["source"]
                        run.status = "SUCCESS"
                        run.error_code = None
                        summary = f"座位 {winner['seat']}：{winner['message']}"
                        extra_successes = sorted({outcome["seat"] for outcome in parallel_outcomes if outcome["code"] is None and outcome["seat"] != winner["seat"]})
                        if extra_successes:
                            # The volley is simultaneous by design: the stop flag
                            # only reaches racers that had not POSTed yet, so a
                            # second seat may have succeeded too. Verify what the
                            # platform really holds instead of hiding it.
                            summary += f"；并行提交的座位 {'、'.join(extra_successes)} 同样返回成功，{_reconcile_parallel_successes(client, request_values, target_day, extra_successes)}"
                        run.message = redact(summary)
                        return run.id
                    unknown = next((outcome for outcome in parallel_outcomes if outcome["code"] == "SUBMIT_OUTCOME_UNKNOWN"), None)
                    if unknown is not None:
                        # A racer's POST may have reached the platform even
                        # though its response was lost. Mirror the serial
                        # path: verify on the server, then stop — the serial
                        # fallback must never re-submit this slot.
                        state, detail = _verify_duplicate_on_server(client, request_values, target_day)
                        if state == "found":
                            _persist_discovered_context(plan, client)
                            run.selected_seat = unknown["seat"]
                            run.parameter_source = unknown["source"]
                            run.status = "SUCCESS"
                            run.error_code = None
                            run.message = redact(f"座位 {unknown['seat']}：提交响应丢失，经超星端核实预约已生效（{detail}）")
                            return run.id
                        if detail:
                            messages.append(f"自动核实：{detail}")
                        _set_failure(run, "SUBMIT_OUTCOME_UNKNOWN", "; ".join(messages), status="NEEDS_VERIFICATION")
                        return run.id
                    fatal = next((outcome["code"] for outcome in reversed(parallel_outcomes) if outcome["code"] and outcome["code"] not in _RETRYABLE_CODES), None)
                    if fatal is not None:
                        # A racer was rejected for a non-retryable reason (risk
                        # control, captcha, login, deadline). The serial path
                        # stops immediately on such codes; the volley's verdict
                        # gets the same respect — no more submissions.
                        _set_failure(run, fatal, "; ".join(messages), status=_failure_status(fatal))
                        return run.id
                else:
                    pre_resolved = _await_fire_and_prefetch(client, fire_epoch, request_values, target_day, seats[0], deadline)
        attempts = min(max(plan.max_attempts, len(seats)), MAX_SUBMIT_ATTEMPTS)
        if plan.max_attempts > MAX_SUBMIT_ATTEMPTS:
            messages.append(f"按安全策略将尝试次数从 {plan.max_attempts} 钳制为 {MAX_SUBMIT_ATTEMPTS}")
        # A confirmed-unavailable seat is dead: never spend another submit on
        # it. The parallel opening shot may already have consumed candidates.
        dead_seats = {outcome["seat"] for outcome in parallel_outcomes if outcome["code"] == "SEAT_UNAVAILABLE"}
        # A TOKEN_STALE rejection (platform error 303) booked nothing: the
        # platform explicitly refused the request and asked for a refresh and
        # resubmit. Counting such a rejection as a spent attempt made a fully
        # stale volley (every racer got 303) exhaust the whole budget —
        # range(used_attempts, attempts) came back empty, no refresh-retry
        # ever ran, and the run died reporting the last racer's code (e.g.
        # SEAT_UNAVAILABLE) even though live seats remained retryable.
        used_attempts = sum(1 for outcome in parallel_outcomes if outcome["submitted"] and outcome["code"] != "TOKEN_STALE")
        # Remember the most recent failure code so an exhausted budget is
        # reported with its real cause instead of a blanket "seat taken".
        last_code: str | None = next((outcome["code"] for outcome in reversed(parallel_outcomes) if outcome["code"]), None)
        # Refresh-retry rotation: after a whole-volley rejection (e.g. every
        # racer got 303) the budget buys one shot per DISTINCT live seat — it
        # must not be spent hammering the first candidate four times while
        # 098/099 never see a refresh retry. Once every live seat had its turn
        # this round, the order starts over.
        round_tried: set[str] = set()
        for attempt in range(used_attempts, attempts):
            if time.monotonic() >= deadline:
                raise ReservationError("DEADLINE_EXCEEDED", "任务超过 60 秒运行上限")
            seat = next((candidate for candidate in seats if candidate not in dead_seats and candidate not in round_tried), None)
            if seat is None:
                round_tried.clear()
                seat = next((candidate for candidate in seats if candidate not in dead_seats), None)
            if seat is None:
                break  # every candidate is confirmed taken; stop hammering
            round_tried.add(seat)
            run.selected_seat = seat
            # The pre-fetch rides ONLY the first submit. Consume it before
            # the call so a rejected attempt (a 303 told us its token died)
            # can never re-submit the same stale page on the retry.
            pending_pre = pre_resolved
            pre_resolved = None
            attempt_at = app_clock.server_now()
            try:
                message = client.submit_once(
                    request_values["room_id"], seat, request_values["start_time"], request_values["end_time"], target_day,
                    select_params=request_values["select_params"], select_path=request_values["select_context_path"], select_source=request_values["select_context_source"],
                    pre_resolved=pending_pre,
                )
                _persist_discovered_context(plan, client)
                run.parameter_source = getattr(client, "last_parameter_source", "") or "unknown"
                _append_attempt(
                    run,
                    seat=seat,
                    source=run.parameter_source,
                    submitted=bool(getattr(client, "last_submitted", True)),
                    code=None,
                    message=message,
                    timing={"at": round(attempt_at, 3)},
                )
                run.status = "SUCCESS"
                run.error_code = None
                run.message = redact(f"座位 {seat}：{message}")
                return run.id
            except ReservationError as exc:
                last_code = exc.code
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
                    timing={"at": round(attempt_at, 3)},
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
        final_code = last_code or "SEAT_UNAVAILABLE"
        _set_failure(run, final_code, "; ".join(messages) or "所有候选座位均不可预约", status=_failure_status(final_code))
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
            if fire_epoch is not None and not probe_only:
                # Fold "when did the platform first give a real answer" into
                # the fire calibration (see problems.txt). Only VOLLEY-phase
                # attempts (parent shot / racers, marked by page_ms) count:
                # serial attempts happen after our own back-off waits and
                # would feed the EMA a positive feedback loop (every run
                # later than the last). Wrapped so a calibration hiccup can
                # never break the run's own result.
                try:
                    accept_at = min(
                        (
                            item["timing"]["at"]
                            for item in run.attempt_details
                            if isinstance(item.get("timing"), dict)
                            and "page_ms" in item["timing"]
                            and item.get("submitted")
                            and item.get("code") in _ACCEPT_SIGNAL_CODES
                        ),
                        default=None,
                    )
                    if accept_at is not None:
                        _record_accept_offset(db, accept_at - fire_epoch)
                except Exception:
                    logger.debug("fire calibration update failed", exc_info=True)
            db.commit()
            if run.status in _NOTIFY_STATUSES and run.trigger not in {"probe", "discover"}:
                title = f"抢座 {run.status}：{run.plan_name or run.plan_id or ''} {run.target_date or ''}".strip()
                body = f"账号 {run.account_name or '-'}；座位 {run.selected_seat or '、'.join(run.candidate_seats) or '-'}\n{run.message or ''}"
                notify_async(title, body)
        db.close()
