"""
Recurrence calculation utilities for scheduled tasks.

Implements logic to calculate next occurrence based on schedule rules:
- period: day, hour, minute, or day-of-week (monday, tuesday, etc.)
- at: time in HH:MM format (for daily/weekly schedules)
- every: repeat interval (e.g., every 5 minutes)
- until: stop recurring after this date (optional)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


def calculate_next_occurrence(
    current_time: datetime,
    start_from: Optional[datetime],
    period: str,
    at: Optional[str],
    every: Optional[int],
    until: Optional[datetime],
) -> Optional[datetime]:
    """
    Calculate the next occurrence of a recurring schedule.

    Args:
        current_time: Current time (typically now in UTC)
        start_from: When the schedule starts (first possible occurrence)
        period: "day", "hour", "minute", or day-of-week ("monday", etc.)
        at: Time in HH:MM format (required for day/weekly, ignored for minute)
        every: Repeat every N periods (e.g., every=5 with period=minute → every 5 min)
        until: Stop recurring after this date (optional)

    Returns:
        Next occurrence datetime in UTC, or None if schedule has ended
    """

    if start_from is None:
        return None

    # Ensure all times are UTC aware
    current_time = _ensure_utc(current_time)
    start_from = _ensure_utc(start_from)
    until = _ensure_utc(until) if until else None

    # If we're before start_from, the first occurrence is at start_from
    if current_time < start_from:
        # But only if it's still within the until window
        if until is None or start_from <= until:
            return start_from
        else:
            return None

    # If we've passed the until date, no more occurrences
    if until is not None and current_time >= until:
        return None

    # Parse the 'at' time (HH:MM format)
    at_hour, at_minute = _parse_at_time(at) if at else (0, 0)

    # Calculate based on period
    if period == "minute":
        return _next_minute_occurrence(current_time, every, until)
    elif period == "hour":
        return _next_hour_occurrence(current_time, every, at_minute, until)
    elif period == "day":
        return _next_day_occurrence(current_time, every, at_hour, at_minute, until)
    elif period in (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ):
        return _next_weekday_occurrence(current_time, period, at_hour, at_minute, until)
    else:
        return None


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is UTC aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_at_time(at: Optional[str]) -> tuple[int, int]:
    """
    Parse 'at' time string in HH:MM format.

    Returns:
        (hour, minute) tuple, defaults to (0, 0) if invalid
    """
    if not at:
        return (0, 0)
    try:
        parts = at.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return (hour, minute)
    except (ValueError, IndexError):
        return (0, 0)


def _next_minute_occurrence(
    current_time: datetime,
    every: Optional[int],
    until: Optional[datetime],
) -> Optional[datetime]:
    """Calculate next occurrence for minute-based schedules."""
    interval_minutes = every if every and every > 0 else 1

    # Round up to next occurrence
    next_run = current_time + timedelta(minutes=interval_minutes)

    if until and next_run > until:
        return None

    return next_run


def _next_hour_occurrence(
    current_time: datetime,
    every: Optional[int],
    at_minute: int,
    until: Optional[datetime],
) -> Optional[datetime]:
    """Calculate next occurrence for hour-based schedules."""
    interval_hours = every if every and every > 0 else 1

    # Next occurrence at the specified minute
    next_run = current_time.replace(minute=at_minute, second=0, microsecond=0)

    # If that time has passed in this hour, go to next hour(s)
    if next_run <= current_time:
        next_run += timedelta(hours=interval_hours)
    else:
        # If next_run is in the future this hour, check if it's less than interval
        # For now, just use it
        pass

    if until and next_run > until:
        return None

    return next_run


def _next_day_occurrence(
    current_time: datetime,
    every: Optional[int],
    at_hour: int,
    at_minute: int,
    until: Optional[datetime],
) -> Optional[datetime]:
    """Calculate next occurrence for day-based schedules."""
    interval_days = every if every and every > 0 else 1

    # Next occurrence at specified time
    next_run = current_time.replace(
        hour=at_hour, minute=at_minute, second=0, microsecond=0
    )

    # If that time has passed today, go to next day(s)
    if next_run <= current_time:
        next_run += timedelta(days=interval_days)

    if until and next_run > until:
        return None

    return next_run


def _next_weekday_occurrence(
    current_time: datetime,
    period: str,
    at_hour: int,
    at_minute: int,
    until: Optional[datetime],
) -> Optional[datetime]:
    """Calculate next occurrence for weekly schedules (e.g., 'monday', 'tuesday')."""

    # Map period names to weekday numbers (Monday=0, Sunday=6)
    day_map = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    target_weekday = day_map.get(period.lower())
    if target_weekday is None:
        return None

    # Start from tomorrow (give next week a chance)
    next_run = current_time.replace(
        hour=at_hour, minute=at_minute, second=0, microsecond=0
    )

    # If that time has passed today, move to tomorrow
    if next_run <= current_time:
        next_run += timedelta(days=1)

    # Find the next occurrence of target_weekday
    current_weekday = next_run.weekday()
    days_ahead = (target_weekday - current_weekday) % 7

    if days_ahead == 0:
        # It's the same day of week but time has passed, go to next week
        if next_run <= current_time:
            days_ahead = 7

    next_run += timedelta(days=days_ahead)

    if until and next_run > until:
        return None

    return next_run


def is_schedule_active(until: Optional[datetime]) -> bool:
    """Check if a schedule is still active (hasn't reached until date)."""
    if until is None:
        return True
    return datetime.now(timezone.utc) <= until
