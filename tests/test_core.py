import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.chaoxing_client import ChaoxingClient, normalize_seat, parse_select_context_url, parse_select_url, ProbeResult, ReservationError
from app.db import Account, AppSetting, Base, PlanSeat, ReservationPlan, ReservationRun
from app.service import execute_plan
from app.validation import normalize_time, validate_reservation_time_range
from app.web import PlanIn, PlanPatch, _plan_json


def test_normalize_seat():
    assert normalize_seat("6") == "006"
    assert normalize_seat("056") == "056"
    with pytest.raises(ValueError):
        normalize_seat("A6")


def test_page_parser_supports_submit_enc():
    response = type("Response", (), {"content": b'<input id="submit_enc" value="abc"><input id="algorithm" value="algo">', "text": '<input id="submit_enc" value="abc"><input id="algorithm" value="algo">'})()
    result = ChaoxingClient("u", "p")._parse_page(response)
    assert result == ProbeResult(True, "abc", "algo", "none", "TOKEN_READY", "已取得页面的预约参数")


def test_page_parser_detects_point_click():
    response = type("Response", (), {"content": "请点击图中目标".encode(), "text": "请点击图中目标"})()
    result = ChaoxingClient("u", "p")._parse_page(response)
    assert not result.ok
    assert result.captcha_type == "point_click"


def test_currently_occupied_page_is_not_reported_as_captcha():
    html = "该座位已被别人预约，等待用户签到中。当前预约时段 08:00-08:30"
    response = type("Response", (), {"content": html.encode(), "text": html})()
    result = ChaoxingClient("u", "p")._parse_page(response)
    assert not result.ok
    assert result.captcha_type == "none"
    assert result.page_state == "CURRENTLY_OCCUPIED"


def test_currently_occupied_page_with_complete_params_keeps_legacy_submission_path():
    html = '该座位已被别人预约<input id="submit_enc" value="token"><input id="algorithm" value="algo">'
    response = type("Response", (), {"content": html.encode(), "text": html})()
    result = ChaoxingClient("u", "p")._parse_page(response)
    assert result.ok
    assert result.page_state == "TOKEN_READY"
    assert result.token == "token"


def test_token_missing_page_is_not_reported_as_captcha():
    html = "座位预约，目前无人使用，您可直接进行预约"
    response = type("Response", (), {"content": html.encode(), "text": html})()
    result = ChaoxingClient("u", "p")._parse_page(response)
    assert not result.ok
    assert result.captcha_type == "none"
    assert result.page_state == "TOKEN_MISSING"


def test_normalize_time_accepts_single_digit_hour():
    assert normalize_time("8:30") == "08:30"
    with pytest.raises(ValueError):
        normalize_time("24:00")


def test_reservation_time_range_is_limited_to_12_hours():
    validate_reservation_time_range("08:00", "20:00")
    with pytest.raises(ValueError, match="12"):
        validate_reservation_time_range("08:00", "20:01")


def test_plan_input_normalizes_time_and_rejects_invalid_duration():
    plan = PlanIn(account_id=1, name="test", room_id="10713", seats=["097"], start_time="8:30", end_time="20:00", run_time="6:59")
    assert (plan.start_time, plan.run_time) == ("08:30", "06:59")
    with pytest.raises(ValueError, match="12"):
        PlanIn(account_id=1, name="test", room_id="10713", seats=["097"], start_time="08:00", end_time="20:01")


def test_plan_input_normalizes_candidate_seats_and_rejects_duplicates():
    plan = PlanIn(account_id=1, name="test", room_id="10713", seats=["97", "098"], start_time="08:00", end_time="09:00")
    assert plan.seats == ["097", "098"]
    with pytest.raises(ValueError, match="不能重复"):
        PlanIn(account_id=1, name="test", room_id="10713", seats=["97", "097"], start_time="08:00", end_time="09:00")
    with pytest.raises(ValueError, match="候选座位数"):
        PlanIn(account_id=1, name="test", room_id="10713", seats=["001", "002"], start_time="08:00", end_time="09:00", max_attempts=1)


def test_plan_input_normalizes_legacy_full_context_url_and_rejects_wrong_room():
    plan = PlanIn(
        account_id=1,
        name="test",
        room_id="10713",
        seats=["097"],
        start_time="08:00",
        end_time="09:00",
        select_params={"id": "10713"},
        select_context_path="https://office.chaoxing.com/front/third/apps/seat/select",
        select_context_source="room_context",
    )
    assert plan.select_context_path == "/front/third/apps/seat/select"
    with pytest.raises(ValueError, match="阅览室 ID 不一致"):
        PlanIn(
            account_id=1,
            name="test",
            room_id="10714",
            seats=["097"],
            start_time="08:00",
            end_time="09:00",
            select_params={"id": "10713"},
        )


def test_plan_response_roundtrips_through_patch_without_losing_context(tmp_path):
    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.select_params_json = json.dumps({"id": "100"})
    plan.select_context_source = "room_context"
    plan.select_context_path = "/front/third/apps/seat/select"
    db.commit()
    payload = _plan_json(plan)
    parsed = PlanPatch.model_validate(payload)
    assert (parsed.select_context_source, parsed.select_context_path) == ("room_context", "/front/third/apps/seat/select")
    db.close()


def test_room_context_discovery_stores_a_relative_path(monkeypatch):
    client = ChaoxingClient("u", "p")
    monkeypatch.setattr(client, "_current_page", lambda *_: object())
    monkeypatch.setattr(client, "_parse_page", lambda _: ProbeResult(False, page_state="TOKEN_MISSING"))
    monkeypatch.setattr(client, "_select_context_from_response", lambda *_: None)
    monkeypatch.setattr(client, "fetch_target_day_page", lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", page_state="TOKEN_READY"))
    result = client.resolve_submission_page("100", "001", date(2026, 9, 3))
    assert result.ok
    assert client.last_discovered_select_path == "/front/third/apps/seat/select"


def test_auto_discovered_context_refreshes_an_existing_auto_plan(tmp_path):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.select_params_json = json.dumps({"id": "100"})
    plan.select_context_source = "room_context"
    plan.select_context_path = "/front/apps/seat/select"
    client = type("Client", (), {
        "last_discovered_select_params": {"id": "100", "deptIdEnc": "fresh"},
        "last_parameter_source": "stored_auto_context",
        "last_discovered_select_path": "/front/third/apps/seat/select",
    })()
    service._persist_discovered_context(plan, client)
    assert plan.select_params == {"id": "100", "deptIdEnc": "fresh"}
    assert plan.select_context_source == "auto_context"
    assert plan.select_context_path == "/front/third/apps/seat/select"
    db.close()


def _test_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _make_plan(session_factory, account_id=1, seats=("001", "002", "003"), max_attempts=3):
    db = session_factory()
    account = db.get(Account, account_id)
    if account is None:
        account = Account(id=account_id, name=f"account-{account_id}", username=f"user-{account_id}", password_blob=b"test")
    plan = ReservationPlan(account=account, name=f"plan-{account_id}", room_id="100", start_time="08:00", end_time="09:00", run_time="06:00", day_offset=1, weekdays_json='["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]', max_attempts=max_attempts, enabled=True)
    plan.seats = [PlanSeat(seat_num=seat, priority=index) for index, seat in enumerate(seats)]
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()
    return plan_id


def test_candidate_seats_are_submitted_once_in_priority_order(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    # A legacy high retry count cannot make a candidate submit twice.
    plan_id = _make_plan(factory, seats=("001", "002"), max_attempts=5)
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            calls.append(seat)
            raise service.ReservationError("SEAT_UNAVAILABLE", "occupied")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_wait_first_pass", lambda _: None)
    execute_plan(plan_id)
    assert calls == ["001", "002"]
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "FAILED"
    assert run.error_code == "SEAT_UNAVAILABLE"
    db.close()


def test_different_accounts_can_run_together_but_same_account_is_serial(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    first_plan = _make_plan(factory, account_id=1, seats=("001",))
    same_account_plan = _make_plan(factory, account_id=1, seats=("003",))
    second_plan = _make_plan(factory, account_id=2, seats=("002",))
    active_by_account, max_active_by_account = {}, {}
    guard = threading.Lock()

    class FakeClient:
        def __init__(self, username, *args, **kwargs): self.account = username
        def login(self): pass
        def submit_once(self, *args, **kwargs):
            with guard:
                active_by_account[self.account] = active_by_account.get(self.account, 0) + 1
                max_active_by_account[self.account] = max(max_active_by_account.get(self.account, 0), active_by_account[self.account])
            time.sleep(.05)
            with guard: active_by_account[self.account] -= 1
            return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    threads = [threading.Thread(target=execute_plan, args=(first_plan,)), threading.Thread(target=execute_plan, args=(same_account_plan,)), threading.Thread(target=execute_plan, args=(second_plan,))]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert max_active_by_account == {"user-1": 1, "user-2": 1}


def test_only_exact_recent_success_is_skipped(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",))
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        def submit_once(self, *args, **kwargs): calls.append("submit"); return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_verify_submission_with_poll", lambda *_: ({"id": "test"}, ""))
    service.execute_plan(plan_id)
    service.execute_plan(plan_id)
    assert calls == ["submit"]
    db = factory()
    run = db.scalar(__import__("sqlalchemy").select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("SKIPPED", "RECENT_DUPLICATE")
    db.close()


def test_old_or_legacy_success_requires_confirmation_not_a_permanent_block(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",))
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        def submit_once(self, *args, **kwargs): calls.append("submit"); return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_verify_submission_with_poll", lambda *_: ({"id": "test"}, ""))
    db = factory()
    db.add(ReservationRun(account_id=1, target_date=(service.now_shanghai().date() + timedelta(days=1)).isoformat(), trigger="manual", status="SUCCESS", message="legacy"))
    db.commit()
    db.close()
    service.execute_plan(plan_id)
    assert calls == []  # Old records are a warning, not an automatic permanent block.
    db = factory()
    legacy_warning = db.scalar(__import__("sqlalchemy").select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (legacy_warning.status, legacy_warning.error_code) == ("NEEDS_VERIFICATION", "LEGACY_SUCCESS")
    db.close()
    service.execute_plan(plan_id, override_duplicate=True)
    assert calls == ["submit"]
    db = factory()
    successful = db.scalar(__import__("sqlalchemy").select(ReservationRun).where(ReservationRun.status == "SUCCESS").order_by(ReservationRun.id.desc()))
    successful.finished_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=6)
    db.commit()
    db.close()
    service.execute_plan(plan_id)
    db = factory()
    pending_confirmation = db.scalar(__import__("sqlalchemy").select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (pending_confirmation.status, pending_confirmation.error_code) == ("NEEDS_VERIFICATION", "POSSIBLE_DUPLICATE")
    db.close()
    service.execute_plan(plan_id, override_duplicate=True)
    assert calls == ["submit", "submit"]


def test_successful_probe_never_blocks_a_real_reservation(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",))
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        def probe(self, *args): return type("Probe", (), {"captcha_type": "none"})()
        def submit_once(self, *args, **kwargs): calls.append("submit"); return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_verify_submission_with_poll", lambda *_: ({"id": "test"}, ""))
    service.execute_plan(plan_id, trigger="probe", probe_only=True)
    service.execute_plan(plan_id)
    assert calls == ["submit"]


def test_probe_records_each_candidate_instead_of_stopping_at_first_nonfatal_error(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001", "002"))

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        last_parameter_source = "room_context"
        last_discovered_select_params = {"id": "100"}
        def resolve_submission_page(self, room, seat, day, select_params=None, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "token ready", source="target_day")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    service.execute_plan(plan_id, trigger="probe", probe_only=True)
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("PROBE_DONE", "PROBE_COMPLETE")
    assert "001" in run.message and "002" in run.message
    db.close()


def test_probe_before_opening_records_waiting_instead_of_failure(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001", "002"))
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.run_time = "19:00"
    plan.day_offset = 2
    db.commit()
    db.close()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        last_parameter_source = ""
        last_discovered_select_params = None
        def resolve_submission_page(self, *args, **kwargs):
            assert kwargs["require_target_day"] is True
            raise ReservationError("TARGET_DAY_NOT_OPEN", "not open")

    current = datetime(2026, 9, 2, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "now_shanghai", lambda: current)

    service.execute_plan(plan_id, trigger="probe", probe_only=True)

    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("PROBE_DONE", "PROBE_WAITING_OPEN")
    assert run.target_date == "2026-09-04"
    assert [item["page_state"] for item in run.probe_results] == ["TARGET_DAY_NOT_OPEN", "TARGET_DAY_NOT_OPEN"]
    assert all(item["ok"] for item in run.probe_results)
    assert "2026-09-02 19:00" in run.message
    db.close()


def test_parse_select_url_extracts_params_and_drops_day():
    url = "https://office.chaoxing.com/front/third/apps/seat/select?deptIdEnc=ABC123&fid=1234&pageToken=temporary&day=2026-09-02"
    # Param names must keep their original casing: the server is case-sensitive.
    assert parse_select_url(url) == {"deptIdEnc": "ABC123", "fid": "1234"}
    with pytest.raises(ValueError):
        parse_select_url("https://example.com/front/third/apps/seat/select?a=1")
    with pytest.raises(ValueError):
        parse_select_url("https://office.chaoxing.com/front/third/apps/seat/code?id=1&seatNum=2")
    with pytest.raises(ValueError):
        parse_select_url("https://office.chaoxing.com/front/third/apps/seat/select?day=2026-09-02")


def test_redirect_classification_distinguishes_login_security_and_risk():
    classify = ChaoxingClient._classify_redirect
    assert classify(302, "https://passport2.chaoxing.com/mlogin").code == "LOGIN_REQUIRED"
    assert classify(303, "https://passport2.chaoxing.com/wlogin1").code == "LOGIN_REQUIRED"
    assert classify(302, "https://captcha.chaoxing.com/captcha/get").code == "SECURITY_CHALLENGE"
    assert classify(301, "https://office.chaoxing.com/front/other").code == "BLOCKED_BY_RISK"


def test_request_without_follow_rejects_redirects():
    client = ChaoxingClient("u", "p")

    class FakeSession:
        def request(self, method, url, **kwargs):
            response = requests.Response()
            response.status_code = 302
            response.headers["Location"] = "https://captcha.chaoxing.com/captcha/get"
            response.url = url
            return response

    client.session = FakeSession()
    with pytest.raises(ReservationError) as exc_info:
        client._request("GET", "https://office.chaoxing.com/front/third/apps/seat/code")
    assert exc_info.value.code == "SECURITY_CHALLENGE"


def test_fetch_target_day_page_sends_day_and_school_params(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.logged_in = True
    captured = {}
    page_html = '<input id="submit_enc" value="tok"><input id="algorithm" value="algo">'

    class FakeResponse:
        status_code = 200
        url = ChaoxingClient.select_page_url
        headers = {}
        content = page_html.encode()
        text = page_html

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, params=kwargs.get("params"))
        return FakeResponse()

    monkeypatch.setattr(client, "_request", fake_request)
    result = client.fetch_target_day_page(date(2026, 9, 2), {"deptIdEnc": "ABC", "fid": "7"})
    assert captured["url"] == ChaoxingClient.select_page_url
    assert captured["params"]["day"] == "2026-09-02"
    assert captured["params"]["deptIdEnc"] == "ABC"
    assert result.ok
    assert result.token == "tok"
    assert result.source == "target_day"


def test_fetch_target_day_page_classifies_platform_error_route_as_not_open(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.logged_in = True
    response = type(
        "Response",
        (),
        {
            "status_code": 200,
            "url": "https://office.chaoxing.com/front/apps/reserve/error/code/500",
            "headers": {},
            "content": b"error",
            "text": "error",
        },
    )()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: response)

    result = client.fetch_target_day_page(date(2026, 9, 4), {"id": "100"})

    assert not result.ok
    assert result.page_state == "TARGET_DAY_NOT_OPEN"
    assert result.source == "target_day"


def test_strict_target_day_probe_does_not_use_current_page_token(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.logged_in = True
    current_html = '<input id="submit_enc" value="current-token"><input id="algorithm" value="algo">'
    current = type(
        "Response",
        (),
        {
            "url": "https://office.chaoxing.com/front/third/apps/seat/codemyselfuse?id=100&seatNum=001",
            "content": current_html.encode(),
            "text": current_html,
        },
    )()
    monkeypatch.setattr(client, "_current_page", lambda *_: current)
    monkeypatch.setattr(
        client,
        "fetch_target_day_page",
        lambda *_args, **_kwargs: ProbeResult(False, page_state="TARGET_DAY_NOT_OPEN", source="target_day"),
    )

    with pytest.raises(ReservationError) as exc_info:
        client.resolve_submission_page("100", "001", date(2026, 9, 4), {"id": "100"}, require_target_day=True)

    assert exc_info.value.code == "TARGET_DAY_NOT_OPEN"


def test_resolve_submission_uses_room_context_when_current_page_is_occupied(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.logged_in = True
    occupied = "该座位已被别人预约，等待用户签到"
    response = type("Response", (), {"content": occupied.encode(), "text": occupied, "url": "https://office.chaoxing.com/front/third/apps/seat/code?id=100&seatNum=001"})()
    calls = []

    monkeypatch.setattr(client, "_current_page", lambda *_: response)

    def fake_target(day, params, select_path=None):
        calls.append((day.isoformat(), dict(params)))
        return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

    monkeypatch.setattr(client, "fetch_target_day_page", fake_target)
    result = client.resolve_submission_page("100", "001", date(2026, 9, 3))
    assert result.ok
    assert client.last_parameter_source == "room_context"
    assert client.last_discovered_select_params == {"id": "100"}
    assert calls == [("2026-09-03", {"id": "100"})]


def test_unavailable_context_never_reaches_submit(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.logged_in = True
    occupied = "该座位已被别人预约，等待用户签到"
    response = type("Response", (), {"content": occupied.encode(), "text": occupied, "url": "https://office.chaoxing.com/front/third/apps/seat/code?id=100&seatNum=001"})()
    monkeypatch.setattr(client, "_current_page", lambda *_: response)
    monkeypatch.setattr(client, "fetch_target_day_page", lambda *_: ProbeResult(False, page_state="TOKEN_MISSING", message="no token", source="target_day"))
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3))
    assert exc_info.value.code == "TARGET_CONTEXT_UNAVAILABLE"
    assert not client.last_submitted


def test_login_non_json_response_is_reported(monkeypatch):
    client = ChaoxingClient("u", "p")

    class BadResponse:
        def json(self):
            raise ValueError("not json")

    class GoodResponse:
        def json(self):
            return {"status": True}

    def fake_request(method, url, **kwargs):
        return BadResponse() if "fanyalogin" in url else GoodResponse()

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(ReservationError) as exc_info:
        client.login()
    assert exc_info.value.code == "LOGIN_RESPONSE"


def test_probe_with_select_params_checks_target_day_once_and_reports_each_seat(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001", "002"))
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.select_params_json = json.dumps({"deptIdEnc": "ABC"})
    db.commit()
    db.close()
    fetches = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        last_parameter_source = "manual_context"
        last_discovered_select_params = None
        def resolve_submission_page(self, room, seat, day, select_params=None, **kwargs):
            fetches.append((day.isoformat(), dict(select_params or {})))
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    service.execute_plan(plan_id, trigger="probe", probe_only=True)
    assert len(fetches) == 1
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("PROBE_DONE", "PROBE_COMPLETE")
    results = json.loads(run.probe_results_json)
    assert [item["seat"] for item in results] == ["001", "002"]
    assert all(item["ok"] for item in results)
    db.close()


def test_candidates_submit_once_and_select_params_are_passed(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001", "002"), max_attempts=5)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.select_params_json = json.dumps({"deptIdEnc": "ABC"})
    db.commit()
    db.close()
    submits = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self): pass
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            submits.append((seat, select_params))
            raise service.ReservationError("SEAT_UNAVAILABLE", "occupied")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_wait_first_pass", lambda _: None)
    service.execute_plan(plan_id)
    assert [seat for seat, _ in submits] == ["001", "002"]
    assert all(params == {"deptIdEnc": "ABC"} for _, params in submits)
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("FAILED", "SEAT_UNAVAILABLE")
    assert len(run.attempt_details) == 2
    db.close()


def test_stale_runs_are_marked_for_verification_not_replayed(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    db = factory()
    db.add(ReservationRun(trigger="scheduled", status="RUNNING", message="started", started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=90)))
    db.commit()
    db.close()
    monkeypatch.setattr(service, "SessionLocal", factory)
    assert service.recover_interrupted_runs() == 1
    db = factory()
    run = db.scalar(select(ReservationRun))
    assert (run.status, run.error_code) == ("NEEDS_VERIFICATION", "INTERRUPTED_NEEDS_VERIFICATION")
    db.close()


def test_v5_database_migrates_context_and_attempt_audit_columns(tmp_path, monkeypatch):
    import app.db as db_module
    import sqlite3

    database_path = tmp_path / "v5.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE reservation_plans (id INTEGER PRIMARY KEY, select_params_json TEXT);
        CREATE TABLE reservation_runs (id INTEGER PRIMARY KEY, probe_results_json TEXT);
        PRAGMA user_version=5;
        """
    )
    connection.close()
    monkeypatch.setattr(db_module, "DATABASE_PATH", database_path)
    monkeypatch.setattr(db_module, "DATA_DIR", tmp_path)
    db_module._migrate_database()
    connection = sqlite3.connect(database_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(reservation_plans)")}
    run_columns = {row[1] for row in connection.execute("PRAGMA table_info(reservation_runs)")}
    assert {"select_context_source", "select_context_path", "select_context_checked_at"} <= plan_columns
    assert {"parameter_source", "attempt_details_json", "request_snapshot_json"} <= run_columns
    settings_columns = {row[1] for row in connection.execute("PRAGMA table_info(app_settings)")}
    assert {"key", "value"} <= settings_columns
    connection.close()


def test_migration_v8_normalizes_legacy_absolute_context_path(tmp_path, monkeypatch):
    import app.db as db_module
    import sqlite3

    database_path = tmp_path / "v8.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE reservation_plans (id INTEGER PRIMARY KEY, select_context_path TEXT);
        INSERT INTO reservation_plans (id, select_context_path)
        VALUES (1, 'https://office.chaoxing.com/front/third/apps/seat/select');
        PRAGMA user_version=8;
        """
    )
    connection.close()
    monkeypatch.setattr(db_module, "DATABASE_PATH", database_path)
    monkeypatch.setattr(db_module, "DATA_DIR", tmp_path)
    db_module._migrate_database()
    connection = sqlite3.connect(database_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    assert connection.execute("SELECT select_context_path FROM reservation_plans WHERE id=1").fetchone()[0] == "/front/third/apps/seat/select"
    connection.close()


def test_wait_between_attempts_is_at_least_two_seconds(monkeypatch):
    import app.service as service

    sleeps = []
    monkeypatch.setattr(service.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(service.random, "uniform", lambda low, high: 0.5)
    service._wait_between_attempts(time.monotonic() + 100)
    assert sleeps == [pytest.approx(2.5)]


def test_parser_is_attribute_order_independent_and_supports_chaoxing_single_submit_enc_shape():
    client = ChaoxingClient("u", "p")
    reordered = '<input value="tok" id="submit_enc"><input value="algo" id="algorithm">'
    result = client._parse_page(type("Response", (), {"content": reordered.encode(), "text": reordered})())
    assert (result.ok, result.token, result.algorithm) == (True, "tok", "algo")
    token_only = '<input id="submit_enc" value="tok">'
    result = client._parse_page(type("Response", (), {"content": token_only.encode(), "text": token_only})())
    assert (result.ok, result.page_state, result.algorithm) == (True, "TOKEN_READY", "tok")


def test_parser_does_not_treat_an_unrelated_slide_word_as_captcha():
    html = '<script>function slideshow(){}</script><input id="submit_enc" value="tok"><input id="algorithm" value="algo">'
    result = ChaoxingClient("u", "p")._parse_page(type("Response", (), {"content": html.encode(), "text": html})())
    assert (result.ok, result.captcha_type) == (True, "none")


def test_submit_normal_none_captcha_reaches_post_and_string_false_is_not_success(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.resolve_submission_page = lambda *args, **kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
    requests_seen = []

    class Response:
        def json(self):
            return {"success": "false", "msg": "预约失败"}

    def fake_request(method, url, **kwargs):
        requests_seen.append((method, url))
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3))
    assert requests_seen == [("POST", client.submit_url)]
    assert client.last_submitted
    assert exc_info.value.code == "SUBMIT_REJECTED"


def test_submit_timeout_is_outcome_unknown_and_never_retryable(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.resolve_submission_page = lambda *args, **kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(ReservationError("NETWORK_ERROR", "timeout")))
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3))
    assert (exc_info.value.code, client.last_submitted) == ("SUBMIT_OUTCOME_UNKNOWN", True)


def test_fatal_context_error_is_not_downgraded_to_missing_context(monkeypatch):
    client = ChaoxingClient("u", "p")
    monkeypatch.setattr(client, "fetch_target_day_page", lambda *args, **kwargs: (_ for _ in ()).throw(ReservationError("SECURITY_CHALLENGE", "risk")))
    monkeypatch.setattr(client, "_current_page", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not continue after security challenge")))
    with pytest.raises(ReservationError) as exc_info:
        client.resolve_submission_page("100", "001", date(2026, 9, 3), {"id": "100"})
    assert exc_info.value.code == "SECURITY_CHALLENGE"


def test_select_context_keeps_legacy_select_path(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.logged_in = True
    html = '<a href="/front/apps/seat/select?id=100&deptIdEnc=ABC">seat</a>'
    response = type("Response", (), {"content": html.encode(), "text": html, "url": "https://office.chaoxing.com/front/third/apps/seat/code?id=100&seatNum=001"})()
    context = client._select_context_from_response(response, "100")
    assert context == ({"id": "100", "deptIdEnc": "ABC"}, "/front/apps/seat/select")
    captured = {}
    page = '<input id="submit_enc" value="tok"><input id="algorithm" value="algo">'
    monkeypatch.setattr(client, "_request", lambda method, url, **kwargs: captured.update(url=url) or type("Response", (), {"content": page.encode(), "text": page, "url": url})())
    client.fetch_target_day_page(date(2026, 9, 3), *context)
    assert captured["url"].endswith("/front/apps/seat/select")


def test_plan_input_discards_ephemeral_params_and_rejects_bad_context_path():
    plan = PlanIn(account_id=1, name="x", room_id="1", seats=["1"], start_time="08:00", end_time="09:00", select_params={"id": "1", "token": "t", "captcha": "c"})
    assert plan.select_params == {"id": "1"}
    with pytest.raises(ValueError):
        PlanIn(account_id=1, name="x", room_id="1", seats=["1"], start_time="08:00", end_time="09:00", select_context_path="/evil")
    assert parse_select_context_url("https://office.chaoxing.com/front/apps/seat/select?id=1")[1] == "/front/apps/seat/select"


def test_queued_run_uses_immutable_request_snapshot(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    run = service._run_snapshot(plan, "manual")
    db.add(run)
    db.commit()
    run_id = run.id
    plan.room_id, plan.start_time, plan.end_time = "200", "10:00", "11:00"
    plan.seats.clear()
    plan.seats.append(PlanSeat(seat_num="002", priority=0))
    db.commit()
    db.close()
    submitted = []

    class FakeClient:
        def __init__(self, *args, **kwargs): self.last_parameter_source = "fake"; self.last_submitted = True; self.last_discovered_select_params = None
        def login(self): pass
        def submit_once(self, room, seat, start, end, day, **kwargs): submitted.append((room, seat, start, end)); return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_verify_submission_with_poll", lambda *_: ({"id": "test"}, ""))
    service.execute_plan(plan_id, run_id=run_id)
    db = factory()
    saved = db.get(ReservationRun, run_id)
    assert submitted == [("100", "001", "08:00", "09:00")]
    assert saved.candidate_seats == ["001"]
    db.close()


def test_manual_run_is_not_blocked_by_schedule_weekday(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    today = service.now_shanghai().strftime("%A")
    plan.weekdays_json = json.dumps(["Tuesday" if today != "Tuesday" else "Wednesday"])
    db.commit()
    db.close()
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): self.last_parameter_source = "fake"; self.last_submitted = True; self.last_discovered_select_params = None
        def login(self): pass
        def submit_once(self, *args, **kwargs): calls.append(True); return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_verify_submission_with_poll", lambda *_: ({"id": "test"}, ""))
    service.execute_plan(plan_id, trigger="manual")
    assert calls == [True]


def test_startup_records_missed_schedule_without_enqueuing_a_late_submit(tmp_path, monkeypatch):
    import app.scheduler as scheduler_module

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.run_time = "08:00"
    plan.weekdays_json = json.dumps(["Monday"])
    db.commit()
    db.close()
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    now = datetime(2026, 9, 7, 8, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert scheduler_module._enqueue_recently_missed_jobs(now) == 1
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "MISSED"
    assert run.error_code == "MISSED_OPENING_WINDOW"
    db.close()
    assert scheduler_module._enqueue_recently_missed_jobs(now) == 0


def test_probe_captcha_is_not_reported_as_ready_to_submit(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)

    class FakeClient:
        def __init__(self, *args, **kwargs): self.last_parameter_source = "room_context"; self.last_discovered_select_params = None
        def login(self): pass
        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "slider", "SLIDER_CAPTCHA", "captcha", source="target_day")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    service.execute_plan(plan_id, trigger="probe", probe_only=True)
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.probe_results[0]["ok"] is False
    assert "安全停止" in run.probe_results[0]["message"]
    db.close()


def _epoch(day: date, hour: int, minute: int) -> int:
    tz = ZoneInfo("Asia/Shanghai")
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp() * 1000)


def test_find_reservation_matches_room_day_and_overlap_only():
    day = date(2026, 9, 3)
    reservations = [
        {"roomId": 10713, "today": "2026-09-03", "seatNum": "097", "startTime": _epoch(day, 8, 0), "endTime": _epoch(day, 8, 30)},
        {"roomId": 10713, "today": "2026-09-02", "seatNum": "097", "startTime": _epoch(date(2026, 9, 2), 8, 0), "endTime": _epoch(date(2026, 9, 2), 8, 30)},
        {"roomId": 99999, "today": "2026-09-03", "seatNum": "097", "startTime": _epoch(day, 8, 0), "endTime": _epoch(day, 8, 30)},
    ]
    # exact overlap
    assert ChaoxingClient.find_reservation(reservations, day, "10713", "08:00", "08:30") is reservations[0]
    # adjacent slot does not overlap
    assert ChaoxingClient.find_reservation(reservations, day, "10713", "08:30", "09:00") is None
    # same time on a different day is not a match
    assert ChaoxingClient.find_reservation(reservations, date(2026, 9, 2), "10713", "08:00", "08:30") is reservations[1]
    # wrong room is not a match
    assert ChaoxingClient.find_reservation(reservations, day, "99999", "08:00", "08:30") is reservations[2]
    assert ChaoxingClient.find_reservation(reservations, day, "10713", "07:00", "08:10") is reservations[0]


def test_fetch_reservations_uses_current_reservations_not_recent_history(monkeypatch):
    client = ChaoxingClient("u", "p")
    client.logged_in = True
    client.last_fid_enc = "abc"

    class Response:
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload

    current = {"roomId": 1, "status": 0}
    cancelled_history = {"roomId": 2, "status": 7}
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: Response(
            {"success": True, "data": {"curReserves": [current], "nearReserves": [cancelled_history]}}
        ),
    )
    assert client.fetch_reservations() == [current]

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response({"success": False, "msg": "boom"}))
    with pytest.raises(ReservationError) as exc_info:
        client.fetch_reservations()
    assert exc_info.value.code == "VERIFY_UNAVAILABLE"

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response({"success": True, "data": {}}))
    with pytest.raises(ReservationError) as exc_info:
        client.fetch_reservations()
    assert exc_info.value.code == "VERIFY_UNAVAILABLE"


def test_duplicate_with_server_side_reservation_skips_without_submitting(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    submits = []
    day = service.now_shanghai().date() + timedelta(days=1)

    class FakeClient:
        def __init__(self, *args, **kwargs): self.last_parameter_source = "room_context"; self.last_discovered_select_params = None
        def login(self): pass
        def fetch_reservations(self, *args, **kwargs):
            return [{"roomId": 100, "today": day.isoformat(), "seatNum": "001", "startTime": _epoch(day, 8, 0), "endTime": _epoch(day, 9, 0)}]
        def submit_once(self, *args, **kwargs): submits.append(True); return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_verify_submission_with_poll", lambda *_: ({"id": "test"}, ""))
    service.execute_plan(plan_id)
    assert submits == [True]
    db = factory()
    first = db.scalar(select(ReservationRun).where(ReservationRun.status == "SUCCESS"))
    first.finished_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    db.commit()
    db.close()
    service.execute_plan(plan_id)
    assert submits == [True]
    db = factory()
    latest = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (latest.status, latest.error_code) == ("SKIPPED", "ALREADY_BOOKED_ON_SERVER")
    assert "超星端已存在本场次预约" in latest.message
    db.close()


def test_duplicate_absent_on_server_proceeds_with_reservation(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    submits = []

    class FakeClient:
        def __init__(self, *args, **kwargs): self.last_parameter_source = "room_context"; self.last_discovered_select_params = None
        def login(self): pass
        def fetch_reservations(self, *args, **kwargs): return []
        def submit_once(self, *args, **kwargs): submits.append(True); return "ok"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "_verify_submission_with_poll", lambda *_: ({"id": "test"}, ""))
    service.execute_plan(plan_id)
    assert submits == [True]
    db = factory()
    first = db.scalar(select(ReservationRun).where(ReservationRun.status == "SUCCESS"))
    first.finished_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    db.commit()
    db.close()
    service.execute_plan(plan_id)
    # The local success is stale on the server side, so the run re-books it.
    assert submits == [True, True]
    db = factory()
    latest = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert latest.status == "SUCCESS"
    assert latest.selected_seat == "001"
    db.close()


def test_fire_time_of_day_leads_by_thirty_seconds_with_midnight_clamp():
    from app.scheduler import _fire_time_of_day, LEAD_SECONDS

    assert _fire_time_of_day("07:30") == (7, 29, 30)
    assert _fire_time_of_day("00:10") == (0, 9, 30)
    # Inside the lead window after midnight we fire exactly on the minute
    # instead of shifting the weekday to the previous day.
    assert _fire_time_of_day("00:00") == (0, 0, 0)
    assert LEAD_SECONDS == 30


def test_submit_once_with_pre_resolved_probe_skips_page_resolution(monkeypatch):
    client = ChaoxingClient("u", "p")

    def fail_resolve(*args, **kwargs):
        raise AssertionError("pre-resolved submit must not re-fetch the page")

    monkeypatch.setattr(client, "resolve_submission_page", fail_resolve)
    pre = ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

    class Response:
        def json(self):
            return {"success": True, "msg": "预约成功"}

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response())
    message = client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3), pre_resolved=pre)
    assert message == "预约成功"
    assert client.last_submitted


def test_first_pass_seat_switch_is_fast_but_jittered(monkeypatch):
    import app.service as service

    sleeps = []
    monkeypatch.setattr(service.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(service.random, "uniform", lambda low, high: low)
    service._wait_first_pass(time.monotonic() + 100)
    assert sleeps == [service.FIRST_PASS_WAIT_RANGE[0]]


def test_submit_outcome_unknown_is_auto_verified_on_server(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    day = service.now_shanghai().date() + timedelta(days=1)

    class FakeClient:
        def __init__(self, *args, **kwargs): self.last_parameter_source = "room_context"; self.last_discovered_select_params = None
        def login(self): pass
        def fetch_reservations(self, *args, **kwargs):
            return [{"roomId": 100, "today": day.isoformat(), "seatNum": "001", "startTime": _epoch(day, 8, 0), "endTime": _epoch(day, 9, 0)}]
        def submit_once(self, *args, **kwargs):
            self.last_submitted = True
            raise service.ReservationError("SUBMIT_OUTCOME_UNKNOWN", "提交请求已发出，但未能确认超星端结果")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    service.execute_plan(plan_id)
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "SUCCESS"
    assert "经超星端核实预约已生效" in run.message
    db.close()


def test_notify_settings_roundtrip_and_disabled_dispatch(tmp_path, monkeypatch):
    import app.notify as notify

    factory = _test_session_factory(tmp_path)
    monkeypatch.setattr(notify, "SessionLocal", factory)
    assert notify.load_settings() == {"notify_type": "none", "notify_key_configured": False}
    notify.save_settings("serverchan", " SCT123456 ")
    assert notify.load_settings() == {"notify_type": "serverchan", "notify_key_configured": True}
    db = factory()
    assert db.get(AppSetting, "notify_key").value != "SCT123456"
    db.close()
    notify.save_settings("serverchan", "")
    assert notify.load_settings() == {"notify_type": "serverchan", "notify_key_configured": True}
    with pytest.raises(ValueError):
        notify.save_settings("sms", "x")
    notify.save_settings("none", "")
    # No channel configured: delivery reports why without raising.
    assert notify.send("t", "b") == "未配置通知渠道"


def test_local_main_does_not_expose_legacy_submit_entrypoint():
    import main

    assert callable(main.main)
    assert not hasattr(main, "debug")
    assert not hasattr(main, "login_and_reserve")
