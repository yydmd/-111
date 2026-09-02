from __future__ import annotations

import re


_TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def normalize_time(value: str) -> str:
    """Accept H:MM or HH:MM and return a canonical HH:MM value."""
    match = _TIME_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError("时间请填写为 HH:MM，例如 08:30")
    hour = int(match.group(1))
    if not 0 <= hour <= 23:
        raise ValueError("小时必须在 00 到 23 之间")
    return f"{hour:02d}:{match.group(2)}"


def validate_reservation_time_range(start_time: str, end_time: str) -> None:
    """Validate the constraints that can be checked before contacting ChaoXing."""
    start_hour, start_minute = map(int, start_time.split(":"))
    end_hour, end_minute = map(int, end_time.split(":"))
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    if end_minutes <= start_minutes:
        raise ValueError("使用结束时间必须晚于使用开始时间")
    if end_minutes - start_minutes > 12 * 60:
        raise ValueError("单次使用时长不能超过 12 小时")
