"""Server clock alignment for the opening-window reservation.

The platform opens reservations at a fixed wall-clock moment in ITS OWN time.
A drifting local Windows clock (no w32tm) then fires early or late. We measure
the offset between the local clock and ChaoXing's ``Date`` response headers and
expose a ``server_now()`` the submit gate can wait on.

The measurement is deliberately cheap: any response (even a redirect) carries
a ``Date`` header with one-second granularity; we sample the midpoint of the
request window and take the median of a few samples.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
PROBE_URLS = (
    "https://office.chaoxing.com/front/third/apps/seat/select",
    "https://passport2.chaoxing.com/mlogin?loginType=1",
)
MAX_SAMPLES = 3
TTL_SECONDS = 10 * 60
# Refuse absurd offsets: if the "server" claims more than this, keep local time.
SANITY_LIMIT_SECONDS = 300.0

_session = requests.Session()
_session.trust_env = False
_lock = threading.Lock()
_offset = 0.0
_measured_at = 0.0
_last_error = ""


def _measure_once(url: str) -> float | None:
    before = time.time()
    response = _session.get(url, timeout=(2, 4), allow_redirects=False,
                            headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    after = time.time()
    raw_date = response.headers.get("Date", "")
    if not raw_date:
        return None
    try:
        server = parsedate_to_datetime(raw_date).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return server - (before + after) / 2


def refresh() -> float:
    """Re-measure the server offset; returns the applied offset in seconds."""
    global _offset, _measured_at, _last_error
    samples: list[float] = []
    for url in PROBE_URLS:
        if len(samples) >= MAX_SAMPLES:
            break
        try:
            value = _measure_once(url)
        except requests.RequestException as exc:
            logger.debug("clock probe failed for %s: %s", url, exc.__class__.__name__)
            continue
        if value is not None:
            samples.append(value)
    with _lock:
        if samples:
            samples.sort()
            offset = samples[len(samples) // 2]
            if abs(offset) <= SANITY_LIMIT_SECONDS:
                _offset = offset
                _last_error = ""
            else:
                _last_error = f"测得异常时钟偏差 {offset:+.1f}s，已忽略"
        else:
            _last_error = _last_error or "未能取得服务器时间，按本地时钟执行"
        _measured_at = time.time()
        return _offset


def server_offset() -> float:
    """Current best offset (server time - local time), refreshed when stale."""
    with _lock:
        age = time.time() - _measured_at
        stale = age > TTL_SECONDS or (_measured_at == 0.0)
    if stale:
        refresh()
    with _lock:
        return _offset


def server_now() -> float:
    """Epoch seconds on the platform's clock."""
    return time.time() + server_offset()


def server_time_of_day_minutes(now_epoch: float | None = None) -> float:
    moment = dt.datetime.fromtimestamp(now_epoch if now_epoch is not None else server_now(), SHANGHAI)
    return moment.hour * 60 + moment.minute + moment.second / 60


def status() -> dict:
    with _lock:
        return {
            "offset_seconds": round(_offset, 3),
            "age_seconds": round(time.time() - _measured_at, 1) if _measured_at else None,
            "error": _last_error,
        }


def warm_start() -> None:
    """Measure once in the background so service startup stays fast."""
    threading.Thread(target=refresh, name="clock-warm", daemon=True).start()
