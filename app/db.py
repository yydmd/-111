from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE_PATH = DATA_DIR / "app.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"
SCHEMA_VERSION = 9
_SELECT_CONTEXT_PATH_RE = re.compile(r"/front/(?:third/)?apps/seat/select", re.I)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    plans: Mapped[list["ReservationPlan"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class ReservationPlan(Base):
    __tablename__ = "reservation_plans"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    room_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)
    run_time: Mapped[str] = mapped_column(String(5), default="06:59", nullable=False)
    day_offset: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    weekdays_json: Mapped[str] = mapped_column(Text, default='["Monday","Tuesday","Wednesday","Thursday","Friday"]', nullable=False)
    slider_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Sanitized stable query params for the seat-select page (without ephemeral
    # date, token, captcha or signature values). They can come from automatic
    # discovery or from the advanced manual fallback.
    select_params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    select_context_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    select_context_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    select_context_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    account: Mapped[Account] = relationship(back_populates="plans")
    seats: Mapped[list["PlanSeat"]] = relationship(back_populates="plan", cascade="all, delete-orphan", order_by="PlanSeat.priority")

    @property
    def weekdays(self) -> list[str]:
        try:
            return json.loads(self.weekdays_json)
        except (TypeError, json.JSONDecodeError):
            return []

    @property
    def select_params(self) -> dict[str, str] | None:
        try:
            data = json.loads(self.select_params_json or "")
        except (TypeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data else None


class PlanSeat(Base):
    __tablename__ = "plan_seats"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("reservation_plans.id", ondelete="CASCADE"), nullable=False)
    seat_num: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan: Mapped[ReservationPlan] = relationship(back_populates="seats")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ReservationRun(Base):
    __tablename__ = "reservation_runs"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("reservation_plans.id", ondelete="SET NULL"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    candidate_seats_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_seat: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duplicate_of_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Per-seat probe results as a JSON list; None for non-probe runs.
    probe_results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Audit metadata only. Never store request tokens, cookies, passwords or enc.
    parameter_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Immutable, non-secret plan values accepted when this run entered the queue.
    request_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def candidate_seats(self) -> list[str]:
        try:
            return json.loads(self.candidate_seats_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []

    @property
    def probe_results(self) -> list[dict]:
        try:
            data = json.loads(self.probe_results_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    @property
    def attempt_details(self) -> list[dict]:
        try:
            data = json.loads(self.attempt_details_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    @property
    def request_snapshot(self) -> dict:
        try:
            data = json.loads(self.request_snapshot_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(connection, _) -> None:
    # Web workers, the scheduler thread, reservation executors and the notify
    # pool all touch this file; WAL keeps readers unblocked by the frequent
    # run-state commits and busy_timeout rides out writer locks instead of
    # raising (a raise inside execute_plan's finally block would swallow the
    # real run result).
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _backup_database() -> None:
    if not DATABASE_PATH.exists() or DATABASE_PATH.stat().st_size == 0:
        return
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"app-before-v{SCHEMA_VERSION}-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(DATABASE_PATH, backup_path)


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _canonical_legacy_context_path(value: object) -> str | None:
    """Normalize v8's accidental full-URL values without trusting a host."""
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if (parsed.hostname or "").lower().rstrip(".") != "office.chaoxing.com":
            return None
        path = parsed.path
    else:
        path = raw
    return path if _SELECT_CONTEXT_PATH_RE.fullmatch(path) else None


def _migrate_database() -> None:
    """Rebuild the small local SQLite schema once, preserving existing credentials and history."""
    if not DATABASE_PATH.exists():
        return
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        version = _schema_version(connection)
        if version >= SCHEMA_VERSION:
            return
        _backup_database()
        if version == 2:
            # Before snapshot columns existed, plan IDs could already have been reused.
            # Do not let those ambiguous old rows masquerade as the current plan.
            connection.execute(
                "UPDATE reservation_runs SET plan_id=NULL, plan_name='迁移前历史计划', account_name='迁移前账号' WHERE target_date IS NULL"
            )
            version = 3
        if version == 3:
            connection.execute("ALTER TABLE reservation_runs ADD COLUMN request_fingerprint VARCHAR(256)")
            connection.execute("ALTER TABLE reservation_runs ADD COLUMN duplicate_of_run_id INTEGER")
            connection.execute("ALTER TABLE reservation_runs ADD COLUMN duplicate_override BOOLEAN NOT NULL DEFAULT 0")
            connection.execute("PRAGMA user_version=4")
            version = 4
        if version == 4:
            # v5: seat-select link params on plans, per-seat probe results on runs.
            connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_params_json TEXT")
            connection.execute("ALTER TABLE reservation_runs ADD COLUMN probe_results_json TEXT")
            connection.execute("PRAGMA user_version=5")
            version = 5
        if version == 5:
            # v6: automatic context provenance and non-sensitive attempt audit.
            connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_context_source VARCHAR(32)")
            connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_context_checked_at DATETIME")
            connection.execute("ALTER TABLE reservation_runs ADD COLUMN parameter_source VARCHAR(32)")
            connection.execute("ALTER TABLE reservation_runs ADD COLUMN attempt_details_json TEXT")
            connection.execute("PRAGMA user_version=6")
            version = 6
        if version == 6:
            # v7: preserve select route variants and the immutable request
            # configuration of queued work.
            connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_context_path VARCHAR(255)")
            connection.execute("ALTER TABLE reservation_runs ADD COLUMN request_snapshot_json TEXT")
            connection.execute("PRAGMA user_version=7")
            version = 7
        if version == 7:
            # v8: local key/value settings (notification channel).
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key VARCHAR(64) PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute("PRAGMA user_version=8")
            version = 8
        if version == 8:
            # v9: early automatic discovery accidentally persisted a complete
            # office.chaoxing.com URL where every API write expects a relative
            # select path. Repair existing plans during startup.
            for plan_id, raw_path in connection.execute(
                "SELECT id, select_context_path FROM reservation_plans WHERE select_context_path IS NOT NULL"
            ):
                canonical_path = _canonical_legacy_context_path(raw_path)
                if canonical_path != raw_path:
                    connection.execute(
                        "UPDATE reservation_plans SET select_context_path=? WHERE id=?",
                        (canonical_path, plan_id),
                    )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
            return
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            BEGIN;
            CREATE TABLE accounts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(120) NOT NULL,
                username VARCHAR(255) NOT NULL UNIQUE,
                password_blob BLOB NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE reservation_plans_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                name VARCHAR(120) NOT NULL,
                room_id VARCHAR(64) NOT NULL,
                start_time VARCHAR(5) NOT NULL,
                end_time VARCHAR(5) NOT NULL,
                run_time VARCHAR(5) NOT NULL DEFAULT '06:59',
                day_offset INTEGER NOT NULL DEFAULT 1,
                weekdays_json TEXT NOT NULL,
                slider_enabled BOOLEAN NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                enabled BOOLEAN NOT NULL DEFAULT 1
            );
            CREATE TABLE plan_seats_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL REFERENCES reservation_plans(id) ON DELETE CASCADE,
                seat_num VARCHAR(16) NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE reservation_runs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER REFERENCES reservation_plans(id) ON DELETE SET NULL,
                account_id INTEGER,
                plan_name VARCHAR(120),
                account_name VARCHAR(120),
                target_date VARCHAR(10),
                request_fingerprint VARCHAR(256),
                candidate_seats_json TEXT,
                selected_seat VARCHAR(16),
                error_code VARCHAR(64),
                duplicate_of_run_id INTEGER,
                duplicate_override BOOLEAN NOT NULL DEFAULT 0,
                trigger VARCHAR(32) NOT NULL,
                status VARCHAR(32) NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                started_at DATETIME NOT NULL,
                finished_at DATETIME
            );
            INSERT INTO accounts_new SELECT id, name, username, password_blob, enabled, created_at FROM accounts;
            INSERT INTO reservation_plans_new SELECT id, account_id, name, room_id, start_time, end_time, run_time, day_offset, weekdays_json, slider_enabled, max_attempts, enabled FROM reservation_plans;
            INSERT INTO plan_seats_new SELECT id, plan_id, seat_num, priority FROM plan_seats;
            INSERT INTO reservation_runs_new (id, plan_id, account_id, plan_name, account_name, target_date, request_fingerprint, candidate_seats_json, selected_seat, error_code, duplicate_of_run_id, duplicate_override, trigger, status, message, started_at, finished_at)
            SELECT r.id,
                   CASE WHEN p.id IS NULL THEN NULL ELSE r.plan_id END,
                   p.account_id,
                   COALESCE(p.name, '已删除计划'),
                   a.name,
                   NULL,
                   NULL,
                   NULL,
                   NULL,
                   NULL,
                   NULL,
                   0,
                   r.trigger, r.status, r.message, r.started_at, r.finished_at
            FROM reservation_runs r
            LEFT JOIN reservation_plans p ON p.id = r.plan_id
            LEFT JOIN accounts a ON a.id = p.account_id;
            DROP TABLE plan_seats;
            DROP TABLE reservation_runs;
            DROP TABLE reservation_plans;
            DROP TABLE accounts;
            ALTER TABLE accounts_new RENAME TO accounts;
            ALTER TABLE reservation_plans_new RENAME TO reservation_plans;
            ALTER TABLE plan_seats_new RENAME TO plan_seats;
            ALTER TABLE reservation_runs_new RENAME TO reservation_runs;
            PRAGMA user_version=4;
            COMMIT;
            """
        )
        connection.execute("PRAGMA foreign_keys=ON")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key migration check failed: {violations}")
        # The legacy rebuild above recreates the v4 layout; bring it to the
        # current version as well.
        connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_params_json TEXT")
        connection.execute("ALTER TABLE reservation_runs ADD COLUMN probe_results_json TEXT")
        connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_context_source VARCHAR(32)")
        connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_context_checked_at DATETIME")
        connection.execute("ALTER TABLE reservation_runs ADD COLUMN parameter_source VARCHAR(32)")
        connection.execute("ALTER TABLE reservation_runs ADD COLUMN attempt_details_json TEXT")
        connection.execute("ALTER TABLE reservation_plans ADD COLUMN select_context_path VARCHAR(255)")
        connection.execute("ALTER TABLE reservation_runs ADD COLUMN request_snapshot_json TEXT")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_database()
    Base.metadata.create_all(engine)


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
