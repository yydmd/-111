"""Single-instance watchdog for the local ChaoXing reservation service.

The watchdog starts ``main.py``, probes ``/health``, and replaces the
service process after an abnormal exit or when the HTTP endpoint stops responding.
It is intentionally small: persistent scheduling remains in SQLite/APScheduler,
while this process only provides supervision.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import msvcrt
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import IO

import requests

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = PYTHON if PYTHON.exists() else Path(sys.executable)
SERVICE_COMMAND = [str(PYTHON), "-u", "main.py"]

DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
WATCHDOG_LOG_PATH = LOG_DIR / "watchdog.log"
SERVICE_LOG_PATH = LOG_DIR / "service.log"
LOCK_PATH = DATA_DIR / "watchdog.lock"

HEALTH_URL = "http://127.0.0.1:8787/health"
HEALTH_TIMEOUT_SECONDS = 3.0
HEALTH_INTERVAL_SECONDS = 7.0
STARTUP_GRACE_SECONDS = 20.0
HEALTH_FAILURE_LIMIT = 3
INITIAL_RESTART_DELAY_SECONDS = 5.0
MAX_RESTART_DELAY_SECONDS = 60.0
STABLE_UPTIME_SECONDS = 60.0
RUN_PROTECTION_SECONDS = 75.0
LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3

stop_requested = False
http = requests.Session()
http.trust_env = False

# Named mutex guard: the file lock below is kept for compatibility, but a
# same-second double start once slipped past it in practice. A kernel mutex is
# authoritative and prevents two watchdogs from supervising one service.
ERROR_ALREADY_EXISTS = 183
_watchdog_mutex_handle: wintypes.HANDLE | None = None


def acquire_watchdog_mutex() -> bool:
    global _watchdog_mutex_handle
    if _watchdog_mutex_handle:
        return True
    identity = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    _watchdog_mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\ChaoxingSeatWatchdog-{suffix}")
    if not _watchdog_mutex_handle:
        return False
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(_watchdog_mutex_handle)
        _watchdog_mutex_handle = None
        return False
    return True


def release_watchdog_mutex() -> None:
    global _watchdog_mutex_handle
    if _watchdog_mutex_handle:
        ctypes.windll.kernel32.CloseHandle(_watchdog_mutex_handle)
        _watchdog_mutex_handle = None


def timestamp() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{timestamp()}] {message}", flush=True)


def file_log(handle: IO[str], message: str) -> None:
    line = f"[{timestamp()}] {message}\n"
    handle.write(line)
    handle.flush()


def rotate_log(path: Path) -> None:
    """Keep local logs bounded without touching an actively written file."""
    if not path.exists() or path.stat().st_size < LOG_ROTATE_BYTES:
        return
    for index in range(LOG_BACKUPS, 0, -1):
        source = path.with_suffix(path.suffix + f".{index}")
        target = path.with_suffix(path.suffix + f".{index + 1}")
        if source.exists():
            if index == LOG_BACKUPS:
                source.unlink()
            else:
                source.replace(target)
    shutil.move(str(path), str(path.with_suffix(path.suffix + ".1")))


def acquire_watchdog_lock():
    """Prevent two watchdogs from supervising the same service."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    handle.seek(0, os.SEEK_END)
    handle.write(f"{os.getpid()}\n".encode("utf-8"))
    handle.flush()
    return handle


def request_stop(_signum=None, _frame=None) -> None:
    global stop_requested
    stop_requested = True


def service_is_healthy() -> bool:
    try:
        # Do not reuse a connection at the same boundary as Uvicorn's default
        # keep-alive timeout; that produced false one-off failures in practice.
        response = http.get(HEALTH_URL, timeout=HEALTH_TIMEOUT_SECONDS, headers={"Connection": "close"})
        return response.status_code == 200
    except requests.RequestException:
        return False


def spawn_service() -> tuple[subprocess.Popen, IO[bytes]]:
    rotate_log(SERVICE_LOG_PATH)
    service_log = open(SERVICE_LOG_PATH, "ab", buffering=0)
    child = subprocess.Popen(
        SERVICE_COMMAND,
        cwd=str(ROOT),
        stdout=service_log,
        stderr=subprocess.STDOUT,
    )
    return child, service_log


def stop_service(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def has_recent_active_run() -> bool:
    """Avoid hard-stopping a live submission just because HTTP is unavailable."""
    try:
        connection = sqlite3.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True, timeout=1)
        try:
            rows = connection.execute(
                "SELECT started_at FROM reservation_runs WHERE status IN ('PENDING', 'RUNNING')"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    for (started_at,) in rows:
        try:
            started = dt.datetime.fromisoformat(str(started_at)).replace(tzinfo=None)
        except ValueError:
            continue
        if (now - started).total_seconds() < RUN_PROTECTION_SECONDS:
            return True
    return False


def run_watchdog() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rotate_log(WATCHDOG_LOG_PATH)
    if not acquire_watchdog_mutex():
        note = f"[{timestamp()}] another watchdog owns the named mutex; exiting."
        print(note, flush=True)
        try:
            with open(WATCHDOG_LOG_PATH, "a", encoding="utf-8") as existing:
                existing.write(note + "\n")
        except OSError:
            pass
        return 0
    lock = acquire_watchdog_lock()
    if lock is None:
        log("Another watchdog is already running; exiting.")
        release_watchdog_mutex()
        return 0

    with open(WATCHDOG_LOG_PATH, "a", encoding="utf-8", buffering=1) as watchdog_log:
        file_log(watchdog_log, f"watchdog started, pid={os.getpid()}, command={SERVICE_COMMAND}")
        restart_delay = INITIAL_RESTART_DELAY_SECONDS

        child: subprocess.Popen | None = None
        service_log: IO[bytes] | None = None
        try:
            while not stop_requested:
                child, service_log = spawn_service()
                started_at = time.monotonic()
                health_failures = 0
                file_log(watchdog_log, f"service started, pid={child.pid}")

                while not stop_requested:
                    if child.poll() is not None:
                        file_log(watchdog_log, f"service exited unexpectedly, code={child.returncode}")
                        break

                    uptime = time.monotonic() - started_at
                    if uptime < STARTUP_GRACE_SECONDS:
                        time.sleep(1.0)
                        continue

                    if service_is_healthy():
                        if health_failures:
                            file_log(watchdog_log, "service recovered")
                        health_failures = 0
                        time.sleep(HEALTH_INTERVAL_SECONDS)
                        continue

                    health_failures += 1
                    file_log(watchdog_log, f"health check failed ({health_failures}/{HEALTH_FAILURE_LIMIT})")
                    if health_failures < HEALTH_FAILURE_LIMIT:
                        time.sleep(HEALTH_INTERVAL_SECONDS)
                        continue

                    if has_recent_active_run():
                        file_log(watchdog_log, "service is unhealthy but a reservation is active; delaying restart")
                        health_failures = 0
                        time.sleep(HEALTH_INTERVAL_SECONDS)
                        continue
                    file_log(watchdog_log, "service appears hung; stopping it for restart")
                    stop_service(child)
                    break

                if stop_requested:
                    stop_service(child)
                    service_log.close()
                    service_log = None
                    file_log(watchdog_log, "watchdog stop requested; service stopped")
                    break

                service_log.close()
                service_log = None
                uptime = time.monotonic() - started_at
                if uptime >= STABLE_UPTIME_SECONDS:
                    restart_delay = INITIAL_RESTART_DELAY_SECONDS
                else:
                    restart_delay = min(restart_delay * 2, MAX_RESTART_DELAY_SECONDS)

                file_log(watchdog_log, f"restarting service in {restart_delay:.0f}s")
                deadline = time.monotonic() + restart_delay
                while not stop_requested and time.monotonic() < deadline:
                    time.sleep(0.5)

            return 0
        except Exception as exc:
            file_log(watchdog_log, f"watchdog fatal error: {exc!r}")
            if child is not None:
                stop_service(child)
            if service_log is not None:
                service_log.close()
            return 1
        finally:
            lock.close()
            file_log(watchdog_log, "watchdog stopped")
            release_watchdog_mutex()


def main() -> int:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return run_watchdog()


if __name__ == "__main__":
    raise SystemExit(main())
