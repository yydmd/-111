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


def test_candidate_seats_each_get_exactly_one_shot(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    # A confirmed-unavailable seat is never retried: with two candidates the
    # budget buys one shot each, and the run stops once both are dead.
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
    execute_plan(plan_id)
    assert calls == ["001", "002"]
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "FAILED"
    assert run.error_code == "SEAT_UNAVAILABLE"
    assert "座位 001" in run.message and "座位 002" in run.message
    assert len(run.attempt_details) == 2
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


def test_redirect_classification_distinguishes_login_security_and_unknown():
    classify = ChaoxingClient._classify_redirect
    assert classify(302, "https://passport2.chaoxing.com/mlogin").code == "LOGIN_REQUIRED"
    assert classify(303, "https://passport2.chaoxing.com/wlogin1").code == "LOGIN_REQUIRED"
    assert classify(302, "https://captcha.chaoxing.com/captcha/get").code == "SECURITY_CHALLENGE"
    # An unrecognised hop is not proof of risk control (2026-09-03 audit): it
    # is usually a benign session/page jump, so it must stay retryable.
    assert classify(301, "https://office.chaoxing.com/front/other").code == "HTTP_REDIRECT"
    from app.service import _RETRYABLE_CODES

    assert "HTTP_REDIRECT" in _RETRYABLE_CODES


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


def test_submit_rounds_are_clamped_to_six_and_select_params_are_passed(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001", "002"), max_attempts=8)
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
    service.execute_plan(plan_id)
    # Distinct seats only: no repeat of a confirmed-dead candidate.
    assert [seat for seat, _ in submits] == ["001", "002"]
    assert all(params == {"deptIdEnc": "ABC"} for _, params in submits)
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("FAILED", "SEAT_UNAVAILABLE")
    assert "钳制为 6" in run.message
    assert len(run.attempt_details) == 2
    db.close()


def test_parallel_opening_shot_records_racers_and_first_winner(tmp_path, monkeypatch):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001", "002"), max_attempts=3)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def login(self): pass
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self): return FakeClient()
        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")
        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            self.last_submitted = True
            if seat == "001":
                raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")
            return "预约成功"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "SUCCESS"
    assert run.selected_seat == "002"
    assert "预约成功" in run.message
    # First winner stops the rest: a loser may be cancelled before its submit,
    # so the audit must contain the winner and only known racers.
    seats_audited = {item["seat"] for item in run.attempt_details}
    assert "002" in seats_audited
    assert seats_audited <= {"001", "002"}
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
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
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
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    assert connection.execute("SELECT select_context_path FROM reservation_plans WHERE id=1").fetchone()[0] == "/front/third/apps/seat/select"
    connection.close()


def test_wait_between_attempts_is_short_and_jittered(monkeypatch):
    import app.service as service

    sleeps = []
    monkeypatch.setattr(service.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(service.random, "uniform", lambda low, high: 0.3)
    service._wait_between_attempts(time.monotonic() + 100)
    assert sleeps == [pytest.approx(0.3)]
    assert service.RETRY_WAIT_RANGE[0] <= 0.3 <= service.RETRY_WAIT_RANGE[1]


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
    service.execute_plan(plan_id, trigger="manual")
    assert calls == [True]


def test_startup_catchup_enqueues_once_for_a_recently_missed_schedule(tmp_path, monkeypatch):
    import app.scheduler as scheduler_module

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.run_time = "08:00"
    plan.weekdays_json = json.dumps(["Monday"])
    db.commit()
    db.close()
    queued = []
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    monkeypatch.setattr(scheduler_module, "enqueue_plan", lambda plan_id, trigger: queued.append((plan_id, trigger)) or 1)
    now = datetime(2026, 9, 7, 8, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert scheduler_module._enqueue_recently_missed_jobs(now) == 1
    assert queued == [(plan_id, "scheduled_catchup")]
    db = factory()
    db.add(ReservationRun(plan_id=plan_id, target_date="2026-09-08", trigger="scheduled", status="SUCCESS", message="done"))
    db.commit()
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


def test_legacy_debug_loads_its_runtime_in_non_action_mode(monkeypatch):
    import main

    calls = []

    class FakeReserve:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))
            self.requests = type("Requests", (), {"headers": {}})()

        def get_login_status(self):
            calls.append(("status",))

        def login(self, username, password):
            calls.append(("login", username, password))

        def submit(self, times, room_id, seats, action):
            calls.append(("submit", times, room_id, seats, action))
            return True

    monkeypatch.setattr(main, "_legacy_runtime", lambda: (FakeReserve, lambda _action: ("", "")))
    monkeypatch.setattr(main, "get_current_dayofweek", lambda _action: "Monday")
    main.debug(
        [{"username": "u", "password": "p", "time": ["08:00", "09:00"], "roomid": "100", "seatid": "001", "daysofweek": ["Monday"]}],
        action=False,
    )
    assert ("submit", ["08:00", "09:00"], "100", ["001"], False) in calls


def test_manual_override_context_survives_auto_discovery_fallback(tmp_path):
    """Bug 1 回归：手动保存的高级覆盖链接不能被自动发现的结果覆写。"""
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.select_params_json = json.dumps({"id": "100", "deptIdEnc": "MANUAL"})
    plan.select_context_source = "advanced_manual"
    plan.select_context_path = "/front/third/apps/seat/select"
    db.commit()
    client = type("Client", (), {
        "last_discovered_select_params": {"id": "100"},
        "last_parameter_source": "auto_context",
        "last_discovered_select_path": "/front/apps/seat/select",
    })()
    service._persist_discovered_context(plan, client)
    assert plan.select_params == {"id": "100", "deptIdEnc": "MANUAL"}
    assert plan.select_context_source == "advanced_manual"
    assert plan.select_context_path == "/front/third/apps/seat/select"
    assert plan.select_context_checked_at is not None
    db.close()


def _volley_test_factory(submits, code, message):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self):
            pass

        def browse(self, *args, **kwargs):
            pass

        def clone_authenticated(self):
            return FakeClient()

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            submits.append(seat)
            self.last_submitted = True
            raise ReservationError(code, message)

    return FakeClient


def _run_volley_plan(tmp_path, monkeypatch, fake_client, seats, max_attempts):
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=seats, max_attempts=max_attempts)
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", fake_client)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    result = (run.status, run.error_code)
    db.close()
    return result


def test_volley_exhaustion_reports_real_failure_code(tmp_path, monkeypatch):
    """Bug 2 回归：并行开抢预算耗尽时保留真实失败码，不再误标为座位不可预约。"""
    submits: list[str] = []
    fake = _volley_test_factory(submits, "BLOCKED_BY_RISK", "平台限流（HTTP 429）")
    status, error_code = _run_volley_plan(tmp_path, monkeypatch, fake, seats=("001", "002", "003"), max_attempts=3)
    # 父会话首枪 001 + 三个 racer 各一发。
    assert sorted(submits) == ["001", "001", "002", "003"]
    assert (status, error_code) == ("BLOCKED_BY_RISK", "BLOCKED_BY_RISK")


def test_volley_outcome_unknown_stops_instead_of_resubmitting(tmp_path, monkeypatch):
    """Bug 2 回归：响应丢失的并行提交不得由串行兜底重复提交。"""
    import app.service as service

    submits: list[str] = []
    verify_calls: list[str] = []

    def fake_verify(client, request_values, target_day):
        verify_calls.append("called")
        return "unavailable", "核实失败（测试）"

    monkeypatch.setattr(service, "_verify_duplicate_on_server", fake_verify)
    fake = _volley_test_factory(submits, "SUBMIT_OUTCOME_UNKNOWN", "提交请求已发出，但未能确认超星端结果")
    status, error_code = _run_volley_plan(tmp_path, monkeypatch, fake, seats=("001", "002", "003"), max_attempts=3)
    # 父会话首枪 + 每个 racer 只提交一次；串行兜底绝不再提交同一个时段。
    assert sorted(submits) == ["001", "001", "002", "003"]
    assert verify_calls == ["called"]
    assert (status, error_code) == ("NEEDS_VERIFICATION", "SUBMIT_OUTCOME_UNKNOWN")


def test_volley_non_retryable_failure_stops_serial_fallback(tmp_path, monkeypatch):
    """修复回归：并行阶段遭遇风控/验证码等非重试失败后，串行兜底不得再次提交。"""
    submits: list[str] = []
    fake = _volley_test_factory(submits, "CAPTCHA_REQUIRED", "检测到滑块验证码，需要人工处理")
    status, error_code = _run_volley_plan(tmp_path, monkeypatch, fake, seats=("001", "002"), max_attempts=6)
    assert sorted(submits) == ["001", "001", "002"]  # 父会话首枪 + 两个 racer，串行兜底不再提交
    assert (status, error_code) == ("NEEDS_VERIFICATION", "CAPTCHA_REQUIRED")


def test_volley_retryable_failures_still_use_serial_fallback(tmp_path, monkeypatch):
    """修复回归：可重试码（座位占用/参数未取得）仍按原设计进入串行轮转。"""
    import app.service as service

    monkeypatch.setattr(service.time, "sleep", lambda *_: None)
    submits: list[str] = []
    codes = {"001": "SEAT_UNAVAILABLE", "002": "TARGET_CONTEXT_UNAVAILABLE"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self):
            pass

        def browse(self, *args, **kwargs):
            pass

        def clone_authenticated(self):
            return FakeClient()

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            submits.append(seat)
            self.last_submitted = True
            raise ReservationError(codes[seat], "occupied" if seat == "001" else "未取得目标日期预约参数")

    status, error_code = _run_volley_plan(tmp_path, monkeypatch, FakeClient, seats=("001", "002"), max_attempts=6)
    # 父会枪确认 001 占用后其 racer 不再补发（复审修复）；002 继续消耗剩余预算。
    assert submits.count("001") == 1
    assert submits.count("002") > 1
    assert (status, error_code) == ("FAILED", "TARGET_CONTEXT_UNAVAILABLE")


def test_volley_multiple_successes_are_reconciled_on_server(tmp_path, monkeypatch):
    """修复回归：多个座位并行提交都成功时，收尾核对超星端实际持有的预约。"""
    tz = ZoneInfo("Asia/Shanghai")
    target = (datetime.now(tz).date() + timedelta(days=1)).isoformat()

    def epoch_ms(hour: int, minute: int) -> int:
        moment = datetime.fromisoformat(f"{target}T{hour:02d}:{minute:02d}:00").replace(tzinfo=tz)
        return int(moment.timestamp() * 1000)

    reservations = [
        {"roomId": "100", "today": target, "startTime": epoch_ms(8, 0), "endTime": epoch_ms(9, 0), "seatNum": seat}
        for seat in ("001", "002")
    ]
    submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self):
            pass

        def browse(self, *args, **kwargs):
            pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def fetch_reservations(self, select_params=None, select_path=None):
            return reservations

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if not self.is_clone:
                # 父会话首枪被 303 拒（与实战一致）；双成功竞速留给 racer。
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            # Simulate real POST latency: without it the first winner can set
            # the stop flag before the second racer passes its pre-submit
            # check, which would hide the multi-success race this test pins.
            time.sleep(0.02)
            submits.append(seat)
            return "预约成功"

    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001", "002"), max_attempts=2)
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert sorted(submits) == ["001", "002"]
    assert run.status == "SUCCESS"
    assert run.selected_seat in {"001", "002"}
    assert "同样返回成功" in run.message
    assert "手动取消多余的预约" in run.message
    # 父会枪的 303 + 两个 racer 的成功，全部进入审计。
    assert len(run.attempt_details) == 3
    db.close()


def test_input_fields_rejects_unrelated_values_as_algorithm_salt():
    """Bug 3 回归：歧义页面的无关输入值不得被当作签名盐。"""
    from app.chaoxing_client import _input_fields

    ambiguous = (
        '<input id="submit_enc" value="TOKEN123">'
        '<input name="keyword" value="搜索词">'
        '<input type="hidden" name="csrf" value="ABC">'
    )
    assert _input_fields(ambiguous) == ("TOKEN123", "")
    assert _input_fields('<input id="submit_enc" value="tok">') == ("tok", "tok")
    assert _input_fields('<input id="algorithm" value="algo"><input id="submit_enc" value="tok">') == ("tok", "algo")


def test_select_url_discovery_keeps_query_values_containing_s():
    """Bug 4 回归：页面文本中发现的选座链接，查询串含 s/S 时不得被截断。"""
    html = "跳转 https://office.chaoxing.com/front/third/apps/seat/select?id=100&deptIdEnc=abcSdef 继续"
    response = type("Response", (), {
        "content": html.encode(),
        "text": html,
        "url": "https://office.chaoxing.com/front/third/apps/seat/code?id=100&seatNum=001",
    })()
    discovered = ChaoxingClient._select_context_from_response(response, "100")
    assert discovered is not None
    assert discovered[0]["deptIdEnc"] == "abcSdef"


def test_submit_once_past_deadline_is_not_audited_as_submitted():
    """次要修复回归：超限后未发出的请求不得记为已提交。"""
    client = ChaoxingClient("u", "p", deadline=time.monotonic() - 1)
    probe = ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3), pre_resolved=probe)
    assert exc_info.value.code == "DEADLINE_EXCEEDED"
    assert client.last_submitted is False


def test_plan_save_refreshes_jobs_and_runs_recent_miss_catchup(tmp_path, monkeypatch):
    """修复回归：保存计划后也要做“刚错过触发点”的补跑检查，而不是只排到明天。"""
    import app.web as web

    factory = _test_session_factory(tmp_path)
    db = factory()
    db.add(Account(id=1, name="a", username="u", password_blob=b"x"))
    db.commit()
    calls = []
    monkeypatch.setattr(web, "refresh_jobs", lambda: calls.append("refresh"))
    monkeypatch.setattr(web, "_enqueue_recently_missed_jobs", lambda: calls.append("catchup"))
    payload = PlanIn(account_id=1, name="p", room_id="10713", seats=["097"], start_time="08:00", end_time="08:30")
    created = web.create_plan(payload, db)
    assert calls == ["refresh", "catchup"]
    # Partial PATCH payload, exactly what the toggle button now sends over
    # HTTP; the endpoint merges it onto the stored plan.
    web.patch_plan(created["id"], {"name": "p2"}, db)
    assert calls == ["refresh", "catchup", "refresh", "catchup"]
    db.close()


def test_submit_once_classifies_stale_page_as_token_stale(monkeypatch):
    """修复回归：平台 303（请刷新后再提交）是可恢复拒绝，不是终局风控。"""
    client = ChaoxingClient("u", "p")
    client.resolve_submission_page = lambda *args, **kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

    class Response:
        def json(self):
            return {"success": False, "msg": "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)"}

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response())
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3))
    assert exc_info.value.code == "TOKEN_STALE"
    assert "TOKEN_STALE" in __import__("app.service", fromlist=["_RETRYABLE_CODES"])._RETRYABLE_CODES


def test_volley_token_stale_falls_back_to_serial_refresh(tmp_path, monkeypatch):
    """修复回归：并行开抢遇到 303 时不终止，串行兜底刷新页面后重试可成功。"""
    import app.service as service

    submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.parent_calls = 0
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self):
            pass

        def browse(self, *args, **kwargs):
            pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            submits.append(seat)
            self.last_submitted = True
            if self.is_clone:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            self.parent_calls += 1
            if self.parent_calls == 1:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            return "预约成功"

    status, error_code = _run_volley_plan(tmp_path, monkeypatch, FakeClient, seats=("001", "002"), max_attempts=6)
    # 父会话首枪被 303 拒；两个 racer 各自愈重试 2 次（共 3 发/座，仍 303）；串行兜底刷新后成功。
    assert len(submits) == 8
    assert (status, error_code) == ("SUCCESS", None)


def _full_stale_volley_client(submits, stale_seats, main_submit):
    """Volley factory: every racer submit raises; the main client behaves per main_submit."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self):
            pass

        def browse(self, *args, **kwargs):
            pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            submits.append(seat)
            self.last_submitted = True
            if not self.is_clone:
                return main_submit(seat)
            if seat in stale_seats:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            raise ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")

    return FakeClient


def test_full_stale_volley_still_gets_refresh_retry(tmp_path, monkeypatch):
    """修复回归：整队 303（含 1 个座位占用）不得耗尽预算，串行刷新重试必须执行。

    生产事故：4 个候选座位并行开抢，3 个收到平台 303（要求刷新后重提）、
    1 个确认占用；默认 max_attempts=3 时旧逻辑 range(4, 4) 为空，一次刷新
    重试都没做就报“座位不可预约”。
    """
    import app.service as service

    monkeypatch.setattr(service.time, "sleep", lambda *_: None)
    submits: list[str] = []
    parent_calls = {"n": 0}

    def main_submit(seat):
        parent_calls["n"] += 1
        if parent_calls["n"] == 1:
            # 父会话首枪同样吃到 303（与实战一致），刷新后的串行重试才成功。
            raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
        return "预约成功"

    fake = _full_stale_volley_client(submits, stale_seats={"097", "098", "099"}, main_submit=main_submit)
    status, error_code = _run_volley_plan(tmp_path, monkeypatch, fake, seats=("097", "098", "099", "100"), max_attempts=3)
    # 父会枪 303(1) + 3 个 303 座位各 1+2 次自愈(9) + 占用座位 1 发 + 串行刷新成功(1) = 12。
    assert len(submits) == 12
    assert submits.count("100") == 1  # 确认占用的座位不自愈也不重试
    assert submits.count("097") == 5  # 父会枪 1 + 自愈 3 + 串行刷新成功 1
    assert (status, error_code) == ("SUCCESS", None)


def test_stale_exhaustion_reports_token_stale_not_seat_unavailable(tmp_path, monkeypatch):
    """修复回归：刷新重试仍全部 303 时，终局报告真实原因 TOKEN_STALE。

    旧逻辑取“最后一个结果”的错误码（座位占用），把 3 个可重试座位被 303
    拒绝的真实原因完全掩盖。
    """
    import app.service as service

    monkeypatch.setattr(service.time, "sleep", lambda *_: None)
    submits: list[str] = []

    def always_stale(_seat):
        raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")

    fake = _full_stale_volley_client(submits, stale_seats={"097", "098", "099"}, main_submit=always_stale)
    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097", "098", "099", "100"), max_attempts=3)
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", fake)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    # 串行兜底确实跑了刷新重试（提交数超过并行 volley 的 4 次）。
    assert len(submits) > 4
    assert (run.status, run.error_code) == ("FAILED", "TOKEN_STALE")
    assert "刷新后再提交" in run.message
    seats_audited = {item["seat"] for item in run.attempt_details}
    assert "097" in seats_audited  # 刷新重试被记录在审计里
    db.close()


def test_catchup_after_failed_run_still_enqueues(tmp_path, monkeypatch):
    """2026-09-03 01:29 事故回归：当天已有 FAILED 的同目标日定时 run 不得吞掉补跑。

    用户把 run_time 改晚并在唤醒时刻之后保存，cron 只能排到明天；此前
    唯一的兜底补跑被 (plan, target_date) 去重命中当天早些时候的 FAILED
    记录而静默跳过——计划当天从未执行。
    """
    import app.scheduler as scheduler_module

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("001",), max_attempts=1)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.run_time = "08:00"
    plan.day_offset = 1
    plan.weekdays_json = json.dumps(["Monday"])
    db.commit()
    db.close()
    db = factory()
    db.add(ReservationRun(plan_id=plan_id, account_id=1, target_date="2026-09-08", trigger="scheduled", status="FAILED", error_code="TOKEN_STALE", message="failed earlier today"))
    db.commit()
    db.close()
    queued = []
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    monkeypatch.setattr(scheduler_module, "enqueue_plan", lambda plan_id, trigger: queued.append((plan_id, trigger)) or 1)
    now = datetime(2026, 9, 7, 8, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert scheduler_module._enqueue_recently_missed_jobs(now) == 1
    assert queued == [(plan_id, "scheduled_catchup")]
    # 已成功的记录仍然阻塞补跑（当天真的订上了就没有再跑的意义）。
    db = factory()
    db.add(ReservationRun(plan_id=plan_id, account_id=1, target_date="2026-09-08", trigger="scheduled", status="SUCCESS", message="done"))
    db.commit()
    db.close()
    assert scheduler_module._enqueue_recently_missed_jobs(now) == 0


def test_patch_toggle_keeps_discovered_select_context(tmp_path, monkeypatch):
    """2026-09-03 审计回归：启停切换（部分 PATCH）不得清空已发现的选座参数。"""
    import app.web as web

    factory = _test_session_factory(tmp_path)
    db = factory()
    db.add(Account(id=1, name="a", username="u", password_blob=b"x"))
    db.commit()
    monkeypatch.setattr(web, "refresh_jobs", lambda: None)
    monkeypatch.setattr(web, "_enqueue_recently_missed_jobs", lambda: 0)
    payload = PlanIn(account_id=1, name="p", room_id="10713", seats=["097"], start_time="08:00", end_time="08:30")
    created = web.create_plan(payload, db)
    plan = db.get(ReservationPlan, created["id"])
    plan.select_params_json = json.dumps({"id": "10713"})
    plan.select_context_path = "/front/third/apps/seat/select"
    plan.select_context_source = "auto_context"
    plan.select_context_checked_at = datetime(2026, 9, 3, 1, 0)
    db.commit()

    web.patch_plan(created["id"], {"enabled": False}, db)
    plan = db.get(ReservationPlan, created["id"])
    assert plan.enabled is False
    assert plan.select_params == {"id": "10713"}
    assert plan.select_context_path == "/front/third/apps/seat/select"
    assert plan.select_context_source == "auto_context"
    assert plan.select_context_checked_at == datetime(2026, 9, 3, 1, 0)

    # 显式携带 select_params=null 的全量表单提交仍然清除参数（编辑表单语义）。
    web.patch_plan(created["id"], {"select_params": None}, db)
    plan = db.get(ReservationPlan, created["id"])
    assert plan.select_params is None
    assert plan.select_context_path is None
    assert plan.select_context_checked_at is None
    db.close()


def test_plan_rejects_run_time_before_half_past_midnight():
    """调度在 00:00–00:29 的 run_time 会提前唤醒长达半小时，超出 60 秒运行上限。"""
    import app.web as web

    base = {"account_id": 1, "name": "p", "room_id": "10713", "seats": ["097"], "start_time": "08:00", "end_time": "08:30"}
    with pytest.raises(Exception):
        web.PlanData.model_validate({**base, "run_time": "00:10"})
    accepted = web.PlanData.model_validate({**base, "run_time": "00:30"})
    assert accepted.run_time == "00:30"


def test_serial_refresh_retry_rotates_candidate_seats(tmp_path, monkeypatch):
    """2026-09-03 审计回归：整队 303 后，刷新重试必须轮换候选座位。

    旧的兜底循环永远选第一个未确认占用的座位，3 次刷新重试全部砸在
    097 上，098/099 一次也得不到。
    """
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097", "098", "099"), max_attempts=3)
    serial_submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self): pass

        def browse(self, *args, **kwargs): pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            self.last_submitted = True
            if not self.is_clone:
                serial_submits.append(seat)
            raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    # 首条是父会话首枪（097 被 303 拒，同样计入父会话提交），其后是串行轮换 097→098→099。
    assert serial_submits == ["097", "097", "098", "099"]
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("FAILED", "TOKEN_STALE")
    db.close()


def test_single_seat_stale_retry_refetches_page(tmp_path, monkeypatch):
    """2026-09-03 审计回归：单座位串行 303 后的重试必须重新取页。

    pre_resolved 只能搭第一次提交；被 303 拒绝后仍复用同一旧 token 提交
    是上次事故根因（token 太旧）的单座位残留路径。
    """
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097",), max_attempts=3)
    fresh_resolves: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self): pass

        def browse(self, *args, **kwargs): pass

        def submit_once(self, room, seat, start, end, day, select_params=None, pre_resolved=None, **kwargs):
            self.last_submitted = True
            if pre_resolved is not None:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            fresh_resolves.append(seat)  # 重新取页后成功
            return "预约成功"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_await_fire_and_prefetch",
        lambda *_args, **_kwargs: ProbeResult(True, "stale-tok", "algo", "none", "TOKEN_READY", "pre", source="target_day"),
    )
    service.execute_plan(plan_id, trigger="scheduled")
    assert fresh_resolves == ["097"]
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "SUCCESS"
    db.close()


def test_submit_once_classifies_rate_limit_and_phrase_variants(monkeypatch):
    """限流（"频繁"）是可重试退避，不是终局风控；303 变体短语仍归 TOKEN_STALE。"""
    client = ChaoxingClient("u", "p")
    client.resolve_submission_page = lambda *args, **kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

    class Response:
        def __init__(self, msg):
            self._msg = msg

        def json(self):
            return {"success": False, "msg": self._msg}

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response("操作过于频繁，请稍后再试"))
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3))
    assert exc_info.value.code == "RATE_LIMITED"
    from app.service import _RETRYABLE_CODES

    assert "RATE_LIMITED" in _RETRYABLE_CODES

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response("页面停留时间过长，请刷新页面后重试"))
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3))
    assert exc_info.value.code == "TOKEN_STALE"


def test_poll_until_open_raises_at_deadline(monkeypatch):
    """纵深防御：窗口迟迟不开时 poller 必须在 deadline 抛错，不得无限空转。"""
    import app.service as service

    class NeverOpenClient:
        def fetch_target_day_page(self, *args, **kwargs):
            raise ReservationError("TARGET_DAY_NOT_OPEN", "未开放")

    monkeypatch.setattr(service.app_clock, "server_now", lambda: 1000.0)
    with pytest.raises(ReservationError) as exc_info:
        service._poll_until_open(
            NeverOpenClient(), 1000.5,
            {"select_params": None, "room_id": "100", "select_context_path": None},
            date(2026, 9, 4), deadline=time.monotonic() - 1,
        )
    assert exc_info.value.code == "DEADLINE_EXCEEDED"


def test_poll_until_open_refetches_stale_page_at_fire(monkeypatch):
    """fire 时刻缓存的页面过旧（>1s）时，先再取一次新页面，失败才回退旧页。"""
    import app.service as service

    state = {"monotonic": 0.0, "clock": 1005.0}

    class FakeTime:
        @staticmethod
        def monotonic():
            return state["monotonic"]

        @staticmethod
        def sleep(_seconds):
            state["monotonic"] += 1.5
            state["clock"] += 1.5

    monkeypatch.setattr(service, "time", FakeTime)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: state["clock"])

    class Client:
        def __init__(self):
            self.fetches = 0

        def fetch_target_day_page(self, *args, **kwargs):
            self.fetches += 1
            if self.fetches == 1:
                return ProbeResult(True, "old", "algo", "none", "TOKEN_READY", "old page", source="target_day")
            return ProbeResult(True, "new", "algo", "none", "TOKEN_READY", "fresh page", source="target_day")

    client = Client()
    result = service._poll_until_open(
        client, 1006.0,
        {"select_params": None, "room_id": "100", "select_context_path": None},
        date(2026, 9, 4), deadline=FakeTime.monotonic() + 60,
    )
    assert result.message == "fresh page"
    assert client.fetches == 2


def test_clock_refresh_respects_time_budget(monkeypatch):
    """clock 校准必须在预算内结束：读超时黑洞下不能再烧掉几十秒运行预算。"""
    import app.clock as clock

    state = {"now": 0.0, "calls": 0}

    class FakeTime:
        @staticmethod
        def monotonic():
            return state["now"]

        @staticmethod
        def time():
            return state["now"]

    def fake_measure(_url, _budget):
        state["calls"] += 1
        state["now"] += 1.0  # 每次探测"耗时" 1 秒
        return None

    monkeypatch.setattr(clock, "time", FakeTime)
    monkeypatch.setattr(clock, "_measure_once", fake_measure)
    clock.refresh(dense=True, budget_seconds=3.0)
    assert state["calls"] == 3  # 8 个想要的样本只拿到预算允许的 3 次机会


def test_run_endpoints_reject_when_account_busy(tmp_path, monkeypatch):
    """同一账号已有任务排队/执行中时，probe/discover/run 直接 409。"""
    import app.web as web

    factory = _test_session_factory(tmp_path)
    db = factory()
    db.add(Account(id=1, name="a", username="u", password_blob=b"x"))
    db.commit()
    monkeypatch.setattr(web, "refresh_jobs", lambda: None)
    monkeypatch.setattr(web, "_enqueue_recently_missed_jobs", lambda: 0)
    payload = PlanIn(account_id=1, name="p", room_id="10713", seats=["097"], start_time="08:00", end_time="08:30")
    created = web.create_plan(payload, db)
    db.add(ReservationRun(plan_id=created["id"], account_id=1, target_date="2026-09-08", trigger="manual", status="RUNNING", message="busy"))
    db.commit()
    for endpoint in (web.probe_plan, web.discover_plan_context, web.run_plan):
        with pytest.raises(Exception) as exc_info:
            endpoint(created["id"], db)
        assert exc_info.value.status_code == 409
    db.close()


def test_next_run_at_reads_live_scheduler():
    """/api/plans 的 next_run_at 来自真实调度器，而非浏览器推算。"""
    from types import SimpleNamespace

    import app.web as web
    from apscheduler.triggers.date import DateTrigger

    plan_stub = SimpleNamespace(id=999911, enabled=True)
    assert web._next_run_at(SimpleNamespace(id=1, enabled=False)) is None
    fire_moment = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=5)
    try:
        web.scheduler.start(paused=True)
        web.scheduler.add_job(lambda: None, DateTrigger(run_date=fire_moment), id="plan-999911", replace_existing=True)
        value = web._next_run_at(plan_stub)
        assert value is not None
        parsed = datetime.fromisoformat(value)
        assert abs((parsed - (fire_moment + timedelta(seconds=30))).total_seconds()) < 1
    finally:
        try:
            web.scheduler.remove_job("plan-999911")
        except Exception:
            pass
        web.scheduler.shutdown(wait=False)


def test_volley_racer_heals_token_stale_in_place(tmp_path, monkeypatch):
    """竞速硬化回归：被 303 拒绝的 racer 原地刷新重提，不退到秒级串行路径。

    遥测（page_ms/submit_ms/token_age_ms/heals）必须落进审计记录，
    用真实数据持续定位 303 根因。
    """
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097", "098"), max_attempts=3)
    submits: list[str] = []
    parent_submits: list[str] = []
    tries: dict[str, int] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self): pass

        def browse(self, *args, **kwargs): pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if not self.is_clone:
                parent_submits.append(seat)
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            submits.append(seat)
            if seat != "097":
                # 其余座位终局占用（不自愈），保证 097 是唯一可能的赢家。
                raise ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")
            tries[seat] = tries.get(seat, 0) + 1
            if tries[seat] == 1:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            return "预约成功"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    # _hold_until_fire 不打桩：真实的 50ms 错峰保证 097 先自愈成功、098 未及出手。
    service.execute_plan(plan_id, trigger="scheduled")
    # 优先级最高的 097：父会枪被 303 拒、racer 首发被拒、自愈重提成功；098 未及出手即停。
    assert submits.count("097") == 2
    assert parent_submits == ["097"]
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.selected_seat) == ("SUCCESS", "097")
    codes = [(a["seat"], a["code"]) for a in run.attempt_details if a["seat"] == "097"]
    assert ("097", "TOKEN_STALE") in codes and ("097", None) in codes
    healed = next(a for a in run.attempt_details if a["seat"] == "097" and a["code"] is None)
    assert {"page_ms", "submit_ms", "token_age_ms"} <= set(healed.get("timing", {}))
    assert healed["timing"].get("heals") == 1
    db.close()


def test_volley_heal_attempts_are_bounded(tmp_path, monkeypatch):
    """自愈有上限：持续 303 的座位打满 1+RACER_HEAL_ATTEMPTS 发后交回串行轮换。"""
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097", "098"), max_attempts=3)
    clone_submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.parent_calls = 0
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self): pass

        def browse(self, *args, **kwargs): pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if self.is_clone:
                clone_submits.append(seat)
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            self.parent_calls += 1
            if self.parent_calls == 1:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            return "预约成功"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    assert clone_submits.count("097") == 1 + service.RACER_HEAL_ATTEMPTS
    assert clone_submits.count("098") == 1 + service.RACER_HEAL_ATTEMPTS
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "SUCCESS"  # 自愈耗尽后串行兜底收尾
    db.close()


def test_late_racer_submit_is_audited_and_verifiable(monkeypatch):
    """竞速硬化回归：deadline 后仍在途的提交必须进审计并触发核实，不得静默失败。"""
    import app.service as service

    monkeypatch.setattr(service, "LATE_SUBMIT_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    released = threading.Event()

    class FakeClient:
        deadline = None

        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if self.is_clone:
                if not released.wait(3):
                    raise ReservationError("NETWORK_ERROR", "读超时")
                return "预约成功"
            # 父会话首枪快速被 303 拒（不阻塞），把场景留给卡死的 racer。
            raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

    client = FakeClient()
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    request_values = {"room_id": "100", "start_time": "08:00", "end_time": "09:00",
                      "select_params": None, "select_context_path": None, "select_context_source": None}
    try:
        outcomes, winner = service._parallel_opening_shot(
            client, request_values, date(2026, 9, 4), ["097"], time.time(), time.monotonic() + 0.3,
        )
        assert winner is None
        assert [(o["seat"], o["code"], o["submitted"]) for o in outcomes] == [
            ("097", "TOKEN_STALE", True),  # 父会话首枪
            ("097", "SUBMIT_OUTCOME_UNKNOWN", True),  # 宽限后仍在途的 racer
        ]
        # 核实需要活的会话：客户端 deadline 必须被临时延长。
        assert client.deadline is not None and client.deadline > time.monotonic()
    finally:
        released.set()


def test_volley_stagger_preserves_priority_order(monkeypatch):
    """竞速硬化回归：racer 按优先级序错峰开火（097 最先，总跨度 ≥ 3×50ms）。"""
    import app.service as service

    first_submit_at: dict[str, float] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            first_submit_at.setdefault(seat, time.monotonic())
            raise ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")

        def clone_authenticated(self):
            return FakeClient()

    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    request_values = {"room_id": "100", "start_time": "08:00", "end_time": "09:00",
                      "select_params": None, "select_context_path": None, "select_context_source": None}
    outcomes, winner = service._parallel_opening_shot(
        FakeClient(), request_values, date(2026, 9, 4), ["097", "098", "099", "100"], time.time(), time.monotonic() + 10,
    )
    assert winner is None
    assert {o["seat"] for o in outcomes} == {"097", "098", "099", "100"}
    timestamps = [first_submit_at[seat] for seat in ("097", "098", "099", "100")]
    assert timestamps == sorted(timestamps)  # 优先级高的先发
    assert (timestamps[-1] - timestamps[0]) >= 3 * service.RACER_STAGGER_SECONDS - 0.03


def test_parent_shot_wins_and_stops_racers(tmp_path, monkeypatch):
    """速度升级回归：父会话首枪成功时立即获胜，racer 一发都不用打。"""
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097", "098"), max_attempts=3)
    submits: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self): pass

        def browse(self, *args, **kwargs): pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            submits.append(("parent" if not self.is_clone else "racer", seat))
            if not self.is_clone:
                return "预约成功"
            raise AssertionError("父会话已获胜，racer 不应提交")

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    assert submits == [("parent", "097")]  # 唯一一发，最高优先级座位
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.selected_seat) == ("SUCCESS", "097")
    assert len(run.attempt_details) == 1
    assert run.attempt_details[0]["timing"].get("at") is not None
    db.close()


def test_fire_calibration_roundtrip(tmp_path, monkeypatch):
    """校准存取：EMA 更新、拒绝异常偏移、无样本时开火时刻零偏移。"""
    import app.service as service
    from types import SimpleNamespace

    factory = _test_session_factory(tmp_path)
    db = factory()
    assert service._load_fire_calibration(db) == (0.0, 0)
    assert service._record_accept_offset(db, 1.5) == 1.5
    assert service._load_fire_calibration(db) == (1.5, 1)
    assert service._record_accept_offset(db, 2.5) == 2.0  # EMA α=0.5
    assert service._load_fire_calibration(db) == (2.0, 2)
    assert service._record_accept_offset(db, 99.0) is None  # 超界拒绝
    assert service._record_accept_offset(db, -99.0) is None  # 超界负偏移拒绝
    # 回落：平台即时受理（偏移 ≤ 0）把 EMA 拉回，陈旧偏移不会永久晚开火。
    assert service._record_accept_offset(db, -1.0) == 0.5  # 2.0 + 0.5*(-1-2.0)
    assert service._load_fire_calibration(db) == (0.5, 3)
    assert service._record_accept_offset(db, 0.0) == 0.25  # 继续向 0 收敛
    assert service._load_fire_calibration(db) == (0.25, 4)

    # 无样本 → 开火时刻就是基准；有样本 → 后移 max(0, ema - 0.2)。
    monkeypatch.setattr(service.app_clock, "refresh", lambda *a, **k: 0.0)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    monkeypatch.setattr(service.random, "uniform", lambda low, high: 0.0)
    stale = db.get(AppSetting, service._CALIBRATION_SETTING_KEY)
    if stale is not None:
        db.delete(stale)
        db.commit()
    plan = SimpleNamespace(run_time="23:59")
    fire_no_calibration = service._scheduled_fire_epoch(plan, db)
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    expected_base = now.replace(hour=23, minute=59, second=0, microsecond=0).timestamp()
    assert abs(fire_no_calibration - expected_base) < 1e-6

    db.add(AppSetting(key="fire_accept_calibration", value='{"ema": 1.5, "samples": 5}'))
    db.commit()
    fire_calibrated = service._scheduled_fire_epoch(plan, db)
    assert abs(fire_calibrated - (expected_base + 1.3)) < 1e-6  # 1.5 - 0.2 安全余量
    db.close()


def test_final_heal_uses_full_resolver_chain(tmp_path, monkeypatch):
    """速度升级回归：自愈最后一轮改走完整解析链（实战 4/4 成功路径）。"""
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097", "098"), max_attempts=3)
    full_resolves: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.parent_calls = 0
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self): pass

        def browse(self, *args, **kwargs): pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            full_resolves.append("full")
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "full", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if self.is_clone:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            self.parent_calls += 1
            if self.parent_calls == 1:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            return "预约成功"

    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: time.time())
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    service.execute_plan(plan_id, trigger="scheduled")
    # 每个 racer 3 发（首发与自愈1走快捷路径，末轮自愈走完整链）：末轮必须走完整链。
    assert len(full_resolves) >= 2
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert run.status == "SUCCESS"
    db.close()


def test_scheduled_run_records_fire_calibration(tmp_path, monkeypatch):
    """端到端：volley 阶段（父会枪）的“首个真实受理”时刻进入校准样本。

    串行阶段的尝试被排除——它们发生在我们自己的退避等待之后，计入会形成
    “每轮都比上轮晚”的正反馈（2026-09-03 复审缺陷）。
    """
    import app.service as service

    factory = _test_session_factory(tmp_path)
    plan_id = _make_plan(factory, seats=("097", "098"), max_attempts=3)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def login(self): pass

        def browse(self, *args, **kwargs): pass

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            raise ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")

    fire = time.time()
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "ChaoxingClient", FakeClient)
    monkeypatch.setattr(service, "decrypt_password", lambda _: "password")
    monkeypatch.setattr(service, "notify_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_scheduled_fire_epoch", lambda _plan: fire)
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    assert (run.status, run.error_code) == ("FAILED", "SEAT_UNAVAILABLE")
    ema, samples = service._load_fire_calibration(db)
    assert samples == 1  # 父会枪的占用回答（真实受理信号，volley 阶段）
    assert 0.0 <= ema <= service._CALIBRATION_MAX_SECONDS
    db.close()


def test_parent_shot_crash_stops_racers(monkeypatch):
    """复审缺陷回归：父会枪抛意外异常时必须先 stop 再放行，racer 一发不发。

    异常向上展开会释放账号锁——任何越过停止检查的提交都会超出锁的保护范围。
    """
    import app.service as service

    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    clone_submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if not self.is_clone:
                raise RuntimeError("父会话意外崩溃（如平台返回 JSON 数组）")
            clone_submits.append(seat)
            return "预约成功"

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    request_values = {"room_id": "100", "start_time": "08:00", "end_time": "09:00",
                      "select_params": None, "select_context_path": None, "select_context_source": None}
    with pytest.raises(RuntimeError):
        service._parallel_opening_shot(FakeClient(), request_values, date(2026, 9, 4), ["097", "098"], time.time(), time.monotonic() + 5)
    assert clone_submits == []


def test_volley_dead_seat_skips_racer_shot(monkeypatch):
    """复审缺陷回归：父会枪确认占用后，同座位 racer 不再补一发。"""
    import app.service as service

    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    clone_submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if not self.is_clone:
                raise ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")
            clone_submits.append(seat)
            raise ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    request_values = {"room_id": "100", "start_time": "08:00", "end_time": "09:00",
                      "select_params": None, "select_context_path": None, "select_context_source": None}
    outcomes, winner = service._parallel_opening_shot(FakeClient(), request_values, date(2026, 9, 4), ["097", "098"], time.time(), time.monotonic() + 5)
    assert winner is None
    assert "097" not in clone_submits  # 父会枪已确认占用：racer 跳过
    assert clone_submits == ["098"]
    assert {o["seat"] for o in outcomes} == {"097", "098"}


def test_grace_stops_resolving_straggler(monkeypatch):
    """复审缺陷回归：宽限耗尽后无条件 stop——仍在解析（未提交）的 racer
    不得在函数返回后越过停止检查与串行兜底并发提交。"""
    import app.service as service

    monkeypatch.setattr(service, "LATE_SUBMIT_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    racer_submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def fetch_target_day_page(self, *args, **kwargs):
            if self.is_clone:
                time.sleep(1.0)  # 解析阶段拖过长于宽限
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "slow-resolve", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if self.is_clone:
                racer_submits.append(seat)
                return "预约成功"
            raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

    client = FakeClient()
    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    request_values = {"room_id": "100", "start_time": "08:00", "end_time": "09:00",
                      "select_params": None, "select_context_path": None, "select_context_source": None}
    outcomes, winner = service._parallel_opening_shot(client, request_values, date(2026, 9, 4), ["097"], time.time(), time.monotonic() + 0.3)
    assert winner is None
    assert [o["code"] for o in outcomes] == ["TOKEN_STALE"]  # 只有父会枪；racer 未在途
    time.sleep(1.2)  # 等拖尾 racer 走完解析
    assert racer_submits == []  # stop 已置位：解析完成后越过停止检查直接退出


def test_racer_survives_resolver_unexpected_error(monkeypatch):
    """复审缺陷回归：解析层抛非 ReservationError 不再静默杀死 racer。"""
    import app.service as service

    monkeypatch.setattr(service.app_clock, "server_now", lambda: time.time())
    submits: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None

        def fetch_target_day_page(self, *args, **kwargs):
            if self.is_clone:
                raise ReservationError("TARGET_CONTEXT_UNAVAILABLE", "取页失败")
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")

        def resolve_submission_page(self, *args, **kwargs):
            if self.is_clone:
                raise RuntimeError("页面解析意外崩溃")
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

        def submit_once(self, room, seat, *args, **kwargs):
            self.last_submitted = True
            if not self.is_clone:
                raise ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            submits.append(seat)
            return "预约成功"

        def clone_authenticated(self):
            clone = FakeClient()
            clone.is_clone = True
            return clone

    monkeypatch.setattr(
        service, "_poll_until_open",
        lambda *_args, **_kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day"),
    )
    monkeypatch.setattr(service, "_hold_until_fire", lambda *_args, **_kwargs: None)
    request_values = {"room_id": "100", "start_time": "08:00", "end_time": "09:00",
                      "select_params": None, "select_context_path": None, "select_context_source": None}
    outcomes, winner = service._parallel_opening_shot(FakeClient(), request_values, date(2026, 9, 4), ["097"], time.time(), time.monotonic() + 5)
    # racer 的两级解析都意外崩溃，但兜底到 poller 页面后照常提交并获胜。
    assert winner is not None and winner["seat"] == "097"
    assert submits == ["097"]


def test_submit_once_non_dict_payload_is_outcome_unknown(monkeypatch):
    """复审缺陷回归：平台/WAF 返回 JSON 数组时不得 AttributeError 崩溃。"""
    client = ChaoxingClient("u", "p")
    client.resolve_submission_page = lambda *args, **kwargs: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")

    class ArrayResponse:
        def json(self):
            return []  # 异常网关返回的 JSON 数组

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: ArrayResponse())
    with pytest.raises(ReservationError) as exc_info:
        client.submit_once("100", "001", "08:00", "09:00", date(2026, 9, 3))
    assert exc_info.value.code == "SUBMIT_OUTCOME_UNKNOWN"
