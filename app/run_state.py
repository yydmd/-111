"""Shared lifecycle definitions used by the service, API and watchdog."""
from datetime import UTC, datetime, timedelta

ACTIVE_STATUSES = ("PENDING", "RUNNING", "WAITING_LOGIN", "WAITING_OPEN", "WAITING_USER", "VERIFYING")
BROWSER_RUN_SECONDS = 300
VERIFY_SECONDS = 20
HEARTBEAT_GRACE_SECONDS = 30


def live_browser_run(heartbeat, expires, now=None):
    now = now or datetime.now(UTC).replace(tzinfo=None)
    if isinstance(heartbeat, str):
        heartbeat = datetime.fromisoformat(heartbeat)
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    return bool(heartbeat and expires and now < expires + timedelta(seconds=VERIFY_SECONDS + 10)
                and now - heartbeat < timedelta(seconds=HEARTBEAT_GRACE_SECONDS))
