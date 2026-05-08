from datetime import datetime, time, timezone
from enum import Enum

import schedule
from schedule import Job
from tortoise.fields import (
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
    period = CharEnumField(Period)
    at = CharField(255, null=True)

    def get_id(self) -> int:
        return self._id

    def to_job(self) -> Job:
        if self.every is not None:
            job = schedule.every(self.every)
        else:
            job = schedule.every()

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
            # Use `start_from` as the single source of truth when present.
            # Normalize `start_from` to UTC and derive HH:MM from that UTC
            # instant to pass into `job.at()`. Fall back to `self.at` if
            # `start_from` isn't set.
            if self.start_from is not None:
                sf = self.start_from
                if sf.tzinfo is None:
                    sf = sf.replace(tzinfo=timezone.utc)
                sf_utc = sf.astimezone(timezone.utc)
                at_str = f"{sf_utc.hour:02d}:{sf_utc.minute:02d}"
                job = job.at(at_str)
            else:
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
