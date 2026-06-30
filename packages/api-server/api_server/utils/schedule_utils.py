from __future__ import annotations

import re
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta, timezone
from typing import Optional

import schedule

from api_server.models import tortoise_models as ttm


def _ensure_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_planned_end(planned_end_at: Optional[str]) -> Optional[dt_time]:
    if not planned_end_at:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", planned_end_at)
    if not m:
        return None
    h = int(m.group(1))
    m2 = int(m.group(2))
    return dt_time(hour=h, minute=m2)


def _occurrence_allowed(
    schedule_row: ttm.ScheduledTaskSchedule,
    candidate_dt_utc: datetime,
    task_row: Optional[ttm.ScheduledTask] = None,
) -> bool:
    # until check
    if schedule_row.until is not None:
        until_utc = _ensure_utc_aware(schedule_row.until)
        if candidate_dt_utc > until_utc:
            return False

    # except_dates stored as ISO date strings (YYYY-MM-DD)
    if task_row is not None and getattr(task_row, "except_dates", None):
        try:
            date_iso = candidate_dt_utc.date().isoformat()
            if date_iso in task_row.except_dates:
                return False
        except Exception:
            pass

    # planned_end_at (HH:MM local) check
    planned = _parse_planned_end(schedule_row.planned_end_at)
    if planned is not None:
        local_tz = datetime.now().astimezone().tzinfo

        candidate_local = candidate_dt_utc.astimezone(local_tz)
        start_local = _ensure_utc_aware(schedule_row.start_from).astimezone(local_tz)

        _, end_dt = _build_planned_window(candidate_local, start_local, planned)

        if candidate_local > end_dt:
            return False

    return True


def should_skip_due_to_planned_end(
    schedule_row: ttm.ScheduledTaskSchedule,
    now_utc: datetime,
    candidate_dt_utc: Optional[datetime] = None,
) -> bool:
    """
    Return True when the scheduler is already past the daily planned end time
    for the occurrence being considered.

    This is stricter than `_occurrence_allowed(...)` because it uses the current
    wall clock (`now_utc`) to block late firing of a due task after the end time
    has passed.
    """
    if schedule_row.planned_end_at is None:
        return False

    planned = _parse_planned_end(schedule_row.planned_end_at)
    if planned is None:
        return False

    local_tz = datetime.now().astimezone().tzinfo
    now_local = now_utc.astimezone(local_tz)

    candidate_local = (
        candidate_dt_utc.astimezone(local_tz)
        if candidate_dt_utc is not None
        else now_local
    )

    start_local = _ensure_utc_aware(schedule_row.start_from).astimezone(local_tz)

    _, end_dt = _build_planned_window(now_local, start_local, planned)

    if candidate_local > end_dt:
        return True

    return False


def compute_next_run(
    schedule_row: ttm.ScheduledTaskSchedule,
    after_dt: datetime,
    task_row: Optional[ttm.ScheduledTask] = None,
) -> Optional[datetime]:
    """
    Compute the next run after `after_dt` (UTC-aware) for the given
    `ScheduledTaskSchedule` using its `to_job()` recurrence. Returns a
    UTC-aware datetime or None if no next occurrence exists or it's beyond
    bounds (`until`, `planned_end_at`, `except_dates`).

    This function converts `after_dt` to server-local naive time and drives
    the `schedule.Job` internal next-run computation. It then converts the
    candidate local naive run back to UTC and validates bounds. If the
    candidate is disallowed, the computation advances to the next occurrence
    and repeats until an allowed run is found or a maximum iteration count
    is reached.
    """
    if after_dt.tzinfo is None:
        raise ValueError("after_dt must be timezone-aware")

    # Convert after_dt (UTC-aware) into server-local naive time
    local_tz = datetime.now().astimezone().tzinfo
    local_after = after_dt.astimezone(local_tz).replace(tzinfo=None)

    job = schedule_row.to_job()

    orig_datetime = schedule.datetime.datetime

    class FixedDateTime(orig_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return local_after
            return local_after.replace(tzinfo=tz)

    max_iters = 1000
    try:
        for _ in range(max_iters):
            schedule.datetime.datetime = FixedDateTime
            try:
                job._schedule_next_run()
            except Exception:
                # some schedule versions may not expose internals; rely on job.next_run
                pass
            finally:
                schedule.datetime.datetime = orig_datetime

            next_run_naive = job.next_run
            if next_run_naive is None:
                return None

            next_run_utc = next_run_naive.replace(tzinfo=local_tz).astimezone(
                timezone.utc
            )

            if _occurrence_allowed(schedule_row, next_run_utc, task_row):
                return next_run_utc

            # Advance local_after to just after this candidate and continue
            local_after = next_run_naive + timedelta(seconds=1)

        return None
    finally:
        schedule.datetime.datetime = orig_datetime


def _build_planned_window(now_local, start_local, planned_end_time):
    start_dt = datetime.combine(
        now_local.date(), start_local.time(), tzinfo=now_local.tzinfo
    )

    end_dt = datetime.combine(
        now_local.date(), planned_end_time, tzinfo=now_local.tzinfo
    )

    # overnight handling
    if planned_end_time < start_local.time():
        end_dt += timedelta(days=1)

    return start_dt, end_dt
