from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


NOVOSIBIRSK_TZ = ZoneInfo("Asia/Novosibirsk")


def now_local() -> datetime:
    return datetime.now(tz=NOVOSIBIRSK_TZ)


def require_local_datetime(value: object, *, name: str = "datetime") -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} needs datetime")
    try:
        offset = value.utcoffset()
    except Exception as error:
        raise ValueError(f"{name} needs timezone-aware datetime") from error
    if value.tzinfo is None or offset is None:
        raise ValueError(f"{name} needs timezone-aware datetime")
    if (
        not isinstance(value.tzinfo, ZoneInfo)
        or value.tzinfo.key != NOVOSIBIRSK_TZ.key
    ):
        raise ValueError(
            f"{name} needs Asia/Novosibirsk timezone, got {_timezone_label(value, offset)}"
        )
    return value


def _timezone_label(value: datetime, offset: timedelta) -> str:
    if isinstance(value.tzinfo, ZoneInfo):
        return value.tzinfo.key
    tz_name = None
    if value.tzinfo is not None:
        try:
            tz_name = value.tzinfo.tzname(value)
        except Exception:
            tz_name = None
    if isinstance(tz_name, str) and tz_name.strip():
        return " ".join(tz_name.split())
    return _format_utc_offset(offset)


def _format_utc_offset(offset: timedelta) -> str:
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if seconds:
        return f"UTC{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
