from __future__ import annotations

import logging
import datetime as dt
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .db import Account, ReservationPlan, ReservationRun, SessionLocal
from .service import enqueue_plan, _reservation_fingerprint
from .run_state import (ACTIVE_STATUSES, PREPARE_LEAD_SECONDS, SCHEDULED_TRIGGERS,
                        scheduled_opening, weekday_name, known_unsubmitted_browser_run)

logger = logging.getLogger(__name__)
# A short wake-up delay should not throw away an opening-window reservation, but
# we still refuse to replay stale work much later.
scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Shanghai"), job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 90})
STARTUP_CATCHUP_SECONDS = 90
# Scheduled plans wake up this many seconds before their run time so login and
# page warm-up finish before the platform's opening moment; the submit itself
# still waits for the (server-aligned) run time inside the run.
LEAD_SECONDS = PREPARE_LEAD_SECONDS
_catchup_status: dict[int, dict] = {}


def catchup_status(plan):
    item = _catchup_status.get(plan.id)
    if not item:
        return None
    current = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    opening = scheduled_opening(plan.run_time, current)
    fingerprint = _reservation_fingerprint(plan, opening.date() + dt.timedelta(days=plan.day_offset))
    if item["opening_at"] != opening.isoformat() or item["fingerprint"] != fingerprint:
        return None
    return {key: value for key, value in item.items() if key != "fingerprint"}


def _fire_time_of_day(run_time: str) -> tuple[int, int, int]:
    """Cron (hour, minute, second) that fires LEAD_SECONDS before run_time.

    Preparation may fall on the previous day; refresh_jobs shifts the cron
    weekday accordingly while the run snapshot retains the opening day.
    """
    hour, minute = map(int, run_time.split(":", 1))
    total = (hour * 3600 + minute * 60 - LEAD_SECONDS) % 86400
    return total // 3600, (total % 3600) // 60, total % 60


def refresh_jobs() -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("plan-"):
            scheduler.remove_job(job.id)
    db = SessionLocal()
    try:
        statement = select(ReservationPlan).join(Account).where(ReservationPlan.enabled.is_(True), Account.enabled.is_(True))
        for plan in db.scalars(statement):
            try:
                fire_hour, fire_minute, fire_second = _fire_time_of_day(plan.run_time)
            except ValueError:
                logger.warning("Skipping plan %s because its run time is invalid", plan.id)
                continue
            weekdays = ",".join(str(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].index(day)) for day in plan.weekdays if day in {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"})
            hour, minute = map(int, plan.run_time.split(':'))
            if hour * 3600 + minute * 60 < LEAD_SECONDS:
                weekdays = ','.join(str((int(day) - 1) % 7) for day in weekdays.split(',') if day)
            if not weekdays:
                continue
            scheduler.add_job(enqueue_plan, CronTrigger(day_of_week=weekdays, hour=fire_hour, minute=fire_minute, second=fire_second, timezone=ZoneInfo("Asia/Shanghai")), args=[plan.id, "scheduled"], id=f"plan-{plan.id}", replace_existing=True)
    finally:
        db.close()


def start_scheduler() -> None:
    if not scheduler.running:
        refresh_jobs()
        _enqueue_recently_missed_jobs()
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _enqueue_recently_missed_jobs(now: dt.datetime | None = None) -> int:
    """Queue one just-missed opening-window run after a service restart.

    APScheduler jobs are in memory. A new scheduler created seconds after the
    planned minute otherwise schedules the *next day* and silently loses today.
    An in-flight run in the durable run table prevents a duplicate catch-up.
    """
    current = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    db = SessionLocal()
    plan_ids: list[int] = []
    try:
        statement = select(ReservationPlan).join(Account).where(ReservationPlan.enabled.is_(True), Account.enabled.is_(True))
        for plan in db.scalars(statement):
            try:
                opening = scheduled_opening(plan.run_time, current, LEAD_SECONDS)
                scheduled_at = opening - dt.timedelta(seconds=LEAD_SECONDS)
            except ValueError:
                continue
            delay = (current - scheduled_at).total_seconds()
            if weekday_name(opening) not in plan.weekdays or not 0 <= delay <= LEAD_SECONDS + STARTUP_CATCHUP_SECONDS:
                continue
            target_date = (opening.date() + dt.timedelta(days=plan.day_offset)).isoformat()
            fingerprint = _reservation_fingerprint(
                plan, opening.date() + dt.timedelta(days=plan.day_offset))
            def decision(state, message, run_id=None):
                _catchup_status[plan.id] = {"state": state, "message": message,
                    "run_id": run_id, "opening_at": opening.isoformat(), "fingerprint": fingerprint}
                logger.info("Plan %s catch-up %s: %s", plan.id, state, message)
            # Only an IN-FLIGHT run (PENDING/RUNNING) or an already SUCCESSFUL
            # one for this plan+target-day counts as "already created".
            # Matching any historical row once swallowed this catch-up on
            # 2026-09-03: run_time had been moved later the same day, the
            # earlier attempt's FAILED row matched the (plan, target_date) key,
            # and the plan silently never ran while the UI kept promising
            # today's execution. FAILED and other terminal statuses mean the
            # window was lost — exactly what a catch-up retry is for; a real
            # double submit is still barred downstream by the
            # success-fingerprint check plus server-side verification.
            already_created = db.scalar(
                select(ReservationRun.id).where(
                    ReservationRun.plan_id == plan.id,
                    ReservationRun.target_date == target_date,
                    ReservationRun.request_fingerprint == fingerprint,
                    ReservationRun.trigger.in_(("scheduled", "scheduled_catchup")),
                    ReservationRun.status.in_((*ACTIVE_STATUSES, "SUCCESS")),
                ).limit(1)
            )
            if already_created:
                logger.info("Plan %s fire moment just passed but run %s is already in flight; no catch-up needed", plan.id, already_created)
                decision("existing", f"本次已有预约任务 #{already_created}", already_created)
                continue
            unresolved = db.scalar(select(ReservationRun.id).where(
                ReservationRun.account_id == plan.account_id,
                ReservationRun.target_date == target_date,
                ReservationRun.possibly_submitted.is_(True)).limit(1))
            if unresolved:
                decision("blocked", f"任务 #{unresolved} 可能已提交，尚未核实；本次补跑被阻止", unresolved)
                continue
            interrupted = next((old.id for old in db.scalars(select(ReservationRun).where(
                ReservationRun.account_id == plan.account_id,
                ReservationRun.target_date == target_date,
                ReservationRun.error_code == "INTERRUPTED_NEEDS_VERIFICATION"))
                if not known_unsubmitted_browser_run(old)), None)
            if interrupted:
                decision("blocked", f"历史任务 #{interrupted} 缺少安全重试证据；请先核实预约", interrupted)
                continue
            cancelled = next((old.id for old in db.scalars(select(ReservationRun).where(
                ReservationRun.plan_id == plan.id,
                ReservationRun.request_fingerprint == fingerprint,
                ReservationRun.trigger.in_(SCHEDULED_TRIGGERS),
                ReservationRun.error_code == "BROWSER_CANCELLED"))
                if old.request_snapshot.get("opening_at") == opening.isoformat()), None)
            if cancelled:
                decision("cancelled", f"本次任务 #{cancelled} 已主动取消，不会自动补跑", cancelled)
                continue
            decision("queued", "本次补跑正在入队")
            plan_ids.append(plan.id)
    finally:
        db.close()
    accepted = 0
    for plan_id in plan_ids:
        try:
            run_id = enqueue_plan(plan_id, "scheduled_catchup")
            accepted += 1
            _catchup_status[plan_id] = {**_catchup_status[plan_id], "run_id": run_id,
                                      "message": f"已创建补跑任务 #{run_id}"}
        except Exception:
            _catchup_status[plan_id] = {**_catchup_status[plan_id], "state": "failed",
                                      "message": "补跑入队失败，请查看服务日志"}
            logger.exception("Plan %s catch-up enqueue failed", plan_id)
    if accepted:
        logger.warning("Queued %s recently missed reservation job(s)", accepted)
    return accepted
