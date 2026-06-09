import asyncio
import json
import re
import threading
import time as pytime
import traceback
from collections.abc import Iterable
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
    CancelTaskRequest,
    DispatchTaskRequest,
    LoopSummary,
    Pagination,
    ScheduledTask,
    ScheduledTaskSchedule,
    ScheduleRun,
    TaskRequest,
    TaskStatus,
    User,
)
from api_server.models import tortoise_models as ttm
from api_server.models.labels import Labels
from api_server.repositories import TaskRepository
from api_server.rmf_io import tasks_service
from api_server.utils.schedule_utils import (
    _occurrence_allowed,
    compute_next_run,
    should_skip_due_to_planned_end,
)
from api_server.utils.time_utils import (
    datetime_to_wall_millis,
    now_wall_millis,
    wall_millis_to_datetime,
)

from .tasks import post_dispatch_task

router = FastIORouter(tags=["Tasks"])
INTERNAL_USER = User(username="__rmf_internal__", is_admin=True)

ACTIVE_SCHEDULED_TASK_STATUSES = {
    TaskStatus.queued,
    TaskStatus.standby,
    TaskStatus.underway,
    TaskStatus.delayed,
    TaskStatus.blocked,
}

NON_CHAINABLE_SCHEDULED_TASK_CATEGORIES = {"clean"}


def _normalized_category(category) -> str:
    return str(category or "").strip().lower()


def _dedupe_labels(labels: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        deduped.append(label)
    return deduped


def _task_state_is_active(task_state) -> bool:
    return (
        task_state.status in ACTIVE_SCHEDULED_TASK_STATUSES
        and task_state.unix_millis_finish_time is None
    )


async def _schedule_has_active_tasks(
    schedule_id: int,
    task_repo: TaskRepository,
) -> bool:
    label = Labels.from_strings([f"scheduled_schedule_id={schedule_id}"])
    task_states = await task_repo.query_task_states(label=label)
    return any(_task_state_is_active(task_state) for task_state in task_states)


async def _has_chained_child_for_parent(
    schedule_id: int,
    parent_task_id: str,
    task_repo: TaskRepository,
) -> bool:
    # We require both labels to avoid collisions across different schedules.
    label = Labels.from_strings(
        [
            f"scheduled_schedule_id={schedule_id}",
            f"chain_parent_task_id={parent_task_id}",
        ]
    )
    task_states = await task_repo.query_task_states(label=label)
    return len(task_states) > 0


def _parse_task_request_dict(raw_task_request) -> dict:
    if isinstance(raw_task_request, dict):
        return dict(raw_task_request)
    if isinstance(raw_task_request, str):
        try:
            decoded = json.loads(raw_task_request)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            pass
    logger.error(
        "task_request is not a dict or JSON string: %s", type(raw_task_request)
    )
    raise HTTPException(500)


def _build_request_for_schedule_dispatch(
    schedule_row: ttm.ScheduledTaskSchedule,
    *,
    chain_parent_task_id: str | None = None,
) -> TaskRequest:
    parent_task = schedule_row.scheduled_task
    if parent_task is None:
        raise HTTPException(500)

    task_request_data = _parse_task_request_dict(parent_task.task_request)

    # Preserve the original task description (including `rounds`) so scheduled
    # patrols run for the configured number of loops.

    labels = list(task_request_data.get("labels") or [])
    labels.append(f"scheduled_task_id={parent_task.id}")
    labels.append(f"scheduled_schedule_id={schedule_row.get_id()}")
    if chain_parent_task_id is not None:
        labels.append(f"chain_parent_task_id={chain_parent_task_id}")
    task_request_data["labels"] = _dedupe_labels(labels)

    task_request = TaskRequest(**task_request_data)
    # Set a meaningful earliest-start time to avoid RMF interpreting 0 as epoch.
    # For regular scheduled dispatches use the schedule's authoritative start time;
    # for chained (immediate) runs use current time so RMF shows a sensible timestamp.
    if chain_parent_task_id is None:
        scheduled_start = schedule_row.next_run_at or schedule_row.start_from
        if scheduled_start is not None:
            task_request.unix_millis_earliest_start_time = datetime_to_wall_millis(
                scheduled_start
            )
        else:
            task_request.unix_millis_earliest_start_time = now_wall_millis()
    else:
        task_request.unix_millis_earliest_start_time = now_wall_millis()

    task_request.unix_millis_request_time = now_wall_millis()
    return task_request


async def update_completed_clean_schedule_end(
    schedule_row: ttm.ScheduledTaskSchedule,
    task_state,
) -> bool:
    """Store a scheduled clean task's actual finish time as its calendar end."""
    parent_task = schedule_row.scheduled_task
    if parent_task is None or task_state.unix_millis_finish_time is None:
        return False

    task_request_data = _parse_task_request_dict(parent_task.task_request)
    if _normalized_category(task_request_data.get("category")) != "clean":
        return False

    local_tz = datetime.now().astimezone().tzinfo
    finish_dt = wall_millis_to_datetime(task_state.unix_millis_finish_time).astimezone(
        local_tz
    )
    schedule_row.planned_end_at = finish_dt.strftime("%H:%M")
    await schedule_row.save(update_fields=["planned_end_at"])
    logger.info(
        "updated completed scheduled clean end schedule_id=%s task_id=%s planned_end_at=%s",
        schedule_row.get_id(),
        task_state.booking.id,
        schedule_row.planned_end_at,
    )
    return True


async def _dispatch_schedule_task_request(
    schedule_row: ttm.ScheduledTaskSchedule,
    task_repo: TaskRepository,
    *,
    chain_parent_task_id: str | None = None,
) -> None:
    parent_task = schedule_row.scheduled_task
    if parent_task is None:
        logger.warning(
            "scheduled task schedule_id=%s is missing its parent task",
            schedule_row.get_id(),
        )
        return

    task_request = _build_request_for_schedule_dispatch(
        schedule_row,
        chain_parent_task_id=chain_parent_task_id,
    )
    # Debug log: show the outgoing task request payload to help diagnose
    # cases where `description.rounds` may be missing or overwritten.
    try:
        logger.debug(
            "scheduled dispatch payload schedule_id=%s task_request=%s",
            schedule_row.get_id(),
            task_request.model_dump_json(exclude_none=True),
        )
    except Exception:
        logger.exception("failed to render scheduled dispatch payload for logging")
    dispatch_request = DispatchTaskRequest(
        type="dispatch_task_request",
        request=task_request,
    )
    logger.info(
        "dispatching scheduled task schedule_id=%s task_id=%s",
        schedule_row.get_id(),
        parent_task.id,
    )
    await post_dispatch_task(dispatch_request, task_repo)
    parent_task.last_ran = wall_millis_to_datetime(now_wall_millis())
    await parent_task.save(update_fields=["last_ran"])
    logger.info(
        "finished dispatching scheduled task schedule_id=%s task_id=%s",
        schedule_row.get_id(),
        parent_task.id,
    )


async def try_dispatch_chained_schedule_run(
    schedule_row: ttm.ScheduledTaskSchedule,
    task_repo: TaskRepository,
    *,
    completed_task_id: str,
    allowed_categories: set[str],
) -> bool:
    """Try to chain one immediate run after a completion event.

    Returns True if a chained run was dispatched, otherwise False.
    """
    parent_task = schedule_row.scheduled_task
    if parent_task is None:
        logger.warning(
            "chain: schedule_id=%s has no parent task",
            schedule_row.get_id(),
        )
        return False

    task_request_data = _parse_task_request_dict(parent_task.task_request)
    category = _normalized_category(task_request_data.get("category"))
    if category in NON_CHAINABLE_SCHEDULED_TASK_CATEGORIES:
        logger.info(
            "chain: schedule_id=%s skip category=%s because it is single-run only",
            schedule_row.get_id(),
            category,
        )
        return False

    normalized_allowed_categories = {
        _normalized_category(allowed_category)
        for allowed_category in allowed_categories
        if _normalized_category(allowed_category)
    }
    if category not in normalized_allowed_categories:
        logger.info(
            "chain: schedule_id=%s skip category=%s not in allowed set",
            schedule_row.get_id(),
            category,
        )
        return False

    now_utc = datetime.now(timezone.utc)
    if should_skip_due_to_planned_end(schedule_row, now_utc):
        logger.info(
            "chain: schedule_id=%s stop due to planned_end_at=%s",
            schedule_row.get_id(),
            schedule_row.planned_end_at,
        )
        return False

    if not _occurrence_allowed(schedule_row, now_utc, parent_task):
        logger.info(
            "chain: schedule_id=%s stop due to bounds (until/except_dates)",
            schedule_row.get_id(),
        )
        return False

    if await _schedule_has_active_tasks(schedule_row.get_id(), task_repo):
        logger.info(
            "chain: schedule_id=%s skip because another run is still active",
            schedule_row.get_id(),
        )
        return False

    if await _has_chained_child_for_parent(
        schedule_row.get_id(),
        completed_task_id,
        task_repo,
    ):
        logger.info(
            "chain: schedule_id=%s skip duplicate completion booking_id=%s",
            schedule_row.get_id(),
            completed_task_id,
        )
        return False

    await _dispatch_schedule_task_request(
        schedule_row,
        task_repo,
        chain_parent_task_id=completed_task_id,
    )
    return True


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


def _parse_at_time(at: Optional[str]) -> Optional[tuple[int, int]]:
    if not at:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", at)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _matches_schedule_start(
    schedule_row: ttm.ScheduledTaskSchedule,
    candidate_dt_utc: datetime,
) -> bool:
    at = _parse_at_time(schedule_row.at)
    if at is None:
        return False

    local_tz = datetime.now().astimezone().tzinfo
    local_dt = candidate_dt_utc.astimezone(local_tz)
    if (local_dt.hour, local_dt.minute) != at:
        return False

    period = schedule_row.period
    weekday_map = {
        ttm.ScheduledTaskSchedule.Period.Monday: 0,
        ttm.ScheduledTaskSchedule.Period.Tuesday: 1,
        ttm.ScheduledTaskSchedule.Period.Wednesday: 2,
        ttm.ScheduledTaskSchedule.Period.Thursday: 3,
        ttm.ScheduledTaskSchedule.Period.Friday: 4,
        ttm.ScheduledTaskSchedule.Period.Saturday: 5,
        ttm.ScheduledTaskSchedule.Period.Sunday: 6,
    }

    if period == ttm.ScheduledTaskSchedule.Period.Day:
        return True
    if period in weekday_map:
        return local_dt.weekday() == weekday_map[period]
    return False


def _normalize_start_from(
    start_from: Optional[datetime],
    at: Optional[str],
) -> Optional[datetime]:
    if start_from is None or at is None:
        return start_from
    at_time = _parse_at_time(at)
    if at_time is None:
        return start_from

    local_tz = datetime.now().astimezone().tzinfo
    local_dt = _ensure_utc_aware(start_from).astimezone(local_tz)
    local_dt = local_dt.replace(
        hour=at_time[0],
        minute=at_time[1],
        second=0,
        microsecond=0,
    )
    return local_dt.astimezone(timezone.utc)


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

    normalized_start_from = _normalize_start_from(
        schedule_request.start_from,
        schedule_request.at,
    )

    sche = await ttm.ScheduledTaskSchedule.create(
        scheduled_task=parent_task,
        start_from=normalize_to_utc(normalized_start_from),
        until=normalize_to_utc(schedule_request.until),
        planned_end_at=schedule_request.planned_end_at,
        at=schedule_request.at,
        every=schedule_request.every,
        period=schedule_request.period,
        dispatched=False,
    )

    after_dt = normalize_to_utc(normalized_start_from) or datetime.now(timezone.utc)
    next_run = compute_next_run(sche, after_dt)
    if normalized_start_from is not None:
        start_dt = normalize_to_utc(normalized_start_from)
        if start_dt is not None and _matches_schedule_start(sche, start_dt):
            if _occurrence_allowed(sche, start_dt, parent_task):
                if next_run is None or next_run > start_dt:
                    next_run = start_dt
    if next_run is None:
        raise HTTPException(422, "Task is never going to run")

    sche.next_run_at = next_run
    await sche.save(update_fields=["next_run_at"])
    logger.info(
        "scheduled task created schedule_id=%s parent_task_id=%s start_from=%s next_run_at=%s until=%s planned_end_at=%s period=%s at=%s",
        sche.get_id(),
        parent_task.id,
        sche.start_from,
        sche.next_run_at,
        sche.until,
        sche.planned_end_at,
        sche.period,
        sche.at,
    )
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


async def _cancel_overdue_schedule_tasks(
    schedule_row: ttm.ScheduledTaskSchedule,
    task_repo: TaskRepository,
    now_utc: datetime,
) -> None:
    """Cancel live tasks once their schedule has passed the planned end time.

    We only target tasks that are still in a live status, so finished work stays
    untouched.
    """
    if schedule_row.scheduled_task is None:
        return

    schedule_id = schedule_row.get_id()
    label = Labels.from_strings([f"scheduled_schedule_id={schedule_id}"])
    task_states = await task_repo.query_task_states(label=label)

    for task_state in task_states:
        await cancel_active_scheduled_task_if_overdue(
            schedule_row,
            task_state,
            task_repo,
            now_utc,
        )


async def cancel_active_scheduled_task_if_overdue(
    schedule_row: ttm.ScheduledTaskSchedule,
    task_state,
    task_repo: TaskRepository,
    now_utc: datetime,
) -> bool:
    """Cancel a live scheduled task if the schedule window has already closed."""
    if schedule_row.scheduled_task is None:
        return False

    if task_state.status not in ACTIVE_SCHEDULED_TASK_STATUSES:
        return False

    if not should_skip_due_to_planned_end(
        schedule_row,
        now_utc,
        candidate_dt_utc=None,
    ):
        return False

    schedule_id = schedule_row.get_id()
    cancel_request = CancelTaskRequest(
        type="cancel_task_request",
        task_id=task_state.booking.id,
        labels=[
            f"scheduled_schedule_id={schedule_id}",
            "scheduled_end_time_auto_cancel",
        ],
    )
    try:
        logger.info(
            "canceling active scheduled task schedule_id=%s task_id=%s status=%s",
            schedule_id,
            task_state.booking.id,
            task_state.status,
        )
        await tasks_service().call(cancel_request.model_dump_json(exclude_none=True))
        return True
    except Exception:
        logger.exception(
            "failed to cancel scheduled task schedule_id=%s task_id=%s",
            schedule_id,
            task_state.booking.id,
        )
        return False


async def _dispatch_scheduled_schedule(schedule_id: int):
    schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
        _id=schedule_id
    ).select_related("scheduled_task")
    if schedule_row is None:
        logger.warning("scheduled task schedule_id=%s no longer exists", schedule_id)
        return

    schedule_start_dt = schedule_row.next_run_at or schedule_row.start_from
    if schedule_start_dt is not None:
        now_utc = datetime.now(timezone.utc)
        schedule_start = _ensure_utc_aware(schedule_start_dt)
        logger.info(
            "schedule_id=%s dispatch check next_run_at=%s now=%s",
            schedule_id,
            schedule_start.isoformat(),
            now_utc.isoformat(),
        )
        if schedule_start > now_utc:
            logger.warning(
                "schedule_id=%s dispatch requested before next_run_at=%s (now=%s); deferring",
                schedule_id,
                schedule_start.isoformat(),
                now_utc.isoformat(),
            )
            await ttm.ScheduledTaskSchedule.filter(_id=schedule_id).update(
                dispatched=False
            )
            return
    else:
        logger.warning(
            "schedule_id=%s dispatch requested without next_run_at/start_from; deferring",
            schedule_id,
        )
        await ttm.ScheduledTaskSchedule.filter(_id=schedule_id).update(dispatched=False)
        return

    await _dispatch_schedule_task_request(
        schedule_row,
        TaskRepository(INTERNAL_USER),
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
        # Otherwise, update next_run_at and clear dispatched so scheduler can pick it up later
        schedule_row.next_run_at = next_run
        schedule_row.dispatched = False
        await schedule_row.save(update_fields=["next_run_at", "dispatched"])
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
    task_repo = TaskRepository(INTERNAL_USER)
    while True:
        try:
            now = datetime.now(timezone.utc)
            logger.info("SCHEDULER TICK now=%s tzinfo=%s", now.isoformat(), now.tzinfo)
            logger.info("NOW=%s type=%s", now, type(now))

            overdue_schedules = await (
                ttm.ScheduledTaskSchedule.filter(
                    planned_end_at__isnull=False,
                    start_from__isnull=False,
                )
                .select_related("scheduled_task")
                .order_by("start_from", "_id")
            )
            for schedule_row in overdue_schedules:
                await _cancel_overdue_schedule_tasks(schedule_row, task_repo, now)

            # Query candidate schedules, then do the UTC comparison in Python so the
            # scheduler remains consistent whether the DB round-trips naive or aware
            # datetimes.
            due_schedules = await (
                ttm.ScheduledTaskSchedule.filter(
                    dispatched=False,
                )
                .select_related("scheduled_task")
                .order_by("next_run_at", "_id")
            )
            logger.info("DUE COUNT=%s", len(due_schedules))

            for schedule_row in due_schedules:
                if schedule_row.next_run_at is None:
                    next_run = compute_next_run(schedule_row, now)
                    if next_run is None:
                        await ttm.ScheduledTaskSchedule.filter(
                            _id=schedule_row.get_id()
                        ).update(dispatched=True)
                        continue
                    schedule_row.next_run_at = next_run
                    await schedule_row.save(update_fields=["next_run_at"])

                if schedule_row.next_run_at is None:
                    continue

                schedule_start = _ensure_utc_aware(schedule_row.next_run_at)

                logger.info(
                    "DB next_run_at=%s type=%s tz=%s",
                    schedule_row.next_run_at,
                    type(schedule_row.next_run_at),
                    getattr(schedule_row.next_run_at, "tzinfo", None),
                )

                # Defensive tzinfo check: treat naive DB datetimes as UTC but log.
                if schedule_row.next_run_at.tzinfo is None:
                    logger.warning(
                        "schedule_id=%s has naive next_run_at, treating as UTC: %s",
                        schedule_row.get_id(),
                        schedule_row.next_run_at,
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
                    "FOUND DUE TASK schedule_id=%s task_id=%s next_run_at=%s next_run_tz=%s now=%s now_tz=%s",
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
                    "scheduler triggered schedule_id=%s task_id=%s next_run_at=%s now=%s",
                    schedule_id,
                    getattr(schedule_row.scheduled_task, "id", None),
                    schedule_row.next_run_at.isoformat()
                    if schedule_row.next_run_at is not None
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

    if scheduled_task_request.schedules:
        first = scheduled_task_request.schedules[0]
        logger.info(
            "scheduled_task create requester=%s start_from=%s at=%s period=%s",
            scheduled_task_request.task_request.requester,
            getattr(first.start_from, "isoformat", lambda: None)(),
            first.at,
            first.period,
        )

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
    logger.info(
        "scheduled task calendar query start_before=%s until_after=%s limit=%s offset=%s order_by=%s",
        start_before,
        until_after,
        pagination.limit,
        pagination.offset,
        pagination.order_by,
    )
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
    logger.info("scheduled task calendar query returned %s tasks", len(results))
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


@router.get("/{task_id}/runs", response_model=list[ScheduleRun])
async def get_scheduled_task_runs(task_id: int) -> list[ScheduleRun]:
    """
    Returns grouped loop runs per schedule for a given scheduled task id.
    Each loop corresponds to a single dispatched RMF task (one patrol round).
    Grouping is based on the `scheduled_schedule_id:<id>` label attached at dispatch.
    """
    task = await ttm.ScheduledTask.get_or_none(id=task_id).prefetch_related("schedules")
    if task is None:
        raise HTTPException(404)

    repo = TaskRepository(INTERNAL_USER)
    groups: list[ScheduleRun] = []

    for sche in task.schedules:
        sched_id = sche.get_id()
        label = Labels.from_strings([f"scheduled_schedule_id={sched_id}"])
        loops_states = await repo.query_task_states(label=label)
        # Sort by start time for readability
        loops_states.sort(key=lambda s: (s.unix_millis_start_time or 0))
        loops: list[LoopSummary] = [
            LoopSummary(
                task_id=ts.booking.id,
                status=ts.status.value if ts.status else None,
                unix_millis_start_time=ts.unix_millis_start_time,
                unix_millis_finish_time=ts.unix_millis_finish_time,
            )
            for ts in loops_states
        ]
        groups.append(
            ScheduleRun(
                schedule_id=sched_id,
                start_from=sche.start_from,
                until=sche.until,
                planned_end_at=sche.planned_end_at,
                loops=loops,
            )
        )

    return groups


@router.post("/{task_id}/backfill_labels")
async def backfill_labels_for_scheduled_task(task_id: int) -> dict:
    """
    Backfill TaskLabel rows for past loops dispatched from the given
    scheduled task by copying labels from the saved TaskRequest when
    RMF did not echo labels on booking. Useful to make historical runs
    appear in the grouping endpoint.
    """
    # Validate parent exists
    parent = await ttm.ScheduledTask.get_or_none(id=task_id)
    if parent is None:
        raise HTTPException(404)

    repo = TaskRepository(INTERNAL_USER)
    updated = 0
    scanned = 0

    # Scan all saved task requests and pick those that contain the
    # correlation label for this parent scheduled task.
    # This is a conservative pass; DBs are typically small in demos.
    all_reqs = await ttm.TaskRequest.all()
    for req in all_reqs:
        scanned += 1
        try:
            data = req.request if isinstance(req.request, dict) else None
            if not data:
                continue
            labels: list[str] = data.get("labels") or []
            if not isinstance(labels, list):
                continue
            # Accept both legacy colon and new equals labels
            if not (
                f"scheduled_task_id:{task_id}" in labels
                or f"scheduled_task_id={task_id}" in labels
            ):
                continue

            # Load task state row; if exists, write labels if any are missing
            db_task_state = await ttm.TaskState.get_or_none(id_=req.id)
            if db_task_state is None:
                continue

            try:
                # Normalize legacy colon labels to key=value before parsing
                norm = []
                for s in labels:
                    if s.startswith("scheduled_task_id:"):
                        norm.append(s.replace(":", "=", 1))
                    elif s.startswith("scheduled_schedule_id:"):
                        norm.append(s.replace(":", "=", 1))
                    else:
                        norm.append(s)
                parsed = Labels.from_strings(norm)
                await repo.save_task_labels(db_task_state, parsed)
                updated += 1
            except Exception:
                logger.exception(
                    "backfill: failed to save labels for task_id=%s", req.id
                )
        except Exception:
            logger.exception("backfill: error scanning saved task request")

    return {"scanned": scanned, "updated": updated}
