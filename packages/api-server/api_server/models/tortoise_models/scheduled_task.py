from datetime import timezone
from enum import Enum
from typing import TYPE_CHECKING

import schedule
from schedule import Job
from tortoise.fields import (
    BooleanField,
    CharEnumField,
    CharField,
    DatetimeField,
    ForeignKeyField,
    ForeignKeyRelation,
    IntField,
    JSONField,
    ReverseRelation,
    SmallIntField,
)
from tortoise.models import Model


class ScheduledTask(Model):
    task_request = JSONField()
    created_by = CharField(255)
    schedules: ReverseRelation["ScheduledTaskSchedule"]
    last_ran = DatetimeField(null=True)
    except_dates = JSONField(null=True)


class ScheduledTaskSchedule(Model):
    """
    The schedules for a scheduled task request.
    A scheduled task may have multiple schedules.
    """

    class Period(str, Enum):
        Monday = "monday"
        Tuesday = "tuesday"
        Wednesday = "wednesday"
        Thursday = "thursday"
        Friday = "friday"
        Saturday = "saturday"
        Sunday = "sunday"
        Day = "day"
        Hour = "hour"
        Minute = "minute"

    _id = IntField(pk=True, source_field="id")
    scheduled_task: ForeignKeyRelation[ScheduledTask] = ForeignKeyField(
        "models.ScheduledTask", related_name="schedules"
    )
    every = SmallIntField(null=True)
    start_from = DatetimeField(null=True)
    until = DatetimeField(null=True)
    planned_end_at = CharField(255, null=True)
    period = CharEnumField(Period)
    at = CharField(255, null=True)
    dispatched = BooleanField(
        default=False
    )  # Legacy field, kept for backwards compatibility
    # NEW FIELD: Tracks when this schedule should next run
    # Updated after each dispatch to calculate next occurrence
    next_run_at = DatetimeField(null=True)

    def get_id(self) -> int:
        return self._id

    def to_job(self) -> schedule.Job:
        if self.every is not None:
            job = schedule.every(self.every)
        else:
            job = schedule.every(1)

        if self.period in (
            ScheduledTaskSchedule.Period.Monday,
            ScheduledTaskSchedule.Period.Tuesday,
            ScheduledTaskSchedule.Period.Wednesday,
            ScheduledTaskSchedule.Period.Thursday,
            ScheduledTaskSchedule.Period.Friday,
            ScheduledTaskSchedule.Period.Saturday,
            ScheduledTaskSchedule.Period.Sunday,
        ):
            job = getattr(job, self.period)
        elif self.period == ScheduledTaskSchedule.Period.Day:
            job = job.days
        elif self.period == ScheduledTaskSchedule.Period.Hour:
            job = job.hours
        elif self.period == ScheduledTaskSchedule.Period.Minute:
            job = job.minutes
        else:
            raise ValueError("invalid period")

        if self.at is not None:
            # Use the `at` field directly as the time to run the job.
            # The `at` field contains local time (HH:MM format from user's browser).
            job = job.at(self.at)

        # schedule library requires naive datetime for `.until()`; normalize
        # `until` to UTC and pass a naive UTC-localized datetime.
        if self.until is not None:
            u = self.until
            if u.tzinfo is None:
                u = u.replace(tzinfo=timezone.utc)
            u_utc = u.astimezone(timezone.utc)
            job = job.until(u_utc.replace(tzinfo=None))

        # Hashable value in order to tag the job with a unique identifier
        job.tag(self._id)

        return job
