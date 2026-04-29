from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Optional simulation offset in milliseconds (wall_time - sim_time).
# When None, conversions are identity (assume wall==sim).
_sim_offset_ms: Optional[int] = None


def now_wall_millis() -> int:
    """Return current wall-clock time in UTC as unix milliseconds."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def datetime_to_wall_millis(dt: datetime) -> int:
    """Convert a timezone-aware or naive datetime to unix millis (UTC).

    Naive datetimes are treated as UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def wall_millis_to_datetime(ms: int) -> datetime:
    """Convert unix millis (UTC) to a timezone-aware datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def set_sim_offset_ms(offset_ms: int) -> None:
    """Set the sim offset such that: wall_ms - sim_ms = offset_ms."""
    global _sim_offset_ms
    _sim_offset_ms = int(offset_ms)


def clear_sim_offset() -> None:
    """Clear any previously set sim offset (fall back to wall==sim)."""
    global _sim_offset_ms
    _sim_offset_ms = None


def compute_and_set_offset(sim_ms: int, wall_ms: int) -> int:
    """Compute offset from sim and wall times, set it, and return it."""
    offset = wall_ms - sim_ms
    set_sim_offset_ms(offset)
    return offset


def wall_to_sim_millis(wall_ms: int) -> int:
    """Convert a wall-clock millis to sim millis using the configured offset.

    If no offset is configured, returns the input unchanged.
    """
    if _sim_offset_ms is None:
        return int(wall_ms)
    return int(wall_ms - _sim_offset_ms)


def sim_to_wall_millis(sim_ms: int) -> int:
    """Convert sim millis to wall-clock millis using the configured offset.

    If no offset is configured, returns the input unchanged.
    """
    if _sim_offset_ms is None:
        return int(sim_ms)
    return int(sim_ms + _sim_offset_ms)
