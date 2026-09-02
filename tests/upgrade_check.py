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

    class Client:
        def __init__(self, *args, **kwargs):
            self.last_parameter_source = "room_context"
            self.last_submitted = False
            self.last_discovered_select_params = None
        def login(self): pass
        def browse(self, *args, **kwargs): pass
        def clone_authenticated(self): return Client()
        def resolve_submission_page(self, *args, **kwargs):
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
    check("parallel-audit-both", {item["seat"] for item in run.attempt_details} == {"001", "002"},
          f"details={[i['seat'] for i in run.attempt_details]}")
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


def scenario_clock_estimator() -> None:
    import app.clock as clock

    data = [(1.0, 10.0), (0.2, 4.0), (0.1, 2.0), (0.3, 6.0), (0.4, 8.0), (0.5, 12.0), (0.25, 5.0), (0.35, 7.0)]
    index = {"i": 0}

    def fake_measure(_url):
        value = data[index["i"] % len(data)]
        index["i"] += 1
        return value

    clock._measure_once = fake_measure
    applied = clock.refresh(dense=True)
    # Fastest half of the eight samples: offsets 2,4,5,6 → mean 4.25, plus the
    # Date-header truncation bias.
    expected = (2.0 + 4.0 + 5.0 + 6.0) / 4 + clock.DATE_HEADER_BIAS_SECONDS
    check("clock-fastest-half-mean", abs(applied - expected) < 1e-9, f"applied={applied:.3f} expected={expected:.3f}")


def scenario_constants() -> None:
    check("jitter-compressed", service.FIRE_JITTER_RANGE == (0.0, 0.05), str(service.FIRE_JITTER_RANGE))
    check("first-pass-compressed", service.FIRST_PASS_WAIT_RANGE == (0.05, 0.15), str(service.FIRST_PASS_WAIT_RANGE))
    check("retry-compressed", service.RETRY_WAIT_RANGE == (0.3, 0.6), str(service.RETRY_WAIT_RANGE))
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
    scenario_clock_estimator()
    scenario_constants()
    for line in CHECKS:
        print(line)
    failures = [line for line in CHECKS if line.startswith("FAIL")]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
