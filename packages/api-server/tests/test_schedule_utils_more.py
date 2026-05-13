from datetime import datetime, timedelta, timezone

from api_server.models import tortoise_models as ttm
from api_server.utils.schedule_utils import (
    _occurrence_allowed,
    compute_next_run,
    should_skip_due_to_planned_end,
)


class DummyTask:
    def __init__(self, except_dates=None):
        self.except_dates = except_dates or []


def test_until_blocks_occurrence():
    candidate_utc = datetime.now(timezone.utc) + timedelta(minutes=30)
    sche = ttm.ScheduledTaskSchedule(
        every=None,
        period=ttm.ScheduledTaskSchedule.Period.Day,
        at=None,
    )
    sche.until = candidate_utc - timedelta(minutes=1)

    assert not _occurrence_allowed(sche, candidate_utc)


def test_planned_end_at_blocks_occurrence():
    after_dt = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    local_tz = datetime.now().astimezone().tzinfo
    local_after = after_dt.astimezone(local_tz)
    # schedule at 16:00 local, but planned_end_at is 15:30
    at_str = "16:00"
    sche = ttm.ScheduledTaskSchedule(
        every=None, period=ttm.ScheduledTaskSchedule.Period.Day, at=at_str
    )
    sche.planned_end_at = "15:30"

    next_run = compute_next_run(sche, after_dt)
    assert next_run is None


def test_except_dates_skips_date():
    after_dt = datetime(2026, 5, 12, 8, 0, 0, tzinfo=timezone.utc)
    local_tz = datetime.now().astimezone().tzinfo
    local_after = after_dt.astimezone(local_tz)
    target_local = local_after + timedelta(hours=1)
    at_str = f"{target_local.hour:02d}:{target_local.minute:02d}"

    sche = ttm.ScheduledTaskSchedule(
        every=None, period=ttm.ScheduledTaskSchedule.Period.Day, at=at_str
    )

    # candidate is today at target_local; mark this date as excepted
    candidate_local_dt = datetime(
        local_after.year,
        local_after.month,
        local_after.day,
        target_local.hour,
        target_local.minute,
    ).replace(tzinfo=local_tz)
    candidate_utc = candidate_local_dt.astimezone(timezone.utc)

    task = DummyTask(except_dates=[candidate_utc.date().isoformat()])

    next_run = compute_next_run(sche, after_dt, task)
    assert next_run is not None
    # next run should be at least one day after candidate
    assert next_run.date() >= (candidate_utc.date() + timedelta(days=1))


def test_every_minutes_next_within_interval():
    after_dt = datetime(2026, 5, 12, 12, 2, 0, tzinfo=timezone.utc)
    sche = ttm.ScheduledTaskSchedule(
        every=5, period=ttm.ScheduledTaskSchedule.Period.Minute
    )

    next_run = compute_next_run(sche, after_dt)
    assert next_run is not None
    assert next_run > after_dt
    assert next_run - after_dt <= timedelta(minutes=5)


def test_should_skip_due_to_planned_end_when_now_is_late():
    now_utc = datetime(2026, 5, 12, 17, 1, 0, tzinfo=timezone.utc)
    local_tz = datetime.now().astimezone().tzinfo
    local_now = now_utc.astimezone(local_tz)

    sche = ttm.ScheduledTaskSchedule(
        every=None,
        period=ttm.ScheduledTaskSchedule.Period.Day,
        at=f"{local_now.hour:02d}:{local_now.minute:02d}",
    )
    # planned end is one minute before now in the same local timezone
    planned_end = (local_now - timedelta(minutes=1)).strftime("%H:%M")
    sche.planned_end_at = planned_end

    assert should_skip_due_to_planned_end(sche, now_utc, candidate_dt_utc=now_utc)


def test_should_not_skip_due_to_planned_end_before_cutoff():
    now_utc = datetime(2026, 5, 12, 17, 1, 0, tzinfo=timezone.utc)
    local_tz = datetime.now().astimezone().tzinfo
    local_now = now_utc.astimezone(local_tz)

    sche = ttm.ScheduledTaskSchedule(
        every=None,
        period=ttm.ScheduledTaskSchedule.Period.Day,
        at=f"{local_now.hour:02d}:{local_now.minute:02d}",
    )
    # planned end is later than now
    planned_end = (local_now + timedelta(minutes=10)).strftime("%H:%M")
    sche.planned_end_at = planned_end

    assert not should_skip_due_to_planned_end(sche, now_utc, candidate_dt_utc=now_utc)
