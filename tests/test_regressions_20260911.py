"""Incident regressions. Isolated SQLite, fake transports; no live bookings."""
import datetime as dt
import json
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import browser_reserve as br, browser_sessions, clock, notify, scheduler as scheduling, security, service, web
from app.db import Account, AppSetting, Base, PlanSeat, ReservationPlan, ReservationRun
from app.run_state import scheduled_opening, weekday_name

TZ = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 9, 11, 7, 29, tzinfo=TZ)


@pytest.fixture
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'regression.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    for module in (service, scheduling, notify):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(service, "now_shanghai", lambda: NOW)
    monkeypatch.setattr(service, "notify_async", lambda *a: None)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: NOW.timestamp())
    monkeypatch.setattr(browser_sessions, "submit", lambda *a, **kw: None)
    monkeypatch.setattr(scheduling, "_catchup_status", {})
    with sessions() as db:
        account = Account(name="test", username="fixture", password_blob=b"unused", enabled=True)
        plan = ReservationPlan(account=account, name="07:30 test", room_id="10713", execution_mode="browser",
            start_time="21:30", end_time="22:00", run_time="07:30", day_offset=0,
            weekdays_json='["Friday"]', enabled=True)
        plan.seats = [PlanSeat(seat_num="097", priority=0)]
        db.add(plan)
        db.commit()
    yield sessions
    engine.dispose()


def old_run(db, *, submitted=False, attempts=None, mode="browser"):
    old = service._run_snapshot(db.get(ReservationPlan, 1), "scheduled")
    old.status, old.error_code = "NEEDS_VERIFICATION", "INTERRUPTED_NEEDS_VERIFICATION"
    old.possibly_submitted = submitted
    old.request_snapshot_json = json.dumps({"execution_mode": mode})
    old.attempt_details_json = json.dumps(attempts or [])
    db.add(old)
    db.commit()
    return old.id


def test_0730_catchup_survives_legacy_false_interruption_and_active_preview(factory):
    with factory() as db:
        old_id = old_run(db)
        preview = service._run_snapshot(db.get(ReservationPlan, 1), "browser_preview")
        db.add(preview)
        db.commit()
        preview_id = preview.id
    assert scheduling._enqueue_recently_missed_jobs(NOW) == 1
    assert scheduling._enqueue_recently_missed_jobs(NOW) == 0
    with factory() as db:
        queued = db.scalar(select(ReservationRun).where(ReservationRun.trigger == "scheduled_catchup"))
        assert queued.id not in (old_id, preview_id)
        assert queued.status == "PENDING"
        assert queued.request_snapshot["opening_at"] == "2026-09-11T07:30:00+08:00"
        assert queued.target_date == "2026-09-11"


@pytest.mark.parametrize("submitted,attempts,mode,repair", [
    (False, [], "browser", True), (True, [], "browser", False),
    (False, [{"submitted": True}], "browser", False), (False, [], "api", False),
])
def test_repair_requires_positive_evidence_of_no_released_browser_request(factory, submitted, attempts, mode, repair):
    with factory() as db:
        rid = old_run(db, submitted=submitted, attempts=attempts, mode=mode)
    assert service.recover_interrupted_runs(startup=True) == int(repair)
    with factory() as db:
        assert db.get(ReservationRun, rid).error_code == (
            "INTERRUPTED_SAFE_TO_RETRY" if repair else "INTERRUPTED_NEEDS_VERIFICATION")
    assert scheduling._enqueue_recently_missed_jobs(NOW) == int(repair)
    if not repair:
        assert scheduling._catchup_status[1]["state"] == "blocked"
        assert f"#{rid}" in scheduling._catchup_status[1]["message"]


def test_pending_cancel_is_not_claimed_or_resurrected_by_catchup(factory, monkeypatch):
    rid = service.enqueue_plan(1, "scheduled")
    with factory() as db:
        assert web._browser_command(rid, "cancel", db)["accepted"]
    monkeypatch.setattr(br, "run_browser", lambda *a, **kw: pytest.fail("cancelled job executed"))
    assert service.execute_plan(1, "scheduled", run_id=rid) == rid
    assert scheduling._enqueue_recently_missed_jobs(NOW) == 0
    with factory() as db:
        assert db.get(ReservationRun, rid).error_code == "BROWSER_CANCELLED"
        assert db.get(ReservationRun, rid).status == "SKIPPED"


def test_claim_and_cancel_have_exactly_one_winner(factory):
    rid = service.enqueue_plan(1, "scheduled")
    start = threading.Barrier(2)
    results = []
    def claim():
        with factory() as db:
            start.wait()
            results.append(service._claim_pending(db, rid))
    thread = threading.Thread(target=claim)
    thread.start()
    start.wait()
    results.append(service.cancel_pending_run(rid))
    thread.join(timeout=5)
    assert not thread.is_alive() and sum(results) == 1


def test_enqueue_failure_is_terminal_and_visible(factory, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError("fixture worker unavailable")
    monkeypatch.setattr(browser_sessions, "submit", fail)
    assert scheduling._enqueue_recently_missed_jobs(NOW) == 0
    assert scheduling._catchup_status[1]["state"] == "failed"
    with factory() as db:
        run = db.scalar(select(ReservationRun))
        assert run.status == "FAILED" and run.error_code == "ENQUEUE_FAILED"


def test_http_save_at_0729_creates_0730_catchup(factory, monkeypatch):
    from fastapi.testclient import TestClient
    with factory() as db:
        old_run(db)
    monkeypatch.setattr(web, "refresh_jobs", lambda: None)
    monkeypatch.setattr(web, "_enqueue_recently_missed_jobs", lambda: scheduling._enqueue_recently_missed_jobs(NOW))
    def database():
        with factory() as db:
            yield db
    overrides = dict(web.app.dependency_overrides)
    web.app.dependency_overrides[web.get_db] = database
    # No lifespan here: startup belongs to production and must not run in this fixture.
    client = TestClient(web.app, base_url="http://127.0.0.1")
    try:
        response = client.patch("/api/plans/1", json={"run_time": "07:30"}, headers={"x-csrf-token": web.CSRF_TOKEN})
        assert response.status_code == 200, response.text
        assert response.json()["account_enabled"] is True
        with factory() as db:
            queued = db.scalar(select(ReservationRun).where(ReservationRun.trigger == "scheduled_catchup"))
            assert queued and queued.request_snapshot["opening_at"] == "2026-09-11T07:30:00+08:00"
    finally:
        client.close()
        web.app.dependency_overrides.clear()
        web.app.dependency_overrides.update(overrides)


def test_windows_mutex_retains_full_handle_and_reports_duplicate():
    import ctypes
    import os
    import uuid
    from app.single_instance import mutex_api
    if os.name != "nt":
        pytest.skip("Windows-only mutex")
    kernel = mutex_api()
    name = "Local\\ChaoxingRegression-" + uuid.uuid4().hex
    first = kernel.CreateMutexW(None, False, name)
    second = None
    try:
        assert first
        second = kernel.CreateMutexW(None, False, name)
        assert second and ctypes.get_last_error() == 183
    finally:
        if second:
            kernel.CloseHandle(second)
        if first:
            kernel.CloseHandle(first)


def test_cancel_pending_login_clears_checking_indicator(factory):
    rid = service.enqueue_login(1)
    assert service.cancel_pending_run(rid)
    with factory() as db:
        assert db.get(Account, 1).login_status == "UNKNOWN"


def test_midnight_opening_uses_queued_next_day_not_day_after(factory, monkeypatch):
    current = dt.datetime(2026, 9, 10, 23, 56, tzinfo=TZ)
    opening = scheduled_opening("00:05", current)
    assert opening.isoformat() == "2026-09-11T00:05:00+08:00"
    assert weekday_name(opening) == "Friday"
    monkeypatch.setattr(service, "now_shanghai", lambda: current)
    monkeypatch.setattr(web, "scheduler", SimpleNamespace(running=True, get_job=lambda _: SimpleNamespace(
        next_run_time=dt.datetime(2026, 9, 11, 23, 55, tzinfo=TZ))))
    with factory() as db:
        plan = db.get(ReservationPlan, 1)
        plan.run_time = "00:05"
        db.commit()
    assert scheduling._enqueue_recently_missed_jobs(current) == 1
    with factory() as db:
        plan = db.get(ReservationPlan, 1)
        assert web._next_run_at(plan, current) == opening.isoformat()
        plan.account.enabled = False
        assert web._next_run_at(plan, current) is None


def test_redaction_preserves_query_shape_and_nonsecret_fidenc():
    raw = 'roomId=10713&enc=deadbeef&token=zzz&fidEnc=school'
    result = security.redact(raw)
    assert result == 'roomId=10713&enc=<redacted>&token=<redacted>&fidEnc=school'
    assert security.redact(result) == result
    assert security.redact('{"fidEnc":"school", "token":"secret"}') == '{"fidEnc":"school", "token":"<redacted>"}'


def test_checkpoint_redacts_before_comparing_for_commit_throttle(factory, monkeypatch):
    commits = []
    event.listen(factory.class_, "after_commit", lambda session: commits.append(1))
    def browser(rid, aid, values, day, fire, checkpoint, **kw):
        before = len(commits)
        for _ in range(5):
            assert checkpoint("WAITING_OPEN", "roomId=10713&token=secret", None)
        assert len(commits) - before == 1
        return br.BrowserResult("SKIPPED", "BROWSER_CANCELLED", "done")
    monkeypatch.setattr(br, "run_browser", browser)
    service.execute_plan(1)


@pytest.mark.parametrize("value", ["dpapi:v1:<垃圾>", "dpapi:v1:aGVsbG8="])
def test_corrupt_dpapi_is_a_validation_error(value):
    with pytest.raises(ValueError, match="重新填写"):
        security.unprotect_secret(value)


def test_saved_bad_secret_can_be_replaced_or_disabled(factory):
    with factory() as db:
        db.add_all([AppSetting(key="notify_type", value="bark"), AppSetting(key="notify_key", value="dpapi:v1:aGVsbG8=")])
        db.commit()
    with pytest.raises(ValueError):
        notify.save_settings("bark", "")
    notify.save_settings("bark", "new-fixture-key")
    notify.save_settings("none", "")
    assert notify.load_settings() == {"notify_type": "none", "notify_key_configured": False}


@pytest.mark.parametrize("kind,field,success", [("bark", "code", 200), ("serverchan", "code", 0), ("wecom_webhook", "errcode", 0)])
def test_notification_requires_business_success(monkeypatch, kind, field, success):
    for payload, good in [({field: success}, True), ({field: 400}, False), ({}, False), ([], False)]:
        monkeypatch.setattr(notify._session, "post", lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: payload))
        error = notify._dispatch({"notify_type": kind, "notify_key": "fixture"}, "test", "test")
        assert (error is None) is good


def test_stale_clock_read_is_nonblocking_and_refresh_singleflight(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def slow_refresh():
        calls.append(1)
        started.set()
        release.wait(5)
        finished.set()
    monkeypatch.setattr(clock, "_measured_at", 0)
    monkeypatch.setattr(clock, "_offset", 0.25)
    monkeypatch.setattr(clock, "_refresh_running", False)
    monkeypatch.setattr(clock, "refresh", slow_refresh)
    try:
        before = time.monotonic()
        for _ in range(30):
            assert clock.server_offset() == 0.25
        assert time.monotonic() - before < 0.5
        assert started.wait(1) and calls == [1]
    finally:
        release.set()
        assert finished.wait(1)
        # Join only the diagnostic's worker before monkeypatch restores globals.
        for thread in threading.enumerate():
            if thread.name == "clock-refresh":
                thread.join(timeout=1)
