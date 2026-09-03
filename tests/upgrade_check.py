"""Standalone verification of the bot-race upgrade (no pytest machinery).

The DSH sandbox denies directory scanning for pytest's tmp_path machinery and
tempfile.TemporaryDirectory, so this script uses its own sqlite files under
data/ and avoids listing directories altogether. Run:

    .venv\\Scripts\\python.exe tests\\upgrade_check.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.service as service  # noqa: E402
from app.chaoxing_client import ProbeResult  # noqa: E402
from app.db import Account, Base, PlanSeat, ReservationPlan, ReservationRun  # noqa: E402

CHECKS: list[str] = []
DATA = Path(__file__).resolve().parents[1] / "data"
RUN_TAG = f"{int(time.time())}"


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")


def usable_dir(base: Path, name: str) -> Path:
    """Create a directory the sandbox actually lets us use (some fresh dirs
    are born access-denied; cycle names until one works)."""
    base.mkdir(parents=True, exist_ok=True)
    for index in range(30):
        candidate = base / f"{name}{index}"
        candidate.mkdir(exist_ok=True)
        probe = candidate / ".probe"
        try:
            probe.write_text("ok")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise RuntimeError(f"no usable directory under {base}")


def make_factory(root: Path, name: str):
    # Unique per-run directory: a stale check.db from an earlier run would
    # trip the duplicate-reservation guard and skew the scenarios.
    engine = create_engine(f"sqlite:///{usable_dir(root, f'{name}-{RUN_TAG}') / 'check.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def make_plan(factory, seats=("001", "002"), max_attempts=3):
    db = factory()
    account = db.get(Account, 1) or Account(id=1, name="a", username="user-1", password_blob=b"test")
    plan = ReservationPlan(
        account=account, name="p", room_id="100", start_time="08:00", end_time="09:00",
        run_time="19:00", day_offset=2,
        weekdays_json='["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]',
        max_attempts=max_attempts, enabled=True,
    )
    plan.seats = [PlanSeat(seat_num=seat, priority=index) for index, seat in enumerate(seats)]
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()
    return plan_id


class DeadSeatClient:
    def __init__(self, *args, **kwargs): pass
    def login(self): pass
    def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
        raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")


def scenario_serial_no_repeat() -> None:
    factory = make_factory(DATA, "uc-serial")
    plan_id = make_plan(factory, seats=("001", "002"), max_attempts=5)
    calls: list[str] = []

    class Client(DeadSeatClient):
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            calls.append(seat)
            raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")

    service.SessionLocal = factory
    service.ChaoxingClient = Client
    service.decrypt_password = lambda _: "password"
    service.execute_plan(plan_id)
    check("serial-no-repeat", calls == ["001", "002"], f"calls={calls}")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("serial-failed-code", (run.status, run.error_code) == ("FAILED", "SEAT_UNAVAILABLE"))
    check("serial-two-attempts", len(run.attempt_details) == 2, f"details={len(run.attempt_details)}")
    db.close()


def scenario_budget_clamp() -> None:
    factory = make_factory(DATA, "uc-clamp")
    plan_id = make_plan(factory, seats=("001",), max_attempts=8)
    service.SessionLocal = factory
    service.ChaoxingClient = DeadSeatClient
    service.execute_plan(plan_id)
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("budget-clamp-message", "钳制为 6" in (run.message or ""), f"message={run.message!r}")
    db.close()


def scenario_parallel_winner() -> None:
    factory = make_factory(DATA, "uc-parallel")
    plan_id = make_plan(factory, seats=("001", "002"), max_attempts=3)
    fast_fetches: list[str] = []
    slow_resolves: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def login(self): pass
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self): return Client()
        def fetch_target_day_page(self, day, params, select_path=None):
            fast_fetches.append("fetch")
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")
        def resolve_submission_page(self, *args, **kwargs):
            slow_resolves.append("resolve")
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            self.last_submitted = True
            if seat == "001":
                raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")
            return "预约成功"

    service.SessionLocal = factory
    service.ChaoxingClient = Client
    service.notify_async = lambda *args, **kwargs: None
    service._scheduled_fire_epoch = lambda _plan: time.time()
    service._poll_until_open = lambda *a, **k: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day")
    service._hold_until_fire = lambda *a, **k: None
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("parallel-success", run.status == "SUCCESS", f"status={run.status}")
    check("parallel-winner-seat", run.selected_seat == "002", f"seat={run.selected_seat}")
    # Shortest path: every racer rode the single-GET fetch; the full resolver
    # chain (redirects / context discovery) must never run at the opening.
    check("parallel-shortest-path", len(fast_fetches) >= 2 and not slow_resolves,
          f"fast={len(fast_fetches)} slow={len(slow_resolves)}")
    check("parallel-limit-six", service.PARALLEL_SEAT_LIMIT == 6, str(service.PARALLEL_SEAT_LIMIT))
    # First winner stops the rest: a loser may be cancelled before its submit,
    # so the audit must contain the winner and only known racers.
    seats_audited = {item["seat"] for item in run.attempt_details}
    check("parallel-audit-racers", "002" in seats_audited and seats_audited <= {"001", "002"},
          f"details={sorted(seats_audited)}")
    db.close()


def scenario_parallel_fallback_to_serial() -> None:
    """Window never opens: the poller gives up and the serial loop takes over."""
    factory = make_factory(DATA, "uc-fallback")
    plan_id = make_plan(factory, seats=("001", "002"), max_attempts=3)
    calls: list[str] = []

    class Client(DeadSeatClient):
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self): return Client()
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            calls.append(seat)
            raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")

    service.SessionLocal = factory
    service.ChaoxingClient = Client
    service._scheduled_fire_epoch = lambda _plan: time.time()
    service._poll_until_open = lambda *a, **k: None
    service._hold_until_fire = lambda *a, **k: None
    service.execute_plan(plan_id, trigger="scheduled")
    check("fallback-serial-sweeps", calls == ["001", "002"], f"calls={calls}")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("fallback-failed", run.status == "FAILED", f"status={run.status}")
    db.close()


def scenario_parallel_token_stale_recovery() -> None:
    """Production incident 2026-09-03: full volley rejected with platform 303.

    4 seats fired in parallel; three racers got TOKEN_STALE (安全验证已超时，
    请刷新后再提交) and one got SEAT_UNAVAILABLE. With the default budget
    (max_attempts=3 → attempts=4) the stale rejections used to consume the
    whole serial budget, so no refresh-retry ran and the run reported the last
    racer's code (座位不可预约) instead of recovering.
    """
    factory = make_factory(DATA, "uc-stale")
    plan_id = make_plan(factory, seats=("097", "098", "099", "100"), max_attempts=3)
    submits: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.parent_calls = 0
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def login(self): pass
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self):
            clone = Client()
            clone.is_clone = True
            return clone
        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")
        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            submits.append(seat)
            self.last_submitted = True
            if not self.is_clone:
                self.parent_calls += 1
                if self.parent_calls > 1:
                    return "预约成功"  # the serial refresh-retry wins
                raise service.ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            if seat == "100":
                raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")
            raise service.ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")

    service.SessionLocal = factory
    service.ChaoxingClient = Client
    service.notify_async = lambda *args, **kwargs: None
    service._scheduled_fire_epoch = lambda _plan: time.time()
    service._poll_until_open = lambda *a, **k: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day")
    service._hold_until_fire = lambda *a, **k: None
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("stale-volley-recovers", run.status == "SUCCESS", f"status={run.status}")
    # 父会枪 303(1) + 3 个 303 座位各 1+2 次自愈(9) + 占用座位 1 发 + 恰好 1 次串行
    # 刷新重试；the confirmed-taken seat is never healed nor retried.
    check("stale-volley-submit-count", len(submits) == 12, f"submits={submits}")
    check("stale-dead-seat-not-retried", submits.count("100") == 1, f"submits={submits}")
    check("stale-winner-is-live-seat", run.selected_seat in {"097", "098", "099"}, f"seat={run.selected_seat}")
    check("stale-audit-complete", len(run.attempt_details) == 12, f"details={len(run.attempt_details)}")
    db.close()


def scenario_volley_heal_in_place() -> None:
    """Race hardening 2026-09-03: a 303-rejected racer heals itself with one
    fresh page fetch + resubmit instead of falling back to the serial path."""
    factory = make_factory(DATA, "uc-heal")
    plan_id = make_plan(factory, seats=("097", "098"), max_attempts=3)
    submits: list[str] = []
    tries: dict[str, int] = {}
    real_server_now = service.app_clock.server_now

    class Client:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def login(self): pass
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self):
            clone = Client()
            clone.is_clone = True
            return clone
        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")
        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            self.last_submitted = True
            submits.append(seat)
            if not self.is_clone:
                # 父会话首枪被 303 拒（与实战一致）。
                raise service.ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            if seat != "097":
                # 其余座位终局占用（不自愈），保证 097 是唯一可能的赢家。
                raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")
            tries[seat] = tries.get(seat, 0) + 1
            if tries[seat] == 1:
                raise service.ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
            return "预约成功"

    service.SessionLocal = factory
    service.ChaoxingClient = Client
    service.notify_async = lambda *args, **kwargs: None
    service.app_clock.server_now = lambda: time.time()
    service._scheduled_fire_epoch = lambda _plan: time.time()
    service._poll_until_open = lambda *a, **k: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day")
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("heal-in-place-wins", (run.status, run.selected_seat) == ("SUCCESS", "097"), f"{run.status}/{run.selected_seat}")
    check("heal-in-place-submits", submits.count("097") == 3, f"submits={submits}")
    healed = next(a for a in run.attempt_details if a["seat"] == "097" and a["code"] is None)
    check("heal-telemetry-recorded", {"page_ms", "submit_ms", "token_age_ms", "heals"} <= set(healed.get("timing", {})),
          f"timing={healed.get('timing')}")
    db.close()


def scenario_late_submit_unknown() -> None:
    """Race hardening 2026-09-03: a POST still on the wire past the deadline
    must be audited as SUBMIT_OUTCOME_UNKNOWN (server-verified by the caller)
    and never silently dropped."""
    import threading

    real_grace = service.LATE_SUBMIT_GRACE_SECONDS
    real_server_now = service.app_clock.server_now
    real_poller = service._poll_until_open
    released = threading.Event()

    class Client:
        deadline = None
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "target_day"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            self.last_submitted = True
            if self.is_clone:
                if not released.wait(3):
                    raise service.ReservationError("NETWORK_ERROR", "读超时")
                return "预约成功"
            # 父会话首枪快速被 303 拒，把场景留给卡死的 racer。
            raise service.ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")
        def clone_authenticated(self):
            clone = Client()
            clone.is_clone = True
            return clone

    client = Client()
    try:
        service.LATE_SUBMIT_GRACE_SECONDS = 0.2
        service.app_clock.server_now = lambda: time.time()
        service._poll_until_open = lambda *a, **k: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day")
        outcomes, winner = service._parallel_opening_shot(
            client,
            {"room_id": "100", "start_time": "08:00", "end_time": "09:00",
             "select_params": None, "select_context_path": None, "select_context_source": None},
            __import__("datetime").date(2026, 9, 4), ["097"], time.time(), time.monotonic() + 0.3,
        )
        check("late-submit-audited",
              winner is None and [(o["seat"], o["code"]) for o in outcomes] == [("097", "TOKEN_STALE"), ("097", "SUBMIT_OUTCOME_UNKNOWN")],
              f"outcomes={[(o['seat'], o['code']) for o in outcomes]}")
        check("late-submit-client-extended", client.deadline is not None and client.deadline > time.monotonic(),
              f"deadline={client.deadline}")
    finally:
        released.set()
        service.LATE_SUBMIT_GRACE_SECONDS = real_grace
        service.app_clock.server_now = real_server_now
        service._poll_until_open = real_poller


def scenario_stale_exhaustion_keeps_real_code() -> None:
    """Even when every refresh-retry stays stale, the final verdict must be
    TOKEN_STALE (with the platform's refresh hint), not seat-unavailable."""
    factory = make_factory(DATA, "uc-stale2")
    plan_id = make_plan(factory, seats=("097", "098", "099", "100"), max_attempts=3)
    submits: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def login(self): pass
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self):
            clone = Client()
            clone.is_clone = True
            return clone
        def fetch_target_day_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "fast", source="target_day")
        def resolve_submission_page(self, *args, **kwargs):
            return ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "ready", source="target_day")
        def submit_once(self, room, seat, start, end, day, select_params=None, **kwargs):
            submits.append(seat)
            self.last_submitted = True
            if self.is_clone and seat == "100":
                raise service.ReservationError("SEAT_UNAVAILABLE", "该时间段已被占用！")
            raise service.ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")

    service.SessionLocal = factory
    service.ChaoxingClient = Client
    service.notify_async = lambda *args, **kwargs: None
    service._scheduled_fire_epoch = lambda _plan: time.time()
    service._poll_until_open = lambda *a, **k: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day")
    service._hold_until_fire = lambda *a, **k: None
    service.execute_plan(plan_id, trigger="scheduled")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("stale-exhaustion-code", (run.status, run.error_code) == ("FAILED", "TOKEN_STALE"),
          f"status={run.status} code={run.error_code}")
    check("stale-exhaustion-refresh-ran", len(submits) > 4, f"submits={len(submits)}")
    check("stale-exhaustion-hint", "刷新后再提交" in (run.message or ""), "")
    db.close()


def scenario_clock_estimator() -> None:
    import app.clock as clock

    data = [(1.0, 10.0), (0.2, 4.0), (0.1, 2.0), (0.3, 6.0), (0.4, 8.0), (0.5, 12.0), (0.25, 5.0), (0.35, 7.0)]
    index = {"i": 0}

    def fake_measure(_url, _budget=None):
        value = data[index["i"] % len(data)]
        index["i"] += 1
        return value

    clock._measure_once = fake_measure
    applied = clock.refresh(dense=True)
    # Fastest half of the eight samples: offsets 2,4,5,6 → mean 4.25, plus the
    # Date-header truncation bias.
    expected = (2.0 + 4.0 + 5.0 + 6.0) / 4 + clock.DATE_HEADER_BIAS_SECONDS
    check("clock-fastest-half-mean", abs(applied - expected) < 1e-9, f"applied={applied:.3f} expected={expected:.3f}")


def scenario_clock_budget() -> None:
    """2026-09-03 audit: clock calibration must stop at its time budget.

    A read-timeout black hole used to let dense probing burn ~96 s of a run
    that only has 60 s and wakes 30 s early. The fake time module is restored
    afterwards — a frozen clock left behind would make every later scenario's
    server_now() disagree with real fire epochs by eons.
    """
    import app.clock as clock

    state = {"now": 100.0, "calls": 0}
    real_time, real_measure = clock.time, clock._measure_once

    class FakeTime:
        @staticmethod
        def monotonic():
            return state["now"]

        @staticmethod
        def time():
            return state["now"]

    def slow_measure(_url, _budget):
        state["calls"] += 1
        state["now"] += 2.0  # each probe "takes" 2 seconds
        return None

    try:
        clock.time = FakeTime
        clock._measure_once = slow_measure
        clock.refresh(dense=True, budget_seconds=5.0)
        first_pass = state["calls"]
        clock.refresh(dense=True)  # default dense budget must also bound itself
        check("clock-budget-cuts-probes", 0 < first_pass <= 3 and 0 < state["calls"] - first_pass <= 5,
              f"first={first_pass} total={state['calls']}")
    finally:
        clock.time = real_time
        clock._measure_once = real_measure


def scenario_catchup_after_failed_run() -> None:
    """Production incident 2026-09-03 01:29: run_time moved later the same day.

    The save landed 5 s after the new wake-up moment, so cron went to tomorrow
    and the only safety net — the recently-missed catch-up — was swallowed by
    a dedup key that matched the morning attempt's FAILED row for the same
    (plan, target_date). Only in-flight or SUCCESSFUL runs may block a catch-up.
    """
    import app.scheduler as scheduler_module
    import datetime as dt
    from zoneinfo import ZoneInfo

    factory = make_factory(DATA, "uc-catchup")
    plan_id = make_plan(factory, seats=("001",), max_attempts=3)
    db = factory()
    plan = db.get(ReservationPlan, plan_id)
    plan.run_time = "08:00"
    plan.day_offset = 1
    db.commit()
    db.close()
    db = factory()
    db.add(ReservationRun(plan_id=plan_id, account_id=1, target_date="2026-09-08", trigger="scheduled", status="FAILED", error_code="TOKEN_STALE", message="failed earlier today"))
    db.commit()
    db.close()
    queued = []
    scheduler_module.SessionLocal = factory
    scheduler_module.enqueue_plan = lambda plan_id, trigger: queued.append((plan_id, trigger)) or 1
    now = dt.datetime(2026, 9, 7, 8, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    count = scheduler_module._enqueue_recently_missed_jobs(now)
    check("catchup-after-failure", count == 1 and queued == [(plan_id, "scheduled_catchup")], f"queued={queued}")
    db = factory()
    db.add(ReservationRun(plan_id=plan_id, account_id=1, target_date="2026-09-08", trigger="scheduled", status="SUCCESS", message="done"))
    db.commit()
    db.close()
    check("catchup-blocked-by-success", scheduler_module._enqueue_recently_missed_jobs(now) == 0, "")
    db.close()


def scenario_serial_rotation() -> None:
    """2026-09-03 audit: refresh retries after a full-303 volley rotate seats.

    The old fallback always picked the first live seat, spending the whole
    budget on 097 while 098/099 never saw a refresh retry.
    """
    factory = make_factory(DATA, "uc-rotate")
    plan_id = make_plan(factory, seats=("097", "098", "099"), max_attempts=3)
    serial_submits: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.is_clone = False
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def login(self): pass
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self):
            clone = Client()
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
            raise service.ReservationError("TOKEN_STALE", "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)")

    service.SessionLocal = factory
    service.ChaoxingClient = Client
    service.notify_async = lambda *args, **kwargs: None
    service._scheduled_fire_epoch = lambda _plan: time.time()
    service._poll_until_open = lambda *a, **k: ProbeResult(True, "tok", "algo", "none", "TOKEN_READY", "open", source="target_day")
    service._hold_until_fire = lambda *a, **k: None
    service.execute_plan(plan_id, trigger="scheduled")
    # 首条为父会话首枪（097 被 303 拒），其后是串行轮换 097→098→099。
    check("serial-rotation-sweeps", serial_submits == ["097", "097", "098", "099"], f"submits={serial_submits}")
    db = factory()
    run = db.scalar(select(ReservationRun).order_by(ReservationRun.id.desc()))
    check("serial-rotation-verdict", (run.status, run.error_code) == ("FAILED", "TOKEN_STALE"), f"{run.status}/{run.error_code}")
    db.close()


def scenario_patch_toggle_preserves_context() -> None:
    """2026-09-03 audit: a bare enable/disable toggle must not wipe the
    discovered select context (the browser used to PATCH a stale full snapshot
    with select_params=null)."""
    import app.web as web
    from app.db import Account

    factory = make_factory(DATA, "uc-patch")
    db = factory()
    db.add(Account(id=1, name="a", username="u", password_blob=b"x"))
    db.commit()
    web.refresh_jobs = lambda: None
    web._enqueue_recently_missed_jobs = lambda: 0
    payload = web.PlanIn(account_id=1, name="p", room_id="10713", seats=["097"], start_time="08:00", end_time="08:30")
    created = web.create_plan(payload, db)
    plan = db.get(ReservationPlan, created["id"])
    plan.select_params_json = '{"id": "10713"}'
    plan.select_context_path = "/front/third/apps/seat/select"
    plan.select_context_source = "auto_context"
    db.commit()
    web.patch_plan(created["id"], {"enabled": False}, db)
    plan = db.get(ReservationPlan, created["id"])
    check("patch-toggle-keeps-context", plan.select_params == {"id": "10713"} and plan.select_context_source == "auto_context",
          f"params={plan.select_params_json} source={plan.select_context_source}")
    check("patch-toggle-applies-enabled", plan.enabled is False, f"enabled={plan.enabled}")
    web.patch_plan(created["id"], {"select_params": None}, db)
    plan = db.get(ReservationPlan, created["id"])
    check("patch-explicit-null-clears", plan.select_params is None and plan.select_context_path is None, "")
    db.close()


def scenario_redirect_and_rate_limit_retryable() -> None:
    """2026-09-03 audit: unknown 30x hops and throttle wording stay retryable."""
    from app.chaoxing_client import ChaoxingClient

    check("unknown-redirect-retryable",
          ChaoxingClient._classify_redirect(302, "https://office.chaoxing.com/front/other").code == "HTTP_REDIRECT"
          and "HTTP_REDIRECT" in service._RETRYABLE_CODES, "")
    check("rate-limited-retryable", "RATE_LIMITED" in service._RETRYABLE_CODES, "")


def scenario_constants() -> None:
    check("jitter-compressed", service.FIRE_JITTER_RANGE == (0.0, 0.05), str(service.FIRE_JITTER_RANGE))
    check("first-pass-compressed", service.FIRST_PASS_WAIT_RANGE == (0.05, 0.15), str(service.FIRST_PASS_WAIT_RANGE))
    check("retry-compressed", service.RETRY_WAIT_RANGE == (0.2, 0.45), str(service.RETRY_WAIT_RANGE))
    check("budget-raised", service.MAX_SUBMIT_ATTEMPTS == 6, str(service.MAX_SUBMIT_ATTEMPTS))
    check("opening-codes-retryable", {"TARGET_CONTEXT_UNAVAILABLE", "TARGET_DAY_NOT_OPEN"} <= service._RETRYABLE_CODES)
    check("ui-cap-synced", True)  # verified by pydantic model below

    from app.web import PlanData
    try:
        plan6 = PlanData.model_validate({
            "account_id": 1, "name": "x", "room_id": "1", "seats": ["001", "002", "003", "004", "005", "006"],
            "start_time": "08:00", "end_time": "09:00", "max_attempts": 6,
        })
        ok6 = len(plan6.seats) == 6
    except Exception:
        ok6 = False
    try:
        plan7 = PlanData.model_validate({
            "account_id": 1, "name": "x", "room_id": "1", "seats": ["001"],
            "start_time": "08:00", "end_time": "09:00", "max_attempts": 7,
        })
        clamped = plan7.max_attempts == 6
    except Exception:
        clamped = False
    check("ui-allows-six-seats", ok6)
    check("ui-clamps-seven-to-six", clamped)


def main() -> int:
    scenario_serial_no_repeat()
    scenario_budget_clamp()
    scenario_parallel_winner()
    scenario_parallel_fallback_to_serial()
    scenario_parallel_token_stale_recovery()
    scenario_stale_exhaustion_keeps_real_code()
    scenario_clock_estimator()
    scenario_clock_budget()
    scenario_catchup_after_failed_run()
    scenario_serial_rotation()
    scenario_patch_toggle_preserves_context()
    scenario_redirect_and_rate_limit_retryable()
    scenario_constants()
    scenario_volley_heal_in_place()
    scenario_late_submit_unknown()
    for line in CHECKS:
        print(line)
    failures = [line for line in CHECKS if line.startswith("FAIL")]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
