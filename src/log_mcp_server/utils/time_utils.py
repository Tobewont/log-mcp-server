"""Time / timezone helpers.

Internally we always use timezone-aware UTC ``datetime`` objects to avoid
naive/aware mixing. Conversion to the configured display timezone happens
only when formatting for the user.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .errors import ValidationError


def get_timezone(name: str):
    """Return a tzinfo for ``name`` using zoneinfo or pytz fallback."""
    try:
        import zoneinfo

        return zoneinfo.ZoneInfo(name)
    except Exception:
        try:
            import pytz

            return pytz.timezone(name)
        except Exception as e:
            raise ValidationError(f"Invalid timezone: {name}") from e


def now_utc() -> datetime:
    """Current UTC time, timezone-aware."""
    return datetime.now(timezone.utc)


def parse_user_time(value: str) -> datetime:
    """Parse a user-provided time string into a tz-aware UTC datetime.

    Accepts ISO 8601 / RFC3339 strings (with or without ``Z``).  If the
    input has no timezone, it is treated as UTC.
    """
    if not value:
        raise ValidationError("Time value cannot be empty")

    text = value.strip()
    # Accept "Z" suffix as UTC. Only strip a *trailing* Z so that
    # malformed inputs like "2025-Z01-01" don't get silently mangled.
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValidationError(
            f"Invalid time format: {value!r}. Use RFC3339, e.g. "
            "'2025-01-01T00:00:00Z' or '2025-01-01T00:00:00+08:00'."
        ) from e

    if dt.tzinfo is None:
        # Treat naive input as UTC for predictable behaviour.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def from_unix_ns(ns: int | str) -> datetime:
    """Convert a Loki nanosecond timestamp to tz-aware UTC datetime."""
    ns_int = int(ns)
    return datetime.fromtimestamp(ns_int / 1_000_000_000, tz=timezone.utc)


def to_unix_ns(dt: datetime) -> int:
    """Convert a tz-aware datetime to Unix nanoseconds.

    Naive datetimes are explicitly rejected to avoid silent local-time bugs.
    """
    if dt.tzinfo is None:
        raise ValidationError("Datetime must be timezone-aware")
    return int(dt.timestamp() * 1_000_000_000)


def format_in_tz(dt: datetime, tz_name: str) -> str:
    """Format a tz-aware datetime in the configured display timezone."""
    if dt.tzinfo is None:
        raise ValidationError("Datetime must be timezone-aware")
    tz = get_timezone(tz_name)
    return dt.astimezone(tz).isoformat()


def resolve_time_range(
    start: Optional[str],
    end: Optional[str],
    default_minutes: int,
) -> tuple[datetime, datetime]:
    """Resolve user-provided ``start``/``end`` strings to UTC datetimes.

    - Both missing  -> ``[now - default_minutes, now]``
    - Only end given -> ``[end - default_minutes, end]``
    - Only start given -> ``[start, now]``
    - Both given -> ``[start, end]``

    All returned datetimes are UTC tz-aware. ``start`` must be < ``end``.
    """
    end_dt = parse_user_time(end) if end else now_utc()
    start_dt = (
        parse_user_time(start)
        if start
        else end_dt - timedelta(minutes=default_minutes)
    )

    if start_dt >= end_dt:
        raise ValidationError(
            f"Start time must be before end time (got start={start_dt.isoformat()}, "
            f"end={end_dt.isoformat()})."
        )

    return start_dt, end_dt
