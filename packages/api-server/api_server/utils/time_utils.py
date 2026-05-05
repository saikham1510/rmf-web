from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


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


def sim_to_real(sim_time: int, now_sim: int, now_real: int) -> int:
    """Convert simulation time to real (wall-clock) time using current time pairs.

    Args:
        sim_time: Timestamp in simulation time (milliseconds)
        now_sim: Current simulation time (milliseconds), from ROS clock
        now_real: Current real wall-clock time (milliseconds), from time.time()

    Returns:
        Timestamp in real (wall-clock) time (milliseconds)
    """
    return int(now_real + (sim_time - now_sim))


def real_to_sim(real_time: int, now_real: int, now_sim: int) -> int:
    """Convert real (wall-clock) time to simulation time using current time pairs.

    Args:
        real_time: Timestamp in real (wall-clock) time (milliseconds)
        now_real: Current real wall-clock time (milliseconds), from time.time()
        now_sim: Current simulation time (milliseconds), from ROS clock

    Returns:
        Timestamp in simulation time (milliseconds)
    """
    return int(now_sim + (real_time - now_real))
