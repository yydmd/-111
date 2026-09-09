"""Derive rollout evidence from immutable run snapshots; never infer from API probes."""
from datetime import UTC, date, timedelta

from sqlalchemy import select

from .db import ReservationPlan, ReservationRun
from .run_state import ACTIVE_STATUSES


def matches_plan(plan, run):
    snapshot = run.request_snapshot
    expected = {
        "room_id": plan.room_id, "seats": [s.seat_num for s in plan.seats],
        "start_time": plan.start_time, "end_time": plan.end_time,
        "max_attempts": min(6, plan.max_attempts),
        "select_context_path": plan.select_context_path or None,
        "select_params": plan.select_params or None,
    }
    if run.plan_id != plan.id or run.account_id != plan.account_id:
        return False
    if any(snapshot.get(key) != value for key, value in expected.items()):
        return False
    offset = snapshot.get("day_offset")
    if offset is None:
        try:
            created = (run.started_at + timedelta(hours=8)).date()
            offset = (date.fromisoformat(run.target_date) - created).days
        except (ValueError, TypeError, AttributeError):
            return False
    return offset == plan.day_offset


def summarize(plan, runs):
    own = [r for r in runs if r.plan_id == plan.id and r.account_id == plan.account_id
           and r.request_snapshot.get("execution_mode") == "browser"]
    preview = next((r for r in own if r.trigger == "browser_preview"), None)
    trial = next((r for r in own if r.status == "SUCCESS" and matches_plan(plan, r)), None)
    account_success = next((r for r in runs if r.account_id == plan.account_id and r.status == "SUCCESS"
                            and r.request_snapshot.get("execution_mode") == "browser"), None)
    active = any(r.account_id == plan.account_id and r.status in ACTIVE_STATUSES for r in runs)
    unresolved = any(r.account_id == plan.account_id and r.possibly_submitted for r in runs)
    passed = bool(preview and matches_plan(plan, preview) and preview.error_code == "BROWSER_PREVIEW_READY"
                  and preview.status == "PROBE_DONE")
    if active:
        message = "账号任务进行中，结束后再操作"
    elif unresolved:
        message = "存在未核实的提交，请先只读核对"
    elif not preview:
        message = "尚未完成本计划的浏览器演练"
    elif not matches_plan(plan, preview):
        message = "计划参数已修改，需要重新演练"
    elif not passed:
        message = preview.message or "浏览器演练尚未通过"
    elif not trial:
        message = "本计划演练已通过，尚需正式试约并核对官方记录"
    else:
        message = "本计划演练和正式预约核对均已通过"
    def evidence(run):
        return None if run is None else {
            "run_id": run.id, "target_date": run.target_date,
            "status": run.status, "error_code": run.error_code,
            "message": run.message,
            "finished_at": run.finished_at.replace(tzinfo=UTC).isoformat() if run.finished_at else None,
        }
    return {"plan_id": plan.id, "message": message, "preview_passed": passed,
            "preview": evidence(preview), "formal": evidence(trial),
            "account_verified_run_id": account_success.id if account_success else None,
            "can_trial": passed and not active and not unresolved,
            "can_enable": passed and bool(trial) and not active and not unresolved}


def acceptance(db, plan=None):
    plans = [plan] if plan else list(db.scalars(select(ReservationPlan).order_by(ReservationPlan.id)))
    account_ids = {p.account_id for p in plans}
    runs = list(db.scalars(select(ReservationRun).where(
        ReservationRun.account_id.in_(account_ids)).order_by(ReservationRun.id.desc())))
    summaries = [summarize(p, runs) for p in plans]
    return summaries[0] if plan else summaries
