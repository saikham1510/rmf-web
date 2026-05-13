from datetime import datetime, timedelta, timezone

from api_server.models import tortoise_models as ttm
from api_server.utils.schedule_utils import compute_next_run


def test_compute_next_run_daily_at_one_minute_after_after_dt():
    # Choose a fixed after_dt (UTC-aware)
    after_dt = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)

    # Server local tz
    local_tz = datetime.now().astimezone().tzinfo
    local_after = after_dt.astimezone(local_tz)

    # target is one minute after local_after
    target_local = (local_after + timedelta(minutes=1)).time()
    at_str = f"{target_local.hour:02d}:{target_local.minute:02d}"

    sche = ttm.ScheduledTaskSchedule(
        every=None, period=ttm.ScheduledTaskSchedule.Period.Day, at=at_str
    )

    next_run = compute_next_run(sche, after_dt)
    assert next_run is not None

    # expected is local datetime at target_local on same date, converted to UTC
    expected_local_dt = datetime(
        local_after.year,
        local_after.month,
        local_after.day,
        target_local.hour,
        target_local.minute,
    ).replace(tzinfo=local_tz)
    expected_utc = expected_local_dt.astimezone(timezone.utc)

    assert next_run == expected_utc
