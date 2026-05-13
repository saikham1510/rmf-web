import asyncio
import json
import threading
import time as pytime
import traceback
from datetime import datetime, timezone
from typing import Optional

import schedule
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
from api_server.utils.schedule_utils import (
    _occurrence_allowed,
    compute_next_run,
    should_skip_due_to_planned_end,
)
from api_server.utils.time_utils import now_wall_millis, wall_millis_to_datetime

from .tasks import post_dispatch_task

router = FastIORouter(tags=["Tasks"])
INTERNAL_USER = User(username="__rmf_internal__", is_admin=True)


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


def validate_schedules(schedules: list[ScheduledTaskSchedule]) -> None:
    """Validate that at least one schedule produces a valid job. Raises HTTPException if none do."""
    valid_count = 0
    for sche in schedules:
        try:
            db_sche = ttm.ScheduledTaskSchedule(
                every=sche.every,
                start_from=normalize_to_utc(sche.start_from),
                until=normalize_to_utc(sche.until),
                planned_end_at=sche.planned_end_at,
                period=sche.period,
                at=sche.at,
            )
            # Try to construct a job; if successful, this schedule is valid.
            # `to_job()` is defined on the Tortoise model, not the Pydantic request model.
            db_sche.to_job()
            valid_count += 1
        except Exception as e:
            logger.debug(f"schedule validation failed: {e}")
            pass
    if valid_count == 0:
        raise HTTPException(422, "Task is never going to run")


class PostScheduledTaskRequest(BaseModel):
    task_request: TaskRequest
    schedules: list[ScheduledTaskSchedule]


async def _create_schedule_with_next_run(
    parent_task: ttm.ScheduledTask,
    schedule_request: ScheduledTaskSchedule,
) -> ttm.ScheduledTaskSchedule:
    if schedule_request.start_from is None:
        raise HTTPException(422, "start_from is required for scheduled tasks")

    sche = await ttm.ScheduledTaskSchedule.create(
        scheduled_task=parent_task,
        start_from=normalize_to_utc(schedule_request.start_from),
        until=normalize_to_utc(schedule_request.until),
        planned_end_at=schedule_request.planned_end_at,
        at=schedule_request.at,
        every=schedule_request.every,
        period=schedule_request.period,
        dispatched=False,
    )

    after_dt = normalize_to_utc(schedule_request.start_from) or datetime.now(
        timezone.utc
    )
    next_run = compute_next_run(sche, after_dt)
    if next_run is None:
        raise HTTPException(422, "Task is never going to run")

    sche.start_from = next_run
    await sche.save(update_fields=["start_from"])
    return sche


async def schedule_task(task: ttm.ScheduledTask, task_repo: TaskRepository):
    """Validate that the scheduled task has at least one valid schedule."""
    await task.fetch_related("schedules")
    jobs: list[tuple[ttm.ScheduledTaskSchedule, schedule.Job]] = []
    for sche in task.schedules:
        try:
            jobs.append((sche, sche.to_job()))
        except Exception as e:
            logger.debug(f"could not create schedule job: {e}")
            pass
    # Note: validation of at least one valid schedule happens in post_scheduled_task() before DB insert,
    # so we should never reach len(jobs) == 0 here. But keep as safety check.
    if len(jobs) == 0:
        logger.warning(
            f"scheduled task [{task.pk}] has no valid schedules (should not happen)"
        )
        return


async def _dispatch_scheduled_schedule(schedule_id: int):
    schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
        _id=schedule_id
    ).select_related("scheduled_task")
    if schedule_row is None:
        logger.warning("scheduled task schedule_id=%s no longer exists", schedule_id)
        return

    task = schedule_row.scheduled_task
    if task is None:
        logger.warning(
            "scheduled task schedule_id=%s is missing its parent task", schedule_id
        )
        return

    if not isinstance(task.task_request, dict):
        try:
            task_request_data = json.loads(task.task_request)
        except Exception as e:
            logger.error(
                "task_request is not a dict or JSON string: %s", type(task.task_request)
            )
            raise HTTPException(500) from e
    else:
        task_request_data = task.task_request

    # Scheduled patrols are dispatched one loop at a time so recurrence bounds
    # (planned end / until) can control when to stop.
    if (
        isinstance(task_request_data, dict)
        and task_request_data.get("category") == "patrol"
        and isinstance(task_request_data.get("description"), dict)
    ):
        task_request_data = {
            **task_request_data,
            "description": {
                **task_request_data["description"],
                "rounds": 1,
            },
        }

    # Attach schedule context labels so downstream consumers (e.g. completion
    # chaining) can correlate a dispatched task back to its schedule. Labels are
    # optional in the schema, so create/extend as needed.
    try:
        if isinstance(task_request_data, dict):
            labels = list(task_request_data.get("labels") or [])
            # Mark which scheduled_task and schedule row produced this dispatch
            if schedule_row is not None and getattr(
                schedule_row, "scheduled_task", None
            ):
                labels.append(f"scheduled_task_id:{schedule_row.scheduled_task.id}")
            labels.append(f"scheduled_schedule_id:{schedule_id}")
            # De-duplicate while preserving order
            seen = set()
            deduped = []
            for x in labels:
                if x in seen:
                    continue
                seen.add(x)
                deduped.append(x)
            task_request_data["labels"] = deduped
    except Exception:
        # Non-fatal: labels are just hints
        logger.exception("failed to attach schedule labels to task_request")

    task_request = TaskRequest(**task_request_data)
    # The scheduled trigger time is authoritative; dispatch immediately when the
    # schedule fires.
    task_request.unix_millis_earliest_start_time = 0
    task_request.unix_millis_request_time = now_wall_millis()

    dispatch_request = DispatchTaskRequest(
        type="dispatch_task_request",
        request=task_request,
    )
    logger.info(
        "dispatching scheduled task schedule_id=%s task_id=%s",
        schedule_id,
        task.id,
    )
    await post_dispatch_task(dispatch_request, TaskRepository(INTERNAL_USER))
    task.last_ran = wall_millis_to_datetime(now_wall_millis())
    await task.save(update_fields=["last_ran"])
    logger.info(
        "finished dispatching scheduled task schedule_id=%s task_id=%s",
        schedule_id,
        task.id,
    )
    # After dispatch, compute the next occurrence and persist it (DB-driven recurrence)
    try:
        now_utc = datetime.now(timezone.utc)
        next_run = compute_next_run(schedule_row, now_utc)
        if next_run is None:
            # No further occurrences; leave dispatched=True to indicate finished
            logger.info(
                "schedule_id=%s has no further occurrences; marking finished",
                schedule_id,
            )
            return
        # Otherwise, update start_from and clear dispatched so scheduler can pick it up later
        schedule_row.start_from = next_run
        schedule_row.dispatched = False
        await schedule_row.save(update_fields=["start_from", "dispatched"])
        logger.info(
            "schedule_id=%s next_run updated to %s",
            schedule_id,
            next_run.isoformat(),
        )
    except Exception:
        logger.exception(
            "failed computing or persisting next run for schedule_id=%s", schedule_id
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

                if should_skip_due_to_planned_end(
                    schedule_row, now, candidate_dt_utc=schedule_start
                ):
                    logger.info(
                        "schedule_id=%s skipped because now=%s is past planned_end_at=%s",
                        schedule_row.get_id(),
                        now.isoformat(),
                        schedule_row.planned_end_at,
                    )
                    await ttm.ScheduledTaskSchedule.filter(
                        _id=schedule_row.get_id()
                    ).update(dispatched=True)
                    continue

                # Check bounds (until, planned_end_at, except_dates) before claiming
                if not _occurrence_allowed(
                    schedule_row, schedule_start, schedule_row.scheduled_task
                ):
                    logger.info(
                        "schedule_id=%s occurrence %s not allowed by bounds; marking finished",
                        schedule_row.get_id(),
                        schedule_start.isoformat(),
                    )
                    # Mark finished to avoid further attempts
                    await ttm.ScheduledTaskSchedule.filter(
                        _id=schedule_row.get_id()
                    ).update(dispatched=True)
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
    # Validate schedules BEFORE entering transaction to avoid rollback on validation failure
    validate_schedules(scheduled_task_request.schedules)

    try:
        task_repo = TaskRepository(user)
        async with tortoise.transactions.in_transaction():
            scheduled_task = await ttm.ScheduledTask.create(
                task_request=scheduled_task_request.task_request.model_dump(
                    exclude_none=True
                ),
                created_by=user.username,
            )
            # Create schedules and compute authoritative start_from per-schedule
            for schedule_request in scheduled_task_request.schedules:
                await _create_schedule_with_next_run(scheduled_task, schedule_request)
            await schedule_task(scheduled_task, task_repo)
        scheduled_task = await ttm.ScheduledTask.get_or_none(
            id=scheduled_task.id
        ).prefetch_related("schedules")
        if scheduled_task is None:
            raise HTTPException(500)
        return ScheduledTask.model_validate(scheduled_task)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("error creating scheduled task")
        raise HTTPException(500) from e


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
    if task.except_dates is None:
        task.except_dates = []
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
    try:
        task = await get_scheduled_task(task_id)
        if task is None:
            raise HTTPException(404)
        # If "except_date" is provided, it means a single event is being updated.
        # In this case, we perform the following steps:
        #   1. Add the "except_date" to the list of exception dates for the task.
        #   2. Clear all existing schedules associated with the task.
        #   3. Create a new scheduled task with the requested data from the schedule form.

        # Validate schedules BEFORE entering transaction to avoid rollback on validation failure
        validate_schedules(scheduled_task_request.schedules)

        if len(scheduled_task_request.schedules) == 0:
            raise HTTPException(422, "Task is never going to run")

        async with tortoise.transactions.in_transaction():
            if except_date:
                event_date_str = normalize_to_utc(except_date).isoformat()
                if task.except_dates is None:
                    task.except_dates = []
                if not isinstance(task.except_dates, list):
                    logger.error(
                        f"task.except_dates is not a list: {type(task.except_dates)}"
                    )
                    raise HTTPException(500)
                task.except_dates.append(event_date_str[:10])
                await task.save()

                for sche in task.schedules:
                    schedule.clear(sche.get_id())

                scheduled_task = await ttm.ScheduledTask.create(
                    task_request=scheduled_task_request.task_request.model_dump_json(
                        exclude_none=True
                    ),
                    created_by=task.created_by,
                )
                for schedule_request in scheduled_task_request.schedules:
                    await _create_schedule_with_next_run(
                        scheduled_task, schedule_request
                    )
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
                for schedule_request in scheduled_task_request.schedules:
                    await _create_schedule_with_next_run(task, schedule_request)

        refreshed_task = await ttm.ScheduledTask.get_or_none(
            id=task.id
        ).prefetch_related("schedules")
        if refreshed_task is None:
            raise HTTPException(500)
        return ScheduledTask.model_validate(refreshed_task)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("error updating scheduled task")
        raise HTTPException(500) from e


@router.delete("/{task_id}")
async def del_scheduled_tasks(task_id: int):
    async with tortoise.transactions.in_transaction():
        task = await get_scheduled_task(task_id)
        for sche in task.schedules:
            await sche.delete()
        await task.delete()
