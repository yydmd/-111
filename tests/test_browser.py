"""Browser safety and integration checks; all booking traffic is simulated."""
import datetime as dt
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode, parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import browser_reserve as br
from app.db import Account, Base, PlanSeat, ReservationPlan, ReservationRun
from app.run_state import ACTIVE_STATUSES, live_browser_run

DAY = "2026-09-10"
VALUES = {"room_id": "10713", "seats": ["097", "098"], "start_time": "08:30", "end_time": "09:30"}


def record(**overrides):
    tz = dt.timezone(dt.timedelta(hours=8))
    out = {"roomId": "10713", "seatNum": "097", "today": DAY,
           "startTime": int(dt.datetime(2026, 9, 10, 8, 30, tzinfo=tz).timestamp() * 1000),
           "endTime": int(dt.datetime(2026, 9, 10, 9, 30, tzinfo=tz).timestamp() * 1000)}
    out.update(overrides)
    return out


@pytest.mark.parametrize("items,state", [
    ([], "absent"), ([record()], "exact"), ([record(seatNum="98")], "exact"),
    ([record(seatNum="099")], "conflict"), ([record(roomId="9")], "conflict"),
    ([record(endTime=record()["endTime"] - 1800000)], "conflict"),
    ([record(), record(seatNum="098")], "conflict"), ([{}], "unavailable"),
    ([record(today="2026-09-11")], "unavailable"),
])
def test_exact_matching(items, state):
    assert br.reservation_match(items, VALUES, DAY)[0] == state


def test_covering_interval_requires_review_and_adjacent_does_not_overlap():
    tz = dt.timezone(dt.timedelta(hours=8))
    epoch = lambda hour, minute: int(dt.datetime(2026, 9, 10, hour, minute, tzinfo=tz).timestamp() * 1000)
    covering = record(startTime=epoch(8, 0), endTime=epoch(10, 0))
    adjacent = record(startTime=epoch(8, 0), endTime=epoch(8, 30))
    partial = record(startTime=epoch(8, 0), endTime=epoch(9, 0))
    assert br.reservation_match([covering], VALUES, DAY)[0] == "conflict"
    assert br.reservation_match([adjacent], VALUES, DAY)[0] == "absent"
    assert br.reservation_match([partial], VALUES, DAY)[0] == "conflict"


def test_covering_interval_is_confirmed_when_this_submit_extended_an_adjacent_record():
    tz = dt.timezone(dt.timedelta(hours=8))
    epoch = lambda hour, minute: int(dt.datetime(2026, 9, 10, hour, minute, tzinfo=tz).timestamp() * 1000)
    before = [record(startTime=epoch(8, 0), endTime=epoch(8, 30))]
    after = [record(startTime=epoch(8, 0), endTime=epoch(9, 30))]
    state, seat, detail = br.reservation_match(after, VALUES, DAY, before)
    assert (state, seat) == ("exact", "097")
    assert "提交前相邻预约" in detail
    assert br.reservation_match(after, VALUES, DAY, before + [record()])[0] == "conflict"


def test_passport_identity_is_available_before_office_app_opens():
    passport = [{"name": "_uid", "value": "42"}]
    office = passport + [{"name": "oa_uid", "value": "42"}]
    assert br.account_identity(passport, "user") == br.account_identity(office, "user")
    assert br.account_identity(passport, "user") is not None
    assert br.account_identity(passport + [{"name": "oa_uid", "value": "99"}], "user") is None
    assert br.account_identity([], "user") is None


class Route:
    def __init__(self, params=None, path=br.SUBMIT_PATH, query=""):
        self.request = SimpleNamespace(url=br.ORIGIN + path + query, post_data=urlencode(params or {}))
        self.result = None

    def fallback(self):
        self.result = "sent"

    def abort(self):
        self.result = "blocked"


def intent(**overrides):
    return {"roomId": "10713", "day": DAY, "seatNum": "097", "startTime": "08:30", "endTime": "09:30", **overrides}


def make_gate(preview=False, checkpoint=None):
    gate = br.RequestGate(VALUES, DAY, preview, checkpoint or (lambda *args: True), br.Control(), time.monotonic() + 30)
    gate.armed_seat = "097"
    return gate


def test_preview_blocks_submit_get_post_and_other_mutations():
    gate = make_gate(True)
    for route in [Route(intent()), Route(query="?" + urlencode(intent())), Route(path="/data/apps/seat/signback")]:
        gate.route(route)
        assert route.result == "blocked"
    read = Route(path="/data/apps/seat/getusedtimes")
    gate.route(read)
    assert read.result == "sent"


@pytest.mark.parametrize("changes", [{"seatNum": "099"}, {"roomId": "4"}, {"day": "2026-09-11"}, {"endTime": "10:00"}])
def test_changed_intent_never_reaches_platform(changes):
    gate = make_gate()
    route = Route(intent(**changes))
    gate.route(route)
    assert route.result == "blocked" and not gate.pending


def test_commit_before_wire_and_duplicate_callback_blocked():
    route = Route(intent())
    seen = []
    gate = make_gate(checkpoint=lambda *args: seen.append((route.result, args[2])) or True)
    gate.route(route)
    again = Route(intent())
    gate.route(again)
    assert seen == [(None, True)]
    assert route.result == "sent" and again.result == "blocked" and gate.pending


def test_persistence_failure_or_disabled_account_blocks_request():
    for callback in [lambda *a: False, lambda *a: (_ for _ in ()).throw(RuntimeError())]:
        gate = make_gate(checkpoint=callback)
        route = Route(intent())
        gate.route(route)
        assert route.result == "blocked" and not gate.pending


def test_cancel_expiry_account_change_and_duplicate_parameters():
    for cause in ("cancel", "expiry", "identity", "duplicate"):
        gate = make_gate()
        route = Route(intent(), query="?seatNum=097" if cause == "duplicate" else "")
        if cause == "cancel": gate.control.cancel.set()
        if cause == "expiry": gate.deadline = 0
        if cause == "identity": gate.identity_check = lambda: False
        gate.route(route)
        assert route.result == "blocked"


@pytest.mark.parametrize("payload,rejected", [
    ({"success": False, "msg": "座位已被预约"}, True),
    ({"success": True}, False), ({"success": False, "msg": "安全验证失败"}, False),
    ([], False),
])
def test_only_explicit_rejection_allows_next_seat(payload, rejected):
    gate = make_gate()
    gate.route(Route(intent()))
    gate.response(SimpleNamespace(url=br.ORIGIN + br.SUBMIT_PATH, json=lambda: payload))
    assert gate.rejected is rejected
    challenged = isinstance(payload, dict) and "安全验证" in str(payload.get("msg", ""))
    assert gate.challenge_pending is challenged
    assert gate.pending is not (rejected or challenged)


def test_seat_rejection_cannot_continue_when_checkpoint_fails():
    gate = make_gate(checkpoint=lambda *args: args[2] is True)
    gate.route(Route(intent()))
    gate.response(SimpleNamespace(
        url=br.ORIGIN + br.SUBMIT_PATH,
        json=lambda: {"success": False, "msg": "座位已被预约"},
    ))
    assert gate.pending and not gate.rejected
    assert gate.control.cancel.is_set()


def test_target_closed_detection_survives_broken_page_probe():
    class TargetClosedError(Exception):
        pass

    class BrokenPage:
        def is_closed(self):
            raise TargetClosedError()

    assert br._browser_target_closed(BrokenPage(), RuntimeError("connection lost"))
    assert br._browser_target_closed(None, TargetClosedError())


@pytest.mark.parametrize("message,code,terminal", [
    ("操作频繁，请稍后再试", "RATE_LIMITED", False),
    ("检测到异常操作，风控拦截", "BLOCKED_BY_RISK", True),
])
def test_rate_limit_is_retryable_but_explicit_risk_is_terminal(message, code, terminal):
    checkpoints = []
    gate = make_gate(checkpoint=lambda *args: checkpoints.append(args) or True)
    gate.route(Route(intent()))
    gate.response(SimpleNamespace(
        url=br.ORIGIN + br.SUBMIT_PATH,
        json=lambda: {"success": False, "msg": message},
    ))
    assert gate.terminal_code == (code if terminal else None)
    assert bool(gate.terminal_message) is terminal
    assert not gate.pending
    assert gate.rejected is not terminal
    assert gate.rejection_code == (None if terminal else code)
    assert checkpoints[-1][0] == ("BLOCKED_BY_RISK" if terminal else "RUNNING")
    assert checkpoints[-1][2] is False


def test_refresh_rejection_arms_one_same_seat_retry_without_loosening_attempted():
    # The platform's 303 remedy ("refresh and resubmit") is an explicit refusal,
    # not a risk verdict, so the same seat may be re-sent once — through the same
    # explicit unlock the human challenge uses. ``attempted`` itself is never
    # relaxed: once that unlock is consumed, the seat is blocked again.
    gate = make_gate()
    gate.route(Route(intent()))
    assert gate.attempted == {"097"} and gate.pending
    gate.response(SimpleNamespace(
        url=br.ORIGIN + br.SUBMIT_PATH,
        json=lambda: {"success": False,
                      "msg": "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约"},
    ))
    # Classified as a refreshable rejection, NOT as a human challenge, even
    # though the wording contains 安全验证.
    assert gate.refresh_pending and gate.refresh_serial == 1
    assert not gate.challenge_pending and not gate.rejected and not gate.terminal_code
    retry = Route(intent())
    gate.route(retry)
    assert retry.result == "sent" and not gate.refresh_pending
    # Unlock consumed: pending cleared, a third send for the same seat is still
    # refused by the attempted guard.
    gate.pending = False
    gate.sent_seat = "097"
    third = Route(intent())
    gate.route(third)
    assert third.result == "blocked"


def test_gate_records_secret_free_timing_and_meets_local_p95_budget():
    measurements = []
    for _ in range(1000):
        gate = make_gate()
        gate.mark_click("097")
        gate.route(Route(intent()))
        gate.response(SimpleNamespace(
            url=br.ORIGIN + br.SUBMIT_PATH,
            json=lambda: {"success": True},
        ))
        timing = gate.attempt_timings[0]
        assert set(timing) == {
            "seat", "click_to_request_ms", "gate_handler_ms",
            "request_to_response_ms", "_route_finished_ns", "_request",
        }
        assert "token" not in str(timing).lower()
        measurements.append(timing["gate_handler_ms"])
    assert sorted(measurements)[949] <= 10


def test_blocked_submit_consumes_click_timing():
    gate = make_gate(preview=True)
    gate.mark_click("097")
    gate.route(Route(intent()))
    assert gate._click_started is None

    gate.preview = False
    gate.armed_seat = "098"
    route = Route(intent(seatNum="098"))
    gate.route(route)
    assert route.result == "sent"
    assert "click_to_request_ms" not in gate.attempt_timings[0]


def test_response_timing_is_attached_to_its_request_not_latest_attempt():
    gate = make_gate()
    first = Route(intent())
    gate.route(first)
    gate.pending = False
    gate.challenge_pending = True
    second = Route(intent())
    gate.route(second)

    first_response = SimpleNamespace(
        url=br.ORIGIN + br.SUBMIT_PATH,
        request=first.request,
        json=lambda: {"success": True},
    )
    second_response = SimpleNamespace(
        url=br.ORIGIN + br.SUBMIT_PATH,
        request=second.request,
        json=lambda: {"success": True},
    )
    gate.response(first_response)
    assert "request_to_response_ms" in gate.attempt_timings[0]
    assert "request_to_response_ms" not in gate.attempt_timings[1]
    gate.response(second_response)
    assert "request_to_response_ms" in gate.attempt_timings[1]


def test_official_challenge_allows_one_same_seat_continuation_only():
    checkpoints = []
    gate = make_gate(checkpoint=lambda *args: checkpoints.append(args) or True)
    first = Route(intent())
    gate.route(first)
    gate.response(SimpleNamespace(
        url=br.ORIGIN + br.SUBMIT_PATH,
        json=lambda: {"success": False, "msg": "请完成安全验证"},
    ))
    continuation = Route(intent())
    gate.route(continuation)
    duplicate = Route(intent())
    gate.route(duplicate)
    assert first.result == continuation.result == "sent"
    assert duplicate.result == "blocked"
    assert gate.pending and not gate.challenge_pending
    assert [args[2] for args in checkpoints] == [True, False, True]


def test_controls_and_heartbeat():
    control = br.Control()
    br._controls[991] = control
    try:
        assert br.command(991, "focus") and control.focus.is_set()
        assert br.command(991, "cancel") and control.cancel.is_set()
        assert not br.command(991, "unsupported")
    finally:
        br._controls.pop(991)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    assert live_browser_run(now, now + dt.timedelta(seconds=300), now)
    assert not live_browser_run(now - dt.timedelta(seconds=40), now + dt.timedelta(seconds=300), now)


def test_launch_context_uses_system_browser_without_positional_urls(monkeypatch):
    calls = []
    monkeypatch.setattr(br, "browser_channel", lambda: "msedge")
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **k: calls.append((a, k))))
    br.launch_context(pw, "profile")
    assert calls[0][1]["channel"] == "msedge"
    assert calls[0][1]["args"] == ["--window-size=1200,900"]
    assert calls[0][1]["no_viewport"] is True
    assert calls[0][1]["chromium_sandbox"] is True


def test_request_gate_registration_is_narrow_and_reversible():
    calls = []
    context = SimpleNamespace(
        route=lambda pattern, handler: calls.append(("route", pattern, handler)),
        unroute=lambda pattern, handler: calls.append(("unroute", pattern, handler)),
    )
    gate = make_gate()
    br.install_request_gate(context, gate)
    br.remove_request_gate(context, gate)
    count = len(br.REQUEST_GATE_PATTERNS)
    assert [item[1] for item in calls[:count]] == list(br.REQUEST_GATE_PATTERNS)
    assert [item[1] for item in calls[count:]] == list(br.REQUEST_GATE_PATTERNS)
    assert all(item[1] != "**/*" for item in calls)


def test_real_persistent_browser_launch(tmp_path):
    if not br.desktop_available():
        pytest.skip("interactive desktop unavailable")
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        context = br.launch_context(pw, tmp_path / "profile")
        try:
            assert context.pages
            context.pages[0].set_content("<title>Browser launch check</title>")
            assert context.pages[0].title() == "Browser launch check"
            page = context.pages[0]
            assert page.viewport_size is None
            assert page.evaluate("innerWidth") > 520
            # Native resizing, like the user's maximized screenshot, must
            # change the layout viewport rather than leave it at 520 pixels.
            session = context.new_cdp_session(page)
            window = session.send("Browser.getWindowForTarget")
            before = page.evaluate("innerWidth")
            session.send("Browser.setWindowBounds", {"windowId": window["windowId"],
                "bounds": {"windowState": "normal", "width": 950, "height": 750}})
            page.wait_for_function("before => innerWidth !== before", arg=before)
            session.detach()
            br.window_state(page, False)
            session = context.new_cdp_session(page)
            assert session.send('Browser.getWindowForTarget')['bounds']['windowState'] == 'minimized'
            br.window_state(page, True)
            assert session.send('Browser.getWindowForTarget')['bounds']['windowState'] != 'minimized'
            session.detach()
        finally:
            context.close()


def test_native_window_raise_uses_topmost_fallback_when_activation_is_blocked():
    class User32:
        def __init__(self):
            self.calls = []
            self.activations = iter((False, True))

        def ShowWindow(self, hwnd, state):
            self.calls.append(("restore", hwnd, state))

        def BringWindowToTop(self, hwnd):
            self.calls.append(("raise", hwnd))

        def SetForegroundWindow(self, hwnd):
            self.calls.append(("activate", hwnd))
            return next(self.activations)

        def SetWindowPos(self, hwnd, after, *args):
            self.calls.append(("zorder", hwnd, after))

    user32 = User32()
    assert br._raise_native_window(user32, 123, "topmost", "notopmost")
    assert user32.calls == [
        ("restore", 123, 9),
        ("raise", 123),
        ("activate", 123),
        ("zorder", 123, "topmost"),
        ("zorder", 123, "notopmost"),
        ("raise", 123),
        ("activate", 123),
    ]


def test_native_window_raise_avoids_zorder_bounce_when_activation_works():
    class User32:
        def __init__(self): self.calls = []
        def ShowWindow(self, hwnd, state): self.calls.append("restore")
        def BringWindowToTop(self, hwnd): self.calls.append("raise")
        def SetForegroundWindow(self, hwnd): self.calls.append("activate"); return True
        def SetWindowPos(self, *args): self.calls.append("zorder")

    user32 = User32()
    assert br._raise_native_window(user32, 123, "topmost", "notopmost")
    assert user32.calls == ["restore", "raise", "activate"]


@pytest.fixture
def db_factory(tmp_path, monkeypatch):
    from app import service
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "notify_async", lambda *a: None)
    with factory() as db:
        account = Account(name="demo", username="user", password_blob=b"unused")
        plan = ReservationPlan(account=account, name="plan", room_id="10713", execution_mode="browser",
                               start_time="08:30", end_time="09:30", run_time="19:00", enabled=True)
        plan.seats = [PlanSeat(seat_num="097", priority=0)]
        db.add(plan); db.commit()
    yield factory
    engine.dispose()


def test_browser_dispatch_snapshot_and_waiting_lifecycle(db_factory, monkeypatch):
    from app import service
    from app.web import _run_json
    def browser(run_id, account_id, values, day, fire, checkpoint, **kwargs):
        assert values["username"] == "user"
        assert checkpoint("WAITING_USER", "请验证", None)
        with db_factory() as db:
            run = db.get(ReservationRun, run_id)
            assert run.finished_at is None and run.expires_at and run.heartbeat_at
            assert _run_json(run)["execution_mode"] == "browser"
            assert service.active_run_count() == 1
        checkpoint("VERIFYING", "即将提交", True, "097")
        return br.BrowserResult(
            "NEEDS_VERIFICATION", "SUBMIT_OUTCOME_UNKNOWN", "核对失败", "097",
            attempt_timings=[{"seat": "097", "click_to_request_ms": 2.5,
                              "gate_handler_ms": 1.25, "request_to_response_ms": 8.75}],
        )
    monkeypatch.setattr(br, "run_browser", browser)
    monkeypatch.setattr(service, "ChaoxingClient", lambda *a, **k: pytest.fail("browser mode must not log in through requests"))
    run_id = service.execute_plan(1)
    with db_factory() as db:
        run = db.get(ReservationRun, run_id)
        assert run.possibly_submitted and run.finished_at
        assert "username" not in run.request_snapshot
        assert run.attempt_details[0]["timing"] == {
            "click_to_request_ms": 2.5,
            "gate_handler_ms": 1.25,
            "request_to_response_ms": 8.75,
        }
    next_run = service.execute_plan(1)
    with db_factory() as db:
        assert db.get(ReservationRun, next_run).error_code == "UNRESOLVED_SUBMISSION"


def test_browser_rate_limit_is_retryable_failure_and_persisted_with_timing(db_factory, monkeypatch):
    from app import service
    calls = []
    def browser(run_id, account_id, values, day, fire, checkpoint, **kwargs):
        calls.append(values["seats"])
        assert checkpoint("VERIFYING", "预约请求即将发送", True, "097")
        assert checkpoint("RUNNING", "官方提示操作频繁；已退避并继续检查下一候选", False, "097", "RATE_LIMITED")
        return br.BrowserResult(
            "FAILED", "RATE_LIMITED", "官方提示操作频繁；已退避并继续检查下一候选", "097",
            attempt_timings=[{"seat": "097", "gate_handler_ms": 0.5}],
        )
    monkeypatch.setattr(br, "run_browser", browser)
    run_id = service.execute_plan(1)
    with db_factory() as db:
        run = db.get(ReservationRun, run_id)
        assert run.status == "FAILED" and run.error_code == "RATE_LIMITED"
        assert not run.possibly_submitted
        assert run.attempt_details == [{
            "seat": "097", "source": "browser", "submitted": True,
            "code": "RATE_LIMITED", "message": "官方提示操作频繁；已退避并继续检查下一候选",
            "timing": {"gate_handler_ms": 0.5},
        }]
    assert calls == [["097"]]


def test_terminal_risk_checkpoint_survives_window_close_without_replay(db_factory, monkeypatch):
    from app import service
    calls = []

    def browser(*args, **kwargs):
        calls.append(args[0])
        checkpoint = args[5]
        assert checkpoint(
            "BLOCKED_BY_RISK",
            "官方拒绝本次操作；已停止本次任务，不再换座或追加请求",
            False,
            "097",
            "BLOCKED_BY_RISK",
        )
        return br.BrowserResult("SKIPPED", "BROWSER_WINDOW_CLOSED", "窗口意外关闭")

    monkeypatch.setattr(br, "run_browser", browser)
    run_id = service.execute_plan(1)
    with db_factory() as db:
        run = db.get(ReservationRun, run_id)
        assert run.status == "BLOCKED_BY_RISK"
        assert run.error_code == "BLOCKED_BY_RISK"
        assert not run.possibly_submitted
    assert len(calls) == 1


def test_direct_preview_of_api_plan_never_uses_submit_client(db_factory, monkeypatch):
    from app import service
    with db_factory() as db:
        db.get(ReservationPlan, 1).execution_mode = "api"
        db.commit()
    monkeypatch.setattr(service, "ChaoxingClient", lambda *a, **k: pytest.fail("preview cannot create an API submit client"))
    def preview(*args, **kwargs):
        assert kwargs["preview"] is True
        return br.BrowserResult("PROBE_DONE", "BROWSER_PREVIEW_READY", "演练")
    monkeypatch.setattr(br, "run_browser", preview)
    rid = service.execute_plan(1, "browser_preview")
    with db_factory() as db:
        run = db.get(ReservationRun, rid)
        assert run.request_snapshot["execution_mode"] == "browser"
        assert not run.possibly_submitted


def test_restart_marks_even_fresh_browser_waits_without_replaying(db_factory):
    from app import service
    with db_factory() as db:
        run = service._run_snapshot(db.get(ReservationPlan, 1), "scheduled")
        run.status = "WAITING_USER"; run.possibly_submitted = True
        db.add(run); db.commit()
    assert service.recover_interrupted_runs(startup=True) == 1
    with db_factory() as db:
        run = db.scalar(select(ReservationRun))
        assert run.status == "NEEDS_VERIFICATION" and run.possibly_submitted


def test_v9_migration_preserves_wal_data_and_defaults(tmp_path, monkeypatch):
    import sqlite3
    from app import db as module
    path = tmp_path / "v9.db"
    source = sqlite3.connect(path)
    source.executescript("PRAGMA journal_mode=WAL; CREATE TABLE reservation_plans(id INTEGER PRIMARY KEY); CREATE TABLE reservation_runs(id INTEGER PRIMARY KEY); PRAGMA user_version=9; INSERT INTO reservation_plans VALUES(7);")
    source.commit()
    monkeypatch.setattr(module, "DATABASE_PATH", path)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    module._migrate_database()
    assert source.execute("SELECT execution_mode FROM reservation_plans WHERE id=7").fetchone() == ("browser",)
    assert source.execute("PRAGMA user_version").fetchone()[0] == 11
    backup = next((tmp_path / "backups").glob("*.db"))
    with sqlite3.connect(backup) as c:
        assert c.execute("SELECT id FROM reservation_plans").fetchone()[0] == 7
        assert c.execute("PRAGMA user_version").fetchone()[0] == 9
    source.close()


HTML = '''<!doctype html><meta charset="utf-8"><div class="order"></div>
<div class="time_pop"><div class="time_cell">08:30-09:00</div><div class="time_cell">09:00-09:30</div>
<span class="time_sure">提交</span></div><button id="human" hidden>模拟人工完成验证</button>
<script>
window.fidEnc='fixture';const v={chosedDay:'2026-09-10',seatRoom:{id:10713},chosedSeatNum:'097',timesShow:true,
 dynamicChosedTimeInfo:{startTime:'',endTime:''},dynamicTimes:[{time:'08:30-09:00',cls:''},{time:'09:00-09:30',cls:''}]};
document.querySelector('.order').__vue__=v;
document.querySelectorAll('.time_cell').forEach((el,i)=>el.onclick=()=>{v.dynamicChosedTimeInfo.startTime='08:30';v.dynamicChosedTimeInfo.endTime=i?'09:30':'09:00'});
const send=()=>fetch('/data/apps/seat/submit',{method:'POST',body:new URLSearchParams({roomId:'10713',seatNum:v.chosedSeatNum,day:v.chosedDay,startTime:v.dynamicChosedTimeInfo.startTime,endTime:v.dynamicChosedTimeInfo.endTime})});
document.querySelector('.time_sure').onclick=()=>document.querySelector('#human').hidden=false;
document.querySelector('#human').onclick=()=>{send();send()};
</script>'''


@pytest.fixture
def page_fixture():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        sent = []
        def transport(route):
            if route.request.url.endswith(br.SUBMIT_PATH):
                sent.append(route.request.post_data)
                route.fulfill(json={"success": True})
            else:
                route.fulfill(body=HTML, content_type="text/html")
        context.route("**/*", transport)
        page = context.new_page()
        yield page, context, sent
        browser.close()


def test_real_browser_dynamic_verification_and_single_submission(page_fixture):
    page, context, sent = page_fixture
    gate = make_gate()
    br.install_request_gate(context, gate)
    page.goto(br.ORIGIN + "/front/third/apps/seat/select")
    button, _ = br.PageAdapter(page).prepare(VALUES, DAY, "097")
    button.click()
    assert page.locator('#human').is_visible() and not sent
    # This is a synthetic button, not a real captcha.
    page.locator('#human').click()
    page.wait_for_timeout(150)
    assert len(sent) == 1 and gate.pending


def test_real_browser_preview_blocks_even_manual_submission(page_fixture):
    page, context, sent = page_fixture
    gate = make_gate(preview=True)
    br.install_request_gate(context, gate)
    page.goto(br.ORIGIN + "/front/third/apps/seat/select")
    button, _ = br.PageAdapter(page).prepare(VALUES, DAY, "097")
    button.click(); page.locator('#human').click(); page.wait_for_timeout(100)
    assert not sent and not gate.pending


def test_official_risk_token_request_bypasses_python_gate(page_fixture):
    page, context, sent = page_fixture
    risk_requests = []
    context.route(br.ORIGIN + "/risk/token", lambda route: (
        risk_requests.append(route.request.url), route.fulfill(json={"token": "fixture"})
    )[-1])
    gate = make_gate()
    routed = []
    original_route = gate.route
    def observe(route):
        routed.append(route.request.url)
        return original_route(route)
    gate.route = observe
    br.install_request_gate(context, gate)
    risk_html = HTML.replace(
        "document.querySelector('.time_sure').onclick=()=>document.querySelector('#human').hidden=false;",
        "document.querySelector('.time_sure').onclick=()=>fetch('/risk/token').then(()=>send());",
    )
    context.route(br.ORIGIN + "/front/third/apps/seat/select?**",
                  lambda route: route.fulfill(body=risk_html, content_type="text/html"))
    page.goto(br.ORIGIN + "/front/third/apps/seat/select?id=10713")
    button, _ = br.PageAdapter(page).prepare(VALUES, DAY, "097")
    gate.mark_click("097")
    button.click()
    page.wait_for_timeout(150)
    assert len(risk_requests) == len(sent) == 1
    assert routed == [br.ORIGIN + br.SUBMIT_PATH]


def test_nested_unknown_seat_mutation_is_still_blocked(page_fixture):
    page, context, _ = page_fixture
    gate = make_gate()
    br.install_request_gate(context, gate)
    page.goto(br.ORIGIN + "/front/third/apps/seat/select")
    result = page.evaluate("""async () => {
      try {
        const response = await fetch('/front/third/apps/data/apps/seat/cancel', {method:'POST'});
        return `sent:${response.status}`;
      } catch (_) {
        return 'blocked';
      }
    }""")
    assert result == "blocked"


def test_real_browser_adapter_rejects_wrong_day_or_missing_control(page_fixture):
    page, _, _ = page_fixture
    page.goto(br.ORIGIN + "/front/third/apps/seat/select")
    with pytest.raises(ValueError):
        br.PageAdapter(page).prepare(VALUES, "2026-09-11", "097")
    page.locator('.time_sure').evaluate('(el)=>el.remove()')
    with pytest.raises(ValueError, match="提交按钮"):
        br.PageAdapter(page).prepare(VALUES, DAY, "097")


def test_official_not_open_notice_is_not_a_login_or_adapter_failure(page_fixture):
    page, _, _ = page_fixture
    page.goto(br.ORIGIN + "/front/apps/reserve/error/code/500")
    page.set_content("<body>当前区域未到开放预约时间，请咨询管理员确认该区域开放时间</body>")
    assert br.opening_notice(page)
    page.set_content("<body>未知页面错误</body>")
    assert not br.opening_notice(page)


@pytest.mark.parametrize("mode,expected", [("preview", "PROBE_DONE"), ("success", "SUCCESS"),
    ("unknown", "NEEDS_VERIFICATION"), ("cancel", "SKIPPED"), ("close", "SKIPPED"),
    ("recheck", "SKIPPED"), ("login", "SKIPPED"),
    ("qr_confirm", "PROBE_DONE"), ("qr_slow", "PROBE_DONE"),
    ("qr_cancel", "SKIPPED"), ("qr_changed", "SKIPPED"),
    ("scheduled_open", "SUCCESS"), ("existing_disabled", "SKIPPED"), ("early_captcha", "SUCCESS"),
    ("list_delay", "SUCCESS"), ("merged", "NEEDS_VERIFICATION"),
    ("adjacent_merge", "SUCCESS"), ("seat_fallback", "SUCCESS"),
    ("response_captcha", "SUCCESS"), ("late_response_captcha", "SUCCESS"), ("direct_submit", "SUCCESS"),
    ("scheduled_direct", "SUCCESS"), ("scheduled_stale_date", "SUCCESS"),
    ("rate_response", "FAILED"), ("rate_then_success", "SUCCESS"),
    ("stale_plain", "FAILED"), ("stale_security", "SUCCESS"),
    ("stale_redirect", "NEEDS_VERIFICATION"),
    ("risk_response", "BLOCKED_BY_RISK")])
def test_full_browser_runner_with_simulated_transport(page_fixture, tmp_path, monkeypatch, mode, expected):
    page, context, sent = page_fixture
    import playwright.sync_api
    list_reads_after_send = [0]
    list_reads_before_send = [0]
    # The real browser runs all page interactions. Only its HTTP API transport
    # is replaced; no test request can reach a real reservation service.
    class Proxy:
        def __getattr__(self, name):
            return getattr(context, name)
        @property
        def request(self):
            def get(*args, **kwargs):
                if mode == "late_response_captcha" and late_response["response"] is not None and not late_response["released"]:
                    late_response["released"] = True
                    original_gate_response(late_response["gate"], late_response["response"])
                items = [record(seatNum="098" if mode == "rate_then_success" else "097")] if mode == "existing_disabled" or (sent and mode in {"success", "scheduled_open", "early_captcha", "direct_submit", "scheduled_direct", "scheduled_stale_date"}) or (mode in {"response_captcha", "late_response_captcha", "rate_then_success", "stale_security"} and len(sent) >= 2) else []
                if sent and mode == 'list_delay':
                    list_reads_after_send[0] += 1
                    items = [record()] if list_reads_after_send[0] >= 2 else []
                elif not sent:
                    list_reads_before_send[0] += 1
                if sent and mode == 'merged':
                    tz = dt.timezone(dt.timedelta(hours=8))
                    items = [record(
                        startTime=int(dt.datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp() * 1000),
                        endTime=int(dt.datetime(2026, 9, 10, 10, 0, tzinfo=tz).timestamp() * 1000))]
                if mode == 'adjacent_merge':
                    tz = dt.timezone(dt.timedelta(hours=8))
                    items = [record(
                        startTime=int(dt.datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp() * 1000),
                        endTime=int(dt.datetime(2026, 9, 10, 9 if sent else 8, 30, tzinfo=tz).timestamp() * 1000))]
                if mode == 'seat_fallback' and len(sent) == 2:
                    items = [record(seatNum='098')]
                return SimpleNamespace(status=200, json=lambda: {"success": True, "data": {"curReserves": items}})
            return SimpleNamespace(get=get)
    class Runtime:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **k: Proxy()))
        def __exit__(self, *args):
            return False
    if mode != "login":
        context.add_cookies([{"name": "oa_uid", "value": "42", "url": br.ORIGIN}])
    profile = tmp_path / "browser-profiles" / "account-1"
    profile.mkdir(parents=True)
    if mode != "login" and not mode.startswith("qr_"):
        (profile / "account-binding.json").write_text(json.dumps({"identity": hashlib.sha256(b"user|42").hexdigest()}))
    context.route("https://passport2.chaoxing.com/login?**", lambda route: route.fulfill(body="<p>Login required</p>", content_type="text/html"))
    context.route("https://passport2.chaoxing.com/mooc/accountManage", lambda route: route.fulfill(
        body='<input id="phone" name="user_mobile">', content_type="text/html"))
    monkeypatch.setattr(br, "DATA_DIR", tmp_path)
    monkeypatch.setattr(br, "readiness", lambda: {"ready": True})
    monkeypatch.setattr(br, "VERIFY_SECONDS", 0.1)
    if mode == 'list_delay':
        monkeypatch.setattr(br, "VERIFY_SECONDS", 3)
    if mode == 'seat_fallback':
        def candidate_page(route):
            seat = parse_qs(urlparse(route.request.url).query)['seatNum'][0]
            route.fulfill(body=HTML.replace("chosedSeatNum:'097'", f"chosedSeatNum:'{seat}'"), content_type='text/html')
        context.route(br.ORIGIN + '/front/third/apps/seat/select?**', candidate_page)
        def rejection_then_success(route):
            sent.append(route.request.post_data)
            rejected = parse_qs(route.request.post_data)['seatNum'][0] == '097'
            route.fulfill(json={'success':not rejected, 'msg':'座位已被预约' if rejected else '成功'})
        context.route(br.ORIGIN + br.SUBMIT_PATH, rejection_then_success)
    if mode in {'response_captcha', 'late_response_captcha'}:
        challenge_html = HTML.replace(
            "document.querySelector('.time_sure').onclick=()=>document.querySelector('#human').hidden=false;",
            "document.querySelector('.time_sure').onclick=()=>{send();document.querySelector('#human').hidden=false};",
        )
        context.route(br.ORIGIN + '/front/third/apps/seat/select?**',
                      lambda route: route.fulfill(body=challenge_html, content_type='text/html'))
        def challenge_then_success(route):
            sent.append(route.request.post_data)
            route.fulfill(json={"success": len(sent) >= 2,
                                "msg": "成功" if len(sent) >= 2 else "请完成安全验证"})
        context.route(br.ORIGIN + br.SUBMIT_PATH, challenge_then_success)
    if mode in {'rate_response', 'rate_then_success', 'risk_response'}:
        if mode == 'rate_then_success':
            def candidate_page(route):
                seat = parse_qs(urlparse(route.request.url).query)['seatNum'][0]
                route.fulfill(body=HTML.replace("chosedSeatNum:'097'", f"chosedSeatNum:'{seat}'"), content_type='text/html')
            context.route(br.ORIGIN + '/front/third/apps/seat/select?**', candidate_page)
        def reject_for_risk(route):
            sent.append(route.request.post_data)
            if mode == 'rate_then_success' and len(sent) >= 2:
                route.fulfill(json={"success": True, "msg": "成功"})
            else:
                message = "操作频繁，请稍后再试" if mode != 'risk_response' else "检测到异常操作，风控拦截"
                route.fulfill(json={"success": False, "msg": message})
        context.route(br.ORIGIN + br.SUBMIT_PATH, reject_for_risk)
    if mode in {'stale_plain', 'stale_security', 'stale_redirect'}:
        # P0: the platform's 303 remedy ("page sat too long, refresh and
        # resubmit"). Three real-world shapes are probed here because the gate
        # classifies on message text: the platform's wording contains
        # "安全验证", which the gate may read as a human challenge rather than
        # as a refreshable rejection.
        stale_message = ("您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约"
                         if mode == 'stale_security' else "您在页面停留过久，请刷新后再提交预约")
        def stale_submit(route):
            sent.append(route.request.post_data)
            if mode == 'stale_redirect':
                route.fulfill(status=303, headers={"Location": "https://passport2.chaoxing.com/login"})
            elif mode == 'stale_security' and len(sent) >= 2:
                route.fulfill(json={"success": True, "msg": "成功"})
            else:
                route.fulfill(json={"success": False, "msg": stale_message})
        context.route(br.ORIGIN + br.SUBMIT_PATH, stale_submit)
    direct_html = HTML.replace(
        "document.querySelector('.time_sure').onclick=()=>document.querySelector('#human').hidden=false;",
        "document.querySelector('.time_sure').onclick=()=>send();",
    )
    if mode == 'direct_submit':
        context.route(br.ORIGIN + '/front/third/apps/seat/select?**',
                      lambda route: route.fulfill(body=direct_html, content_type='text/html'))
    late_response = {"gate": None, "response": None, "released": False}
    original_gate_response = br.RequestGate.response
    if mode == 'late_response_captcha':
        def hold_first_challenge(gate, response):
            if (response.url.endswith(br.SUBMIT_PATH) and
                    response.json().get("success") is False and
                    late_response["response"] is None):
                late_response["gate"] = gate
                late_response["response"] = response
                return
            original_gate_response(gate, response)
        monkeypatch.setattr(br.RequestGate, "response", hold_first_challenge)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", Runtime)
    server_now = [time.time()]
    fire_epoch = server_now[0] + (30 if mode in {"scheduled_open", "scheduled_direct", "scheduled_stale_date"} else 0)
    monkeypatch.setattr(br.clock, "server_now", lambda: server_now[0])
    navigations = []
    if mode in {"scheduled_open", "scheduled_direct", "scheduled_stale_date", "existing_disabled", "early_captcha"}:
        def seat_page(route):
            navigations.append(route.request.url)
            html = direct_html if mode in {"scheduled_direct", "scheduled_stale_date"} else HTML
            if mode in {"scheduled_open", "scheduled_direct"} and server_now[0] < fire_epoch:
                html = '<p>当前区域未到开放预约时间</p>'
            if mode == "scheduled_stale_date" and server_now[0] < fire_epoch:
                html = direct_html.replace("chosedDay:'2026-09-10'", "chosedDay:'2026-09-09'")
            if mode == "existing_disabled":
                html = HTML.replace("cls:''", "cls:'noSelect'")
            if mode == "early_captcha":
                html += '<div id="early-challenge">请完成安全验证</div>'
            route.fulfill(body=html, content_type="text/html")
        context.route(br.ORIGIN + "/front/third/apps/seat/select?**", seat_page)
    elapsed = [0]
    delayed_read = [False]
    if mode == "qr_slow":
        monkeypatch.setattr(br, "time", SimpleNamespace(
            monotonic=lambda: time.monotonic() + elapsed[0], sleep=time.sleep))
        original_state = br.PageAdapter.state
        def delayed_state(adapter):
            if delayed_read[0]:
                delayed_read[0] = False
                return None
            return original_state(adapter)
        monkeypatch.setattr(br.PageAdapter, "state", delayed_state)
    seen = []
    seen_full = []
    def checkpoint(status, message, submitted, *args):
        seen.append((status, submitted))
        seen_full.append((status, submitted, *args))
        if mode in {"scheduled_open", "scheduled_direct", "scheduled_stale_date"} and status == "WAITING_OPEN":
            assert not sent
            server_now[0] = fire_epoch + 0.1
        if mode == "early_captcha" and status == "WAITING_USER" and "官方页面出现安全验证" in message:
            assert len(navigations) == 1 and not sent
            assert page.evaluate("v.dynamicChosedTimeInfo.startTime") == ''
            page.locator('#early-challenge').evaluate('(e)=>e.remove()')
            return True
        if mode == "login" and status == "WAITING_LOGIN":
            br.command(42, "cancel")
        if mode.startswith("qr_") and status == "WAITING_USER":
            assert message.startswith("请确认登录账号：")
            assert not sent
            if mode == "qr_cancel":
                br.command(42, "cancel")
            else:
                assert br.command(42, "confirm_account")
                if mode == "qr_slow":
                    elapsed[0] += 20
                    delayed_read[0] = True
                if mode == "qr_changed":
                    context.add_cookies([{"name": "oa_uid", "value": "99", "url": br.ORIGIN}])
            return True
        if status == "WAITING_USER":
            if mode == "cancel":
                br.command(42, "cancel")
            elif mode == "close":
                page.close()
            elif mode in {"response_captcha", "late_response_captcha"} and len(sent) == 1:
                page.locator("#human").click()
            elif not sent or mode in {'rate_then_success', 'stale_security', 'stale_plain'} or (mode == 'seat_fallback' and len(sent) == 1):
                # rate_then_success / stale_* must let the human step fire on
                # every attempt: the first seat is throttled or refused with
                # "refresh and resubmit", and the follow-up attempt (after the
                # gate's back-off / page reload) must still be driven, not left
                # waiting forever.
                page.locator("#human").click()
        return True
    values = {**VALUES, "seats": ["097"], "username": "user"}
    if mode in {'seat_fallback', 'rate_then_success'}:
        values['seats'] = ['097', '098']
    result = br.run_browser(42, 1, values, DAY, fire_epoch, checkpoint,
                           preview=mode == "preview" or mode.startswith("qr_"), check_only=mode == "recheck")
    assert result.status == expected, result
    stale_sends = {'stale_plain': 2, 'stale_redirect': 1, 'stale_security': 2}
    if mode in stale_sends:
        assert len(sent) == stale_sends[mode], (mode, len(sent))
    else:
        assert len(sent) == (2 if mode in {'seat_fallback', 'response_captcha', 'late_response_captcha', 'rate_then_success'} else 1 if mode in {"success", "unknown", "scheduled_open", "scheduled_direct", "scheduled_stale_date", "direct_submit", "early_captcha", "list_delay", "merged", "adjacent_merge", "rate_response", "risk_response"} else 0)
    if mode in {'stale_plain', 'stale_security'}:
        # P0/P2-1: the platform's "refresh and resubmit" refusal is now an
        # explicit, bounded automatic retry — the same seat is reloaded and
        # re-sent exactly once, without waiting for a human.
        assert any(entry[0] == 'RUNNING' and entry[1] is False and entry[-1] == 'TOKEN_STALE'
                   for entry in seen_full), seen_full
        # The wording contains 安全验证; proving the challenge branch was NOT
        # taken is what makes this an automatic retry, not a human wait.
        assert not any(status == 'WAITING_USER' and submitted is False
                       for status, submitted, *_ in seen_full)
    if mode == 'stale_plain':
        # The retry is refused too, so the seat is reported honestly as stale
        # rather than as an unknown outcome or as "seat taken".
        assert result.code == 'TOKEN_STALE'
    if mode == 'stale_security':
        # The retry is accepted: recovery inside the opening window.
        assert result.code is None and len(sent) == 2
    if mode == 'stale_redirect':
        # Known limitation: a bodyless 3xx on the submit path carries no text
        # for the gate to classify, so it still degrades to an unknown outcome.
        assert len(sent) == 1
        assert result.code == 'SUBMIT_OUTCOME_UNKNOWN'
    if mode == 'list_delay':
        assert list_reads_after_send[0] == 2
    if mode == 'success':
        # Exactly one reservation-list read before the submit — the pre-fire
        # check. The old post-fire re-check sat on the critical path (an
        # off-script request roughly one round trip before the click, on an
        # endpoint the official seat page never calls) and has been removed.
        assert list_reads_before_send[0] == 1, list_reads_before_send[0]
    if mode == 'seat_fallback':
        assert [parse_qs(body)['seatNum'][0] for body in sent] == ['097', '098']
    if mode == 'merged':
        assert result.code == 'BROWSER_RESERVATION_CONFLICT'
    if mode == 'adjacent_merge':
        assert result.code is None and '提交前相邻预约' in result.message
    if mode in {"scheduled_open", "scheduled_direct", "scheduled_stale_date"}:
        assert len(navigations) == 2
    if mode in {"direct_submit", "scheduled_direct", "scheduled_stale_date"}:
        assert all(status != "WAITING_USER" for status, _ in seen)
    if mode == "existing_disabled":
        assert result.code == "ALREADY_BOOKED_ON_SERVER"
    if mode == "unknown":
        assert result.code == "SUBMIT_OUTCOME_UNKNOWN"
    if mode == "rate_response":
        assert result.code == "RATE_LIMITED"
    if mode == "risk_response":
        assert result.code == "BLOCKED_BY_RISK"
    if mode == "recheck":
        assert result.code == "BROWSER_CHECK_ABSENT"
    if mode == "login":
        assert ("WAITING_LOGIN", None) in seen
    if mode.startswith("qr_"):
        binding = profile / "account-binding.json"
        assert binding.exists() == (mode in {"qr_confirm", "qr_slow"})
        if mode in {"qr_confirm", "qr_slow"}:
            assert json.loads(binding.read_text())["verification"] == "user_confirmed"
    assert 42 not in br._controls


def test_encrypted_session_restores_session_cookies_and_rejects_other_accounts(page_fixture, tmp_path):
    _, context, _ = page_fixture
    context.add_cookies([{"name":"_uid", "value":"42", "url":br.ORIGIN},
                         {"name":"login_token", "value":"fixture-auth-token", "url":br.ORIGIN}])
    expected = br.account_identity(context.cookies(br.ORIGIN), "user")
    assert br.save_browser_session(context, tmp_path, "user", expected)
    ciphertext = (tmp_path / "session.dpapi").read_text()
    assert ciphertext.startswith("dpapi:v1:") and "fixture-auth-token" not in ciphertext
    context.clear_cookies()
    assert not br.restore_browser_session(context, tmp_path, "user", "another-identity")
    assert br.restore_browser_session(context, tmp_path, "user", expected)
    assert any(c['name']=='login_token' and c['value']=='fixture-auth-token' and c['expires']==-1
               for c in context.cookies(br.ORIGIN))
    context.add_cookies([{"name":"_uid", "value":"99", "url":br.ORIGIN}])
    assert not br.restore_browser_session(context, tmp_path, "user", expected)
    assert not br.save_browser_session(context, tmp_path, "user", expected)
    assert (tmp_path / "session.dpapi").read_text() == ciphertext
    context.clear_cookies()
    (tmp_path / "session.dpapi").write_text('{"cookies": []}')
    assert not br.restore_browser_session(context, tmp_path, "user", expected)


def test_account_confirmation_only_accepts_current_pending_identity():
    control = br.Control()
    br._controls[901] = control
    try:
        assert not br.command(901, "confirm_account")
        control.awaiting_identity = "shown-account"
        assert br.command(901, "confirm_account")
        assert control.confirmed_identity == "shown-account"
        control.cancel.set()
        assert not br.command(901, "confirm_account")
    finally:
        br._controls.pop(901)


def test_late_browser_task_does_not_receive_another_five_minutes(monkeypatch):
    monkeypatch.setattr(br, "readiness", lambda: {"ready": True})
    monkeypatch.setattr(br.clock, "server_now", lambda: 1001)
    monkeypatch.setattr(br, "launch_context", lambda *args: pytest.fail("expired job must not launch"))
    result = br.run_browser(902, 1, VALUES, DAY, 700, lambda *args: True)
    assert result.code == "DEADLINE_EXCEEDED"
    assert 902 not in br._controls


def test_rollout_evidence_is_per_plan_and_invalidated_by_edits(db_factory, monkeypatch):
    from app import service, web
    from app.browser_acceptance import acceptance
    from fastapi import HTTPException
    with db_factory() as db:
        plan = db.get(ReservationPlan, 1)
        plan.execution_mode = "api"
        preview = service._run_snapshot(plan, "browser_preview")
        preview.status = "PROBE_DONE"; preview.error_code = "BROWSER_PREVIEW_READY"
        db.add(preview); db.commit()
        assert acceptance(db, plan)["can_trial"]
        assert not acceptance(db, plan)["can_enable"]
        monkeypatch.setattr(web, "enqueue_plan", lambda *args: 123)
        assert web.trial_browser(plan.id, db)["run_id"] == 123
        assert plan.execution_mode == "api"
        success = service._run_snapshot(plan, "browser_trial")
        success.status = "SUCCESS"; success.finished_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        db.add(success); db.commit()
        assert acceptance(db, plan)["can_enable"]
        monkeypatch.setattr(web, "refresh_jobs", lambda: None)
        assert web.enable_browser(plan.id, db)["execution_mode"] == "browser"
        plan.end_time = "10:00"; db.commit()
        assert not acceptance(db, plan)["can_trial"]
        assert not acceptance(db, plan)["can_enable"]
        with pytest.raises(HTTPException):
            web.trial_browser(plan.id, db)
        plan.end_time = "09:30"; db.commit()
        preview.plan_id = None; db.commit()
        assert not acceptance(db, plan)["preview_passed"]


def test_browser_trial_of_api_plan_uses_browser_without_switching(db_factory, monkeypatch):
    from app import service
    with db_factory() as db:
        db.get(ReservationPlan, 1).execution_mode = "api"
        db.commit()
    monkeypatch.setattr(service, "ChaoxingClient", lambda *a, **kw: pytest.fail("trial must use browser"))
    def browser(*args, **kwargs):
        assert not kwargs['preview'] and not kwargs['check_only']
        return br.BrowserResult("SUCCESS", None, "模拟官方记录匹配", "097")
    monkeypatch.setattr(br, "run_browser", browser)
    rid = service.execute_plan(1, "browser_trial")
    with db_factory() as db:
        assert db.get(ReservationPlan, 1).execution_mode == "api"
        assert db.get(ReservationRun, rid).request_snapshot['execution_mode'] == 'browser'


def test_scheduled_browser_snapshot_retains_opening_and_wait_budget(db_factory, monkeypatch):
    from app import service
    from zoneinfo import ZoneInfo
    queued = dt.datetime(2026, 9, 8, 18, 59, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
    opened = queued.replace(hour=19, minute=0, second=0).timestamp()
    monkeypatch.setattr(service, 'now_shanghai', lambda: queued)
    with db_factory() as db:
        plan = db.get(ReservationPlan, 1)
        run = service._run_snapshot(plan, 'scheduled')
        db.add(run); db.commit(); rid = run.id
        plan.run_time = '20:00'
        plan.weekdays_json = '[]'
        db.commit()
    monkeypatch.setattr(service.app_clock, 'server_now', lambda: opened + 40)
    def browser(run_id, account_id, values, day, fire_epoch, checkpoint, **kwargs):
        assert fire_epoch == opened and values['run_time'] == '19:00'
        with db_factory() as db:
            remaining = (db.get(ReservationRun, rid).expires_at - dt.datetime.now(dt.UTC).replace(tzinfo=None)).total_seconds()
        assert 258 < remaining <= 260
        return br.BrowserResult('SKIPPED', 'TEST_COMPLETE', '模拟结束')
    monkeypatch.setattr(br, 'run_browser', browser)
    service.execute_plan(1, 'scheduled', run_id=rid)
    with db_factory() as db:
        assert db.get(ReservationRun, rid).error_code == 'TEST_COMPLETE'


def test_disabled_account_during_wait_stops_before_submit(db_factory, monkeypatch):
    from app import service
    def browser(run_id, account_id, values, day, fire, checkpoint, **kwargs):
        with db_factory() as db:
            db.get(Account, account_id).enabled = False
            db.commit()
        assert not checkpoint("VERIFYING", "准备提交", True, "097")
        return br.BrowserResult("SKIPPED", "BROWSER_CANCELLED", "账号停用")
    monkeypatch.setattr(br, "run_browser", browser)
    run_id = service.execute_plan(1)
    with db_factory() as db:
        assert not db.get(ReservationRun, run_id).possibly_submitted


def test_pre_submit_window_close_is_recovered_once(db_factory, monkeypatch):
    from app import service
    outcomes = [
        br.BrowserResult("SKIPPED", "BROWSER_WINDOW_CLOSED", "窗口意外关闭"),
        br.BrowserResult("SUCCESS", None, "官方记录已确认", "097"),
    ]
    calls = []

    def browser(*args, **kwargs):
        calls.append((args[0], args[4]))
        return outcomes.pop(0)

    monkeypatch.setattr(br, "run_browser", browser)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: 1000)
    run_id = service.execute_plan(1)
    with db_factory() as db:
        run = db.get(ReservationRun, run_id)
        assert run.status == "SUCCESS" and run.selected_seat == "097"
        assert not run.possibly_submitted
    assert len(calls) == 2


def test_pre_submit_window_is_not_reopened_after_account_is_disabled(db_factory, monkeypatch):
    from app import service
    calls = []

    def browser(*args, **kwargs):
        calls.append(args[0])
        with db_factory() as db:
            db.get(Account, 1).enabled = False
            db.commit()
        return br.BrowserResult("SKIPPED", "BROWSER_WINDOW_CLOSED", "窗口意外关闭")

    monkeypatch.setattr(br, "run_browser", browser)
    run_id = service.execute_plan(1)
    with db_factory() as db:
        run = db.get(ReservationRun, run_id)
        assert run.error_code == "PLAN_OR_ACCOUNT_DISABLED"
        assert not run.possibly_submitted
    assert len(calls) == 1


def test_explicitly_cancelled_browser_run_is_never_replayed(db_factory, monkeypatch):
    from app import service
    calls = []

    def browser(*args, **kwargs):
        calls.append(args[0])
        return br.BrowserResult("SKIPPED", "BROWSER_CANCELLED", "用户取消")

    monkeypatch.setattr(br, "run_browser", browser)
    service.execute_plan(1)
    assert len(calls) == 1


def test_unknown_submission_recheck_uses_original_snapshot(db_factory, monkeypatch):
    from app import service
    with db_factory() as db:
        plan = db.get(ReservationPlan, 1)
        original = service._run_snapshot(plan, "manual")
        original.status = "NEEDS_VERIFICATION"; original.possibly_submitted = True
        original.target_date = DAY
        db.add(original); db.commit()
        original_id = original.id
        plan.start_time = "10:00"; plan.end_time = "11:00"; db.commit()
    from app import browser_sessions
    monkeypatch.setattr(browser_sessions, "submit", lambda *a, **k: None)
    queued = service.enqueue_plan(1, "browser_recheck", duplicate_of_run_id=original_id)
    def browser(run_id, account_id, values, day, fire, checkpoint, **kwargs):
        assert day == DAY and values["start_time"] == "08:30" and kwargs["check_only"]
        return br.BrowserResult("SKIPPED", "BROWSER_CHECK_ABSENT", "明确没有预约")
    monkeypatch.setattr(br, "run_browser", browser)
    service.execute_plan(1, "browser_recheck", run_id=queued)
    with db_factory() as db:
        assert not db.get(ReservationRun, original_id).possibly_submitted


def test_exact_recheck_promotes_unresolved_original_to_success(db_factory, monkeypatch):
    from app import service, browser_sessions
    with db_factory() as db:
        plan = db.get(ReservationPlan, 1)
        original = service._run_snapshot(plan, "manual")
        original.status = "NEEDS_VERIFICATION"
        original.error_code = "BROWSER_RESERVATION_CONFLICT"
        original.possibly_submitted = True
        db.add(original)
        db.commit()
        original_id = original.id
    monkeypatch.setattr(browser_sessions, "submit", lambda *a, **k: None)
    queued = service.enqueue_plan(1, "browser_recheck", duplicate_of_run_id=original_id)
    monkeypatch.setattr(br, "run_browser", lambda *a, **k:
        br.BrowserResult("SKIPPED", "BROWSER_CHECK_EXACT", "官方记录完整覆盖目标时段", "097"))
    service.execute_plan(1, "browser_recheck", run_id=queued)
    with db_factory() as db:
        fixed = db.get(ReservationRun, original_id)
        assert fixed.status == "SUCCESS" and fixed.error_code is None
        assert not fixed.possibly_submitted and fixed.selected_seat == "097"
        assert f"核对任务 #{queued}" in fixed.message


def test_exact_recheck_does_not_resolve_a_different_candidate_seat(db_factory, monkeypatch):
    from app import service, browser_sessions
    with db_factory() as db:
        plan = db.get(ReservationPlan, 1)
        matching = service._run_snapshot(plan, "manual")
        matching.status = "NEEDS_VERIFICATION"
        matching.possibly_submitted = True
        matching.candidate_seats_json = '["097"]'
        unrelated = service._run_snapshot(plan, "manual")
        unrelated.status = "NEEDS_VERIFICATION"
        unrelated.possibly_submitted = True
        unrelated.candidate_seats_json = '["098"]'
        db.add_all([matching, unrelated])
        db.commit()
        matching_id, unrelated_id = matching.id, unrelated.id
    monkeypatch.setattr(browser_sessions, "submit", lambda *a, **k: None)
    queued = service.enqueue_plan(1, "browser_recheck", duplicate_of_run_id=matching_id)
    monkeypatch.setattr(br, "run_browser", lambda *a, **k:
        br.BrowserResult("SKIPPED", "BROWSER_CHECK_EXACT", "官方记录完整覆盖目标时段", "097"))
    service.execute_plan(1, "browser_recheck", run_id=queued)
    with db_factory() as db:
        assert db.get(ReservationRun, matching_id).status == "SUCCESS"
        other = db.get(ReservationRun, unrelated_id)
        assert other.status == "NEEDS_VERIFICATION"
        assert other.possibly_submitted and other.selected_seat is None


def test_active_browser_blocks_mode_switch_and_duplicate_enqueue(db_factory, monkeypatch):
    from app import service, web
    from fastapi import HTTPException
    with db_factory() as db:
        run = service._run_snapshot(db.get(ReservationPlan, 1), "scheduled")
        run.status = "WAITING_USER"
        db.add(run); db.commit()
        with pytest.raises(HTTPException) as error:
            web.patch_plan(1, {"execution_mode": "api"}, db)
        assert error.value.status_code == 409
        with pytest.raises(HTTPException):
            web.preview_browser(1, db)
        monkeypatch.setattr(service._executor, "submit", lambda *a, **k: pytest.fail("must not enqueue duplicate"))
        assert service.enqueue_plan(1, "scheduled_catchup") == run.id


def test_management_page_browser_controls_and_mode_save(page_fixture):
    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path
    page, _, _ = page_fixture
    errors, saves = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    html = Environment(loader=FileSystemLoader("app/templates")).get_template("index.html").render(csrf="fixture")
    plan = {"id":1, "account_id":1, "name":"演练计划", "room_id":"10713", "seats":["097"],
            "start_time":"08:30", "end_time":"09:30", "run_time":"19:00", "day_offset":2,
            "max_attempts":1,"enabled":True,"execution_mode":"api", "weekdays":["Monday","Tuesday"]}
    expires = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=280)).isoformat()
    run = {"id":1,"plan_name":"演练计划","account_name":"演示账号","status":"WAITING_USER",
           "trigger":"scheduled","execution_mode":"browser","expires_at":expires,"candidate_seats":["097"],
           "target_date":DAY,"message":"请确认登录账号：请检查官方账号资料"}
    def api(route):
        path = route.request.url.split(br.ORIGIN)[-1]
        payload = {"/api/settings":{"notify_type":"none"},"/api/browser/status":{"ready":True,"message":"浏览器组件就绪"},
                   "/api/browser/acceptance":[{"plan_id":1,"message":"演练通过，尚需正式试约", "preview_passed":True,
                       "preview":{"run_id":8,"target_date":DAY},"formal":None,"can_trial":True,"can_enable":False}],
                   "/api/accounts":[{"id":1,"name":"演示账号","username":"demo","enabled":True}],
                   "/api/plans":[plan],"/api/runs":[run]}.get(path, {})
        if path == "/api/plans/1":
            posted = route.request.post_data_json
            saves.append(posted); plan.update(posted); payload = plan
        route.fulfill(json=payload)
    page.route("**/api/**", api)
    page.route("**/health", lambda route: route.fulfill(json={"ok":True}))
    page.route(br.ORIGIN + "/", lambda route: route.fulfill(body=html, content_type="text/html"))
    page.goto(br.ORIGIN + "/")
    page.get_by_role("button", name="检查登录／提前登录", exact=True).wait_for()
    assert page.get_by_role("button", name="显示原窗口").is_visible()
    assert page.get_by_role("button", name="停止接管").is_visible()
    assert page.get_by_role("button", name="确认是本计划账号").is_visible()
    assert page.get_by_role("button", name="浏览器正式试约", exact=True).count() == 0
    assert page.get_by_role("button", name="切换为浏览器模式", exact=True).count() == 0
    page.get_by_role("button", name="编辑", exact=True).click()
    assert page.locator('select[name="execution_mode"]').count() == 0
    future = {"enabled": True, "next_run_at": "2026-09-09T19:00:00+08:00", "day_offset": 2}
    assert page.evaluate("p => nextTargetDate(p)", future) == "2026-09-11"
    assert page.evaluate("p => nextRunDisplay({...p,enabled:false})", future) == "已停用"
    assert page.evaluate("p => nextTargetDate({...p,enabled:false})", future) == "—"
    assert page.evaluate("p => nextRunDisplay({...p,next_run_at:null})", future) == "暂无已排定任务"
    assert page.evaluate("r => runStatusLabel(r)",
                         {"status": "RUNNING", "error_code": None,
                          "possibly_submitted": False}) == "提前准备 · 尚未提交"
    page.get_by_role("button", name="保存修改", exact=True).click()
    page.wait_for_timeout(150)
    assert saves and saves[0]["execution_mode"] == "browser"
    assert not errors
    directory = Path("data/qa")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / "browser-management.png"), full_page=True)


@pytest.mark.parametrize("mode", ["ready", "expired", "network", "wrong_account", "fresh", "reuse"])
def test_account_login_official_check_and_reused_session(page_fixture, tmp_path, monkeypatch, mode):
    from app import browser_sessions
    from contextlib import contextmanager
    page, context, sent = page_fixture
    profile = tmp_path / 'browser-profiles' / 'account-77'
    profile.mkdir(parents=True)
    identity = hashlib.sha256(b'user|42').hexdigest()
    (profile / 'account-binding.json').write_text(json.dumps({'identity':identity}))
    if mode != 'fresh':
        context.add_cookies([{'name':'_uid','value':'99' if mode=='wrong_account' else '42','domain':'.chaoxing.com','path':'/'}])
    if mode == 'network':
        br.save_browser_session(context, profile, 'user', identity)
    initial_store = (profile / 'session.dpapi').read_text() if mode == 'network' else None
    def account_page(route):
        if mode == 'network':
            route.abort()
        elif mode in {'expired','wrong_account'}:
            route.fulfill(status=302, headers={'Location':'https://passport2.chaoxing.com/login'})
        else:
            route.fulfill(body='<input id="phone" name="user_mobile">', content_type='text/html')
    context.route(br.ACCOUNT_URL, account_page)
    context.route('https://passport2.chaoxing.com/login', lambda r:r.fulfill(body='<input type="password">',content_type='text/html'))
    actor = browser_sessions.AccountSession(77)
    class Proxy:
        def __getattr__(self, name): return getattr(context, name)
        @property
        def request(self):
            return SimpleNamespace(get=lambda *args, **kwargs:SimpleNamespace(status=200,json=lambda:{'success':True,'data':{'curReserves':[record()] if sent else []}}))
    @contextmanager
    def runtime(): yield None
    actor.runtime = runtime
    launches=[]
    monkeypatch.setattr(br, 'launch_context', lambda *a:(launches.append(1) or Proxy()))
    monkeypatch.setattr(browser_sessions, 'current_session', lambda:actor)
    monkeypatch.setattr(br, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(br, 'readiness', lambda:{'ready':True})
    windows=[]
    monkeypatch.setattr(br, 'window_state', lambda page, foreground=False:windows.append(foreground))
    seen=[]
    def checkpoint(status, message, *args):
        seen.append((status,message))
        if status=='WAITING_LOGIN': br.command(909,'cancel')
        return True
    result=br.run_browser(909,77,{'username':'user','seats':[]},None,time.time(),checkpoint,login_only=True)
    assert not sent
    if mode in {'expired','wrong_account','fresh'}:
        assert result.code=='BROWSER_CANCELLED'
        assert any(s=='WAITING_LOGIN' for s,m in seen) and True in windows
    elif mode=='network':
        assert result.code=='LOGIN_CHECK_FAILED'
        assert (profile/'session.dpapi').read_text()==initial_store
    else:
        assert result.code=='LOGIN_READY'
        assert not any(s=='WAITING_LOGIN' for s,m in seen)
        assert True not in windows
    if mode=='reuse':
        context.route(br.ORIGIN+'/front/third/apps/seat/select?**', lambda r:r.fulfill(
            body=HTML.replace("()=>document.querySelector('#human').hidden=false", "()=>send()"),content_type='text/html'))
        result=br.run_browser(910,77,{**VALUES,'username':'user','seats':['097']},DAY,time.time(),checkpoint)
        assert result.status=='SUCCESS',result
        assert len(sent)==1 and launches==[1]
        assert not any(s=='WAITING_USER' for s,m in seen)
    actor.close_context()


def test_account_actor_stops_playwright_object(monkeypatch):
    from app import browser_sessions as sessions
    stopped = []
    actor = sessions.AccountSession(7799)
    actor.pw = SimpleNamespace(stop=lambda: stopped.append(True))
    actor.runtime_owner = object()  # The manager intentionally has no stop().
    monkeypatch.setattr(sessions, 'IDLE_SECONDS', 0)
    actor.run()
    assert stopped == [True]
    assert actor.pw is None and actor.runtime_owner is None


def test_account_without_plan_login_and_booking_queue_handoff(db_factory, monkeypatch):
    from app import service, browser_sessions
    with db_factory() as db:
        account=Account(name='second',username='other',password_blob=b'',enabled=True)
        db.add(account);db.commit(); aid=account.id
    jobs=[]
    monkeypatch.setattr(browser_sessions,'submit',lambda *args, **kwargs:jobs.append((args,kwargs)))
    login_id=service.enqueue_login(aid)
    assert service.enqueue_login(aid)==login_id
    with db_factory() as db:
        assert db.get(ReservationRun,login_id).plan_id is None
    first_login=service.enqueue_login(1)
    booked=service.enqueue_plan(1,'scheduled')
    assert booked!=first_login
    assert service.enqueue_plan(1,'scheduled')==booked
    with db_factory() as db:
        assert db.get(ReservationRun,booked).request_snapshot['execution_mode']=='browser'
    assert len(jobs)==3


def test_actor_serializes_same_account_and_runs_different_accounts_independently(monkeypatch):
    import threading
    from app import browser_sessions as sessions
    first_running=threading.Event(); release=threading.Event(); other_done=threading.Event(); second_done=threading.Event()
    thread_ids=[]
    def first():
        thread_ids.append(threading.get_ident());first_running.set();release.wait(5)
    def second(): thread_ids.append(threading.get_ident());second_done.set()
    monkeypatch.setattr(sessions,'IDLE_SECONDS',0.1)
    sessions.submit(8801,first)
    assert first_running.wait(2)
    sessions.submit(8801,second)
    sessions.submit(8802,other_done.set)
    assert other_done.wait(2) and not second_done.is_set()
    release.set()
    assert second_done.wait(2) and thread_ids[0]==thread_ids[1]


def test_midnight_prepare_targets_opening_day_and_weekday(db_factory,monkeypatch):
    from app import service,scheduler as sched
    from zoneinfo import ZoneInfo
    now=dt.datetime(2026,9,7,23,55,tzinfo=ZoneInfo('Asia/Shanghai'))
    monkeypatch.setattr(service,'now_shanghai',lambda:now)
    with db_factory() as db:
        plan=db.get(ReservationPlan,1)
        plan.run_time='00:05';plan.day_offset=2;plan.weekdays_json='["Tuesday"]';db.commit()
        run=service._run_snapshot(plan,'scheduled')
        assert run.target_date=='2026-09-10'
        assert dt.datetime.fromisoformat(run.request_snapshot['opening_at']).date()==dt.date(2026,9,8)
    monkeypatch.setattr(sched,'SessionLocal',db_factory)
    sched.refresh_jobs()
    job=sched.scheduler.get_job('plan-1')
    assert str(job.trigger.fields[4])=='0'  # Monday preparation for Tuesday opening
    sched.scheduler.remove_job('plan-1')



def test_login_worker_persists_official_status_without_a_plan(db_factory, monkeypatch):
    from app import service, browser_sessions, web
    captured=[]
    monkeypatch.setattr(browser_sessions,'submit',lambda *args,**kwargs:None)
    rid=service.enqueue_login(1)
    def login(run_id,account_id,values,day,fire,checkpoint,**kwargs):
        assert kwargs['login_only'] and not values['seats'] and day is None
        assert checkpoint('RUNNING','官方已确认登录有效',None)
        return br.BrowserResult('SKIPPED','LOGIN_READY','已确认登录')
    monkeypatch.setattr(br,'run_browser',login)
    service.execute_login(rid)
    with db_factory() as db:
        account=db.get(Account,1);run=db.get(ReservationRun,rid)
        assert account.login_status=='READY' and account.login_checked_at
        assert run.plan_id is None and run.finished_at and not run.possibly_submitted
        assert 'username' not in run.request_snapshot
        assert web.account_login_status(1,db)['run_id'] is None
        assert web.AccountIn(name='new',username='new-account').password==''


def test_v10_migration_preserves_account_and_disabled_plan(tmp_path,monkeypatch):
    import sqlite3
    from app import db as module
    path=tmp_path/'v10.db'
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TABLE accounts(id INTEGER PRIMARY KEY, username TEXT, password_blob BLOB); CREATE TABLE reservation_plans(id INTEGER PRIMARY KEY,execution_mode TEXT,enabled BOOLEAN); CREATE TABLE reservation_runs(id INTEGER PRIMARY KEY,status TEXT); PRAGMA user_version=10;")
        c.execute("INSERT INTO accounts VALUES(1,'fixture',?)",(b'encrypted-fixture',))
        c.execute("INSERT INTO reservation_plans VALUES(7,'api',0)")
        c.execute("INSERT INTO reservation_runs VALUES(326,'SUCCESS')")
    monkeypatch.setattr(module,'DATABASE_PATH',path);monkeypatch.setattr(module,'DATA_DIR',tmp_path)
    module._migrate_database()
    with sqlite3.connect(path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==11
        assert c.execute('SELECT username,password_blob,login_status FROM accounts').fetchone()==('fixture',b'encrypted-fixture','UNKNOWN')
        assert c.execute('SELECT execution_mode,enabled FROM reservation_plans').fetchone()==('browser',0)
        assert c.execute('SELECT status FROM reservation_runs').fetchone()==('SUCCESS',)
    backup=next((tmp_path/'backups').glob('app-before-v11-*.db'))
    with sqlite3.connect(backup) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==10


def test_old_mode_input_is_accepted_but_cannot_restore_api_flow(db_factory,monkeypatch):
    from app import web,service,browser_sessions
    monkeypatch.setattr(web,'_refresh_jobs_and_catch_up',lambda:None)
    with db_factory() as db:
        result=web.patch_plan(1,{'execution_mode':'api'},db)
        assert result['execution_mode']=='browser'
    dispatched=[]
    monkeypatch.setattr(browser_sessions,'submit',lambda *args,**kwargs:dispatched.append((args,kwargs)))
    rid=service.enqueue_plan(1)
    assert dispatched and dispatched[0][1]['run_id']==rid
    with db_factory() as db:
        assert db.get(ReservationRun,rid).request_snapshot['execution_mode']=='browser'
