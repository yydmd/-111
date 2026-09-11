"""Shared lifecycle definitions used by the service, API and watchdog."""
from datetime import UTC, datetime, timedelta

ACTIVE_STATUSES = ("PENDING", "RUNNING", "WAITING_LOGIN", "WAITING_OPEN", "WAITING_USER", "VERIFYING")
BROWSER_RUN_SECONDS = 300
VERIFY_SECONDS = 20
HEARTBEAT_GRACE_SECONDS = 30
PREPARE_LEAD_SECONDS = 600
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SCHEDULED_TRIGGERS = ("scheduled", "scheduled_catchup")


def weekday_name(moment):
    return WEEKDAY_NAMES[moment.weekday()]


def scheduled_opening(run_time, current, lead_seconds=PREPARE_LEAD_SECONDS):
    hour, minute = map(int, run_time.split(":"))
    opening = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if 0 < (opening + timedelta(days=1) - current).total_seconds() <= lead_seconds:
        opening += timedelta(days=1)
    return opening


def known_unsubmitted_browser_run(run):
    """Only repair historical browser rows with no evidence of a released POST."""
    return (not run.possibly_submitted
            and run.request_snapshot.get("execution_mode") == "browser"
            and not any(not isinstance(item, dict) or item.get("submitted") for item in run.attempt_details))


def live_browser_run(heartbeat, expires, now=None):
    now = now or datetime.now(UTC).replace(tzinfo=None)
    if isinstance(heartbeat, str):
        heartbeat = datetime.fromisoformat(heartbeat)
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    return bool(heartbeat and expires and now < expires + timedelta(seconds=VERIFY_SECONDS + 10)
                and now - heartbeat < timedelta(seconds=HEARTBEAT_GRACE_SECONDS))
