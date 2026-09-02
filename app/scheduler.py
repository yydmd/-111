from __future__ import annotations

import logging
import datetime as dt
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .db import Account, ReservationPlan, ReservationRun, SessionLocal
from .service import enqueue_plan

logger = logging.getLogger(__name__)
# A short wake-up delay should not throw away an opening-window reservation, but
# we still refuse to replay stale work much later.
scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Shanghai"), job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 15})
# A restart shortly after opening must be visible, but must not submit late.
STARTUP_MISSED_WINDOW_SECONDS = 90
# Scheduled plans wake up this many seconds before their run time so login and
# page warm-up finish before the platform's opening moment; the submit itself
# still waits for the (server-aligned) run time inside the run.
LEAD_SECONDS = 30


def _fire_time_of_day(run_time: str) -> tuple[int, int, int]:
    """Cron (hour, minute, second) that fires LEAD_SECONDS before run_time.

    The one edge case is a run time inside the first half minute after
    midnight: leading across the day boundary would shift the weekday, so we
    clamp to firing exactly at 00:00:00 there.
    """
    hour, minute = map(int, run_time.split(":", 1))
    total = max(0, hour * 3600 + minute * 60 - LEAD_SECONDS)
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
            if not weekdays:
                continue
            scheduler.add_job(enqueue_plan, CronTrigger(day_of_week=weekdays, hour=fire_hour, minute=fire_minute, second=fire_second, timezone=ZoneInfo("Asia/Shanghai")), args=[plan.id, "scheduled"], id=f"plan-{plan.id}", replace_existing=True)
    finally:
        db.close()


def start_scheduler() -> None:
    if not scheduler.running:
        refresh_jobs()
        _record_recently_missed_jobs()
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _record_recently_missed_jobs(now: dt.datetime | None = None) -> int:
    """Persist a missed opening window after restart without late submission."""
    current = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    db = SessionLocal()
    plans: list[ReservationPlan] = []
    try:
        statement = select(ReservationPlan).join(Account).where(ReservationPlan.enabled.is_(True), Account.enabled.is_(True))
        for plan in db.scalars(statement):
            if current.strftime("%A") not in plan.weekdays:
                continue
            try:
                fire_hour, fire_minute, fire_second = _fire_time_of_day(plan.run_time)
                scheduled_at = current.replace(hour=fire_hour, minute=fire_minute, second=fire_second, microsecond=0)
            except ValueError:
                continue
            delay = (current - scheduled_at).total_seconds()
            if not 0 < delay <= STARTUP_MISSED_WINDOW_SECONDS:
                continue
            target_date = (current.date() + dt.timedelta(days=plan.day_offset)).isoformat()
            already_created = db.scalar(
                select(ReservationRun.id).where(
                    ReservationRun.plan_id == plan.id,
                    ReservationRun.target_date == target_date,
                    ReservationRun.trigger.in_(("scheduled", "scheduled_catchup")),
                ).limit(1)
            )
            if not already_created:
                plans.append(plan)
        for plan in plans:
            db.add(ReservationRun(
                plan_id=plan.id, account_id=plan.account_id, plan_name=plan.name,
                account_name=plan.account.name, target_date=(current.date() + dt.timedelta(days=plan.day_offset)).isoformat(),
                trigger="scheduled", status="MISSED", stage="MISSED", error_code="MISSED_OPENING_WINDOW",
                message="服务在开放时间后启动；系统按安全策略未进行补抢", started_at=current.astimezone(dt.UTC).replace(tzinfo=None),
                finished_at=current.astimezone(dt.UTC).replace(tzinfo=None),
            ))
        db.commit()
    finally:
        db.close()
    if plans:
        logger.warning("Recorded %s missed opening-window job(s) after service startup", len(plans))
    return len(plans)


# Kept as a non-submitting compatibility alias for local integrations.
def _enqueue_recently_missed_jobs(now: dt.datetime | None = None) -> int:
    return _record_recently_missed_jobs(now)
