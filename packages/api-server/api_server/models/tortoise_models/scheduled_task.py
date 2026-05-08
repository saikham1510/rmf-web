from enum import Enum

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
    period = CharEnumField(Period)
    at = CharField(255, null=True)
    dispatched = BooleanField(default=False)

    def get_id(self) -> int:
        return self._id
