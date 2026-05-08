import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional

import tortoise.transactions
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel
from tortoise.expressions import Q

from api_server.authenticator import user_dep
from api_server.dependencies import pagination_query
from api_server.fast_io import FastIORouter
from api_server.logger import logger
from api_server.models import (
    DispatchTaskRequest,
    Pagination,
    ScheduledTask,
    ScheduledTaskSchedule,
    TaskRequest,
    User,
)
from api_server.models import tortoise_models as ttm
from api_server.repositories import TaskRepository
from api_server.utils.time_utils import now_wall_millis, wall_millis_to_datetime

from .tasks import post_dispatch_task

router = FastIORouter(tags=["Tasks"])


def normalize_to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("Datetime must include timezone information")
    return dt.astimezone(timezone.utc)


def _ensure_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class PostScheduledTaskRequest(BaseModel):
    task_request: TaskRequest
    schedules: list[ScheduledTaskSchedule]


def _load_task_request(payload: object) -> TaskRequest:
    if isinstance(payload, TaskRequest):
        return payload
    if isinstance(payload, str):
        return TaskRequest.model_validate_json(payload)
    return TaskRequest.model_validate(payload)


async def _dispatch_scheduled_schedule(schedule_id: int):
    # Load fresh ORM objects inside the background task to avoid using
    # ORM instances created in another coroutine/thread
    schedule = await ttm.ScheduledTaskSchedule.get_or_none(
        _id=schedule_id
    ).prefetch_related("scheduled_task")
    if schedule is None:
        logger.warning(
            "scheduled task schedule_id=%s disappeared before dispatch", schedule_id
        )
        return
    task = schedule.scheduled_task
    user = await User.load_from_db(task.created_by)
    if user is None:
        logger.warning(
            "scheduled task schedule_id=%s skipped because creator user [%s] does not exist",
            schedule.get_id(),
            task.created_by,
        )
        return

    try:
        logger.info(
            "POST DISPATCH ENTRY schedule_id=%s task_id=%s", schedule.get_id(), task.id
        )
        task_request = _load_task_request(task.task_request)
        dispatch_request = DispatchTaskRequest(
            type="dispatch_task_request",
            request=task_request,
        )
        logger.info(
            "scheduled task schedule_id=%s task_id=%s dispatched to RMF",
            schedule.get_id(),
            task.id,
        )
        await post_dispatch_task(dispatch_request, TaskRepository(user))
        task.last_ran = wall_millis_to_datetime(now_wall_millis())
        await task.save(update_fields=["last_ran"])
        logger.info(
            "scheduled task schedule_id=%s task_id=%s finished dispatch",
            schedule.get_id(),
            task.id,
        )
    except Exception:
        logger.exception(
            "error dispatching scheduled task schedule_id=%s task_id=%s",
            schedule.get_id(),
            task.id,
        )


async def scheduler_loop(poll_interval: float = 1.0):
    logger.info("UTC scheduler loop started poll_interval=%s", poll_interval)
    while True:
        try:
            now = datetime.now(timezone.utc)
            logger.info("SCHEDULER TICK now=%s tzinfo=%s", now.isoformat(), now.tzinfo)
            logger.info("NOW=%s type=%s", now, type(now))

            # Query candidate schedules, then do the UTC comparison in Python so the
            # scheduler remains consistent whether the DB round-trips naive or aware
            # datetimes.
            due_schedules = await (
                ttm.ScheduledTaskSchedule.filter(
                    dispatched=False,
                    start_from__isnull=False,
                )
                .select_related("scheduled_task")
                .order_by("start_from", "_id")
            )
            logger.info("DUE COUNT=%s", len(due_schedules))

            for schedule_row in due_schedules:
                if schedule_row.start_from is None:
                    continue

                schedule_start = _ensure_utc_aware(schedule_row.start_from)

                logger.info(
                    "DB start_from=%s type=%s tz=%s",
                    schedule_row.start_from,
                    type(schedule_row.start_from),
                    getattr(schedule_row.start_from, "tzinfo", None),
                )

                # Defensive tzinfo check: treat naive DB datetimes as UTC but log.
                if schedule_row.start_from.tzinfo is None:
                    logger.warning(
                        "schedule_id=%s has naive start_from, treating as UTC: %s",
                        schedule_row.get_id(),
                        schedule_row.start_from,
                    )

                if schedule_start > now:
                    continue

                logger.info(
                    "FOUND DUE TASK schedule_id=%s task_id=%s start_from=%s start_from_tz=%s now=%s now_tz=%s",
                    schedule_row.get_id(),
                    getattr(schedule_row.scheduled_task, "id", None),
                    schedule_start.isoformat(),
                    schedule_start.tzinfo,
                    now.isoformat(),
                    now.tzinfo,
                )

                # Atomically claim the schedule by primary key using Python attr name `_id`
                schedule_id = schedule_row.get_id()
                claimed = await ttm.ScheduledTaskSchedule.filter(
                    _id=schedule_id, dispatched=False
                ).update(dispatched=True)
                if not claimed:
                    logger.info(
                        "SKIPPED CLAIMED schedule_id=%s task_id=%s",
                        schedule_id,
                        getattr(schedule_row.scheduled_task, "id", None),
                    )
                    continue

                logger.info(
                    "scheduler triggered schedule_id=%s task_id=%s start_from=%s now=%s",
                    schedule_id,
                    getattr(schedule_row.scheduled_task, "id", None),
                    schedule_row.start_from.isoformat()
                    if schedule_row.start_from is not None
                    else None,
                    now.isoformat(),
                )

                # Create background task with primitive id only and attach error logger
                task = asyncio.create_task(_dispatch_scheduled_schedule(schedule_id))

                def _bg_done_callback(t: asyncio.Task):
                    try:
                        exc = t.exception()
                    except asyncio.CancelledError:
                        return
                    if exc:
                        tb = "".join(
                            traceback.format_exception(
                                type(exc), exc, exc.__traceback__
                            )
                        )
                        logger.error(
                            "background dispatch failed schedule_id=%s exception=%s\n%s",
                            schedule_id,
                            exc,
                            tb,
                        )

                task.add_done_callback(_bg_done_callback)
        except Exception:
            logger.exception("scheduler loop error")

        await asyncio.sleep(poll_interval)


@router.post("", status_code=201, response_model=ScheduledTask)
async def post_scheduled_task(
    scheduled_task_request: PostScheduledTaskRequest,
    user: User = Depends(user_dep),
):
    """
    Create a scheduled task. Below are some examples of how the schedules are represented.
    For more examples, check the docs of the underlying library used [here](https://github.com/dbader/schedule/blob/6eb0b5346b1ce35ece5050e65789fa6e44368175/docs/examples.rst).

    | every | to | period | at | description |
    | - | - | - | - | - |
    | 10 | - | minutes | - | Every 10 minutes |
    | - | - | hour | - | Every hour |
    | - | - | day | 10:30 | Every day at 10:30am |
    | - | - | monday | - | Every monday |
    | - | - | wednesday | 13:15 | Every wednesday at 01:15pm |
    | - | - | minute | :17 | Every 17th sec of a mintue |
    | 5 | 10 | seconds | - | Every 5-10 seconds (randomly) |
    """
    if len(scheduled_task_request.schedules) == 0:
        raise HTTPException(422, "Task is never going to run")

    async with tortoise.transactions.in_transaction():
        scheduled_task = await ttm.ScheduledTask.create(
            task_request=scheduled_task_request.task_request.model_dump_json(
                exclude_none=True
            ),
            created_by=user.username,
        )
        schedules = []
        for schedule_request in scheduled_task_request.schedules:
            if schedule_request.start_from is None:
                raise HTTPException(422, "start_from is required for scheduled tasks")
            schedules.append(
                ttm.ScheduledTaskSchedule(
                    scheduled_task=scheduled_task,
                    start_from=normalize_to_utc(schedule_request.start_from),
                    until=normalize_to_utc(schedule_request.until),
                    at=schedule_request.at,
                    every=schedule_request.every,
                    period=schedule_request.period,
                    dispatched=False,
                )
            )

        await ttm.ScheduledTaskSchedule.bulk_create(schedules)
        return ScheduledTask.model_validate(scheduled_task)


@router.get("", response_model=list[ScheduledTask])
async def get_scheduled_tasks(
    start_before: datetime = Query(
        description="Only return scheduled tasks that start before given timestamp"
    ),
    until_after: datetime = Query(
        description="Only return scheduled tasks that stop after given timestamp"
    ),
    pagination: Pagination = Depends(pagination_query),
):
    q = (
        ttm.ScheduledTask.filter(
            Q(schedules__start_from__lte=start_before)
            | Q(schedules__start_from__isnull=True),
            Q(schedules__until__gte=until_after) | Q(schedules__until__isnull=True),
        )
        .prefetch_related("schedules")
        .distinct()
        .limit(pagination.limit)
        .offset(pagination.offset)
    )
    if pagination.order_by:
        q.order_by(*pagination.order_by)
    results = await q
    await ttm.ScheduledTask.fetch_for_list(results)
    return [ScheduledTask.model_validate(x) for x in results]


@router.get("/{task_id}", response_model=ScheduledTask)
async def get_scheduled_task(task_id: int) -> ttm.ScheduledTask:
    task = await ttm.ScheduledTask.get_or_none(id=task_id).prefetch_related("schedules")
    if task is None:
        raise HTTPException(404)
    return task


@router.put("/{task_id}/clear")
async def del_scheduled_tasks_event(
    task_id: int,
    event_date: datetime,
):
    task = await get_scheduled_task(task_id)
    if task is None:
        raise HTTPException(404)

    event_date_str = normalize_to_utc(event_date).isoformat()
    if not isinstance(task.except_dates, list):
        logger.error(f"task.except_dates is not a list: {type(task.except_dates)}")
        raise HTTPException(500)
    task.except_dates.append(event_date_str[:10])
    await task.save()


@router.post("/{task_id}/update", status_code=201, response_model=ScheduledTask)
async def update_schedule_task(
    task_id: int,
    scheduled_task_request: PostScheduledTaskRequest,
    except_date: Optional[datetime] = None,
):
    task = await get_scheduled_task(task_id)
    if task is None:
        raise HTTPException(404)

    if len(scheduled_task_request.schedules) == 0:
        raise HTTPException(422, "Task is never going to run")

    async with tortoise.transactions.in_transaction():
        if except_date:
            event_date_str = normalize_to_utc(except_date).isoformat()
            if not isinstance(task.except_dates, list):
                logger.error(
                    f"task.except_dates is not a list: {type(task.except_dates)}"
                )
                raise HTTPException(500)
            task.except_dates.append(event_date_str[:10])
            await task.save()

            scheduled_task = await ttm.ScheduledTask.create(
                task_request=scheduled_task_request.task_request.model_dump_json(
                    exclude_none=True
                ),
                created_by=task.created_by,
            )
            schedules = []
            for schedule_request in scheduled_task_request.schedules:
                if schedule_request.start_from is None:
                    raise HTTPException(
                        422, "start_from is required for scheduled tasks"
                    )
                schedules.append(
                    ttm.ScheduledTaskSchedule(
                        scheduled_task=scheduled_task,
                        start_from=normalize_to_utc(schedule_request.start_from),
                        until=normalize_to_utc(schedule_request.until),
                        at=schedule_request.at,
                        every=schedule_request.every,
                        period=schedule_request.period,
                        dispatched=False,
                    )
                )
            await ttm.ScheduledTaskSchedule.bulk_create(schedules)
        else:
            task.update_from_dict(
                {
                    "task_request": scheduled_task_request.task_request.model_dump_json(
                        exclude_none=True
                    ),
                    "except_dates": [],
                }
            )

            for sche in task.schedules:
                await sche.delete()

            await task.save()
            schedules = []
            for schedule_request in scheduled_task_request.schedules:
                if schedule_request.start_from is None:
                    raise HTTPException(
                        422, "start_from is required for scheduled tasks"
                    )
                schedules.append(
                    ttm.ScheduledTaskSchedule(
                        scheduled_task=task,
                        start_from=normalize_to_utc(schedule_request.start_from),
                        until=normalize_to_utc(schedule_request.until),
                        at=schedule_request.at,
                        every=schedule_request.every,
                        period=schedule_request.period,
                        dispatched=False,
                    )
                )

            await ttm.ScheduledTaskSchedule.bulk_create(schedules)

    return ScheduledTask.model_validate(task)


@router.delete("/{task_id}")
async def del_scheduled_tasks(task_id: int):
    async with tortoise.transactions.in_transaction():
        task = await get_scheduled_task(task_id)
        for sche in task.schedules:
            await sche.delete()
        await task.delete()
