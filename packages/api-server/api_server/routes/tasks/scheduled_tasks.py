import asyncio
import json
import re
import threading
import time as pytime
import traceback
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Optional

import schedule
import tortoise.transactions
from cairo import Status
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
from api_server.repositories import FleetRepository, TaskRepository
from api_server.rmf_io import tasks_service
from api_server.ros import ros_node
from api_server.utils.schedule_utils import (
    _occurrence_allowed,
    compute_next_run,
    should_skip_due_to_planned_end,
)
from api_server.utils.time_utils import (
    datetime_to_wall_millis,
    now_wall_millis,
    sim_to_real,
    wall_millis_to_datetime,
)

from .priority import NORMAL_PRIORITY_VALUE, PRIORITY_LABEL_VALUES, task_priority_label
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

    # Compute finish time in UTC. If the finish timestamp looks like simulation time (small number),
    # convert it to real wall-clock using ROS clock offsets.
    finish_ms = task_state.unix_millis_finish_time
    finish_dt_utc = None
    try:
        if finish_ms is None:
            return False
        # Heuristic: if timestamp is too small to be real unix ms (before ~2002), treat as sim time
        if int(finish_ms) < 1_000_000_000_000:
            node = ros_node()
            if node is not None:
                try:
                    now_sim = node.get_clock().now().nanoseconds // 1_000_000
                    now_real = int(pytime.time() * 1000)
                    real_ms = sim_to_real(int(finish_ms), int(now_sim), int(now_real))
                    finish_dt_utc = wall_millis_to_datetime(real_ms)
                except Exception:
                    logger.exception("failed to convert sim time to real time")
        if finish_dt_utc is None:
            finish_dt_utc = wall_millis_to_datetime(int(finish_ms))
    except Exception:
        logger.exception("invalid finish timestamp")
        return False

    # Try to align the recorded end time to the schedule's original timezone
    actual_time_str = None
    try:
        logger.info(
            "update_completed_clean_schedule_end called schedule_id=%s start_from=%s at=%s unix_finish=%s",
            schedule_row.get_id(),
            getattr(schedule_row, "start_from", None),
            getattr(schedule_row, "at", None),
            getattr(task_state, "unix_millis_finish_time", None),
        )
        if schedule_row.start_from and schedule_row.at:
            # Determine offset between stored start_from (UTC) and the original 'at' hour
            s = schedule_row.start_from
            if s.tzinfo is None:
                s = s.replace(tzinfo=timezone.utc)
            s_utc = s.astimezone(timezone.utc)
            at_h = int(str(schedule_row.at).split(":")[0])
            # Compute offset hours (may be negative); keep minutes in account
            offset_hours = at_h - s_utc.hour
            # Apply offset to UTC finish to get a wall-clock hour consistent with schedule.at
            adjusted = finish_dt_utc + timedelta(hours=offset_hours)
            actual_time_str = adjusted.strftime("%H:%M")
        else:
            # Fallback: use server local timezone
            local_tz = datetime.now().astimezone().tzinfo
            finish_dt = finish_dt_utc.astimezone(local_tz)
            actual_time_str = finish_dt.strftime("%H:%M")
    except Exception:
        # On any failure, fallback to server local
        local_tz = datetime.now().astimezone().tzinfo
        finish_dt = finish_dt_utc.astimezone(local_tz)
        actual_time_str = finish_dt.strftime("%H:%M")
        logger.exception("failed to align actual_end_time, falling back to local time")

    # Do NOT overwrite the schedule's `planned_end_at` for recurring schedules.
    # `planned_end_at` represents the daily planned cutoff and should remain
    # stable across occurrences. Overwriting it with a single run's actual
    # finish time can prevent future occurrences from being scheduled.
    schedule_row.actual_end_time = actual_time_str
    schedule_row.actual_end_iso = finish_dt_utc.replace(tzinfo=timezone.utc).isoformat()
    await schedule_row.save(update_fields=["actual_end_time", "actual_end_iso"])
    logger.info(
        "saved actual_end_time=%s for schedule_id=%s",
        actual_time_str,
        schedule_row.get_id(),
    )
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

    # DIAGNOSTIC (ERROR-level so it survives ERROR-only log capture):
    # attribute every loop dispatch to its source. chain_parent set => chaining;
    # None => scheduler-driven occurrence start.
    logger.info(
        "loop dispatch schedule_id=%s source=%s chain_parent=%s ended_latch=%s",
        schedule_row.get_id(),
        "chain" if chain_parent_task_id is not None else "scheduler",
        chain_parent_task_id,
        schedule_row.get_id() in _ended_schedules,
    )

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
    await post_dispatch_task(
        dispatch_request, task_repo, FleetRepository(INTERNAL_USER)
    )
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

    # If end-time recovery already fired for this schedule's current occurrence,
    # the window is closed: do not chain another loop. This is authoritative and
    # independent of the planned_end_at clock check, so a late-completing loop
    # cannot send the robot back out after it has been recalled to the charger.
    if schedule_row.get_id() in _ended_schedules:
        logger.info(
            "chain: schedule_id=%s skip because end-time recovery already fired",
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


async def _trigger_post_task_recovery(task_state, schedule_row, task_repo, now_utc):

    task_id = task_state.booking.id
    schedule_id = schedule_row.get_id()
    logger.info("RECOVERY_TRIGGERED task_id=%s schedule_id=%s", task_id, schedule_id)

    await asyncio.sleep(2.0)

    # 1. Fetch all tasks for this schedule
    label = Labels.from_strings([f"scheduled_schedule_id={schedule_id}"])
    states = await task_repo.query_task_states(label=label)

    # 2. Determine what is done vs not done (you already have phases/events)
    completed = []
    skipped = []

    terminal_ok_statuses = {TaskStatus.completed}
    terminal_bad_statuses = {
        TaskStatus.canceled,
        TaskStatus.failed,
        TaskStatus.skipped,
    }

    for s in states:
        logger.error(
            "RECOVERY_STATE task=%s status=%s finish=%s assigned=%s",
            s.booking.id,
            s.status,
            s.unix_millis_finish_time,
            getattr(s.assigned_to, "name", None),
        )
    for s in states:
        sid = s.booking.id
        if s.status in terminal_ok_statuses:
            # Finished successfully before the window closed.
            completed.append(sid)
        elif s.status in terminal_bad_statuses or s.unix_millis_finish_time is None:
            # Either already terminal-bad, or still has no finish time which
            # means it was mid-execution when time ran out → skipped/cancelled.
            skipped.append(sid)
        else:
            # Has a finish time but unknown status — treat as completed.
            completed.append(sid)

    logger.error(
        "RECOVERY schedule_id=%s completed=%s skipped=%s",
        schedule_id,
        completed,
        skipped,
    )
    robot_name = getattr(task_state.assigned_to, "name", None)

    logger.error(
        "RECOVERY initial robot_name=%s task_id=%s",
        robot_name,
        task_id,
    )

    if not robot_name:
        # Re-query by task id label to get a fresher copy.
        try:
            fresh_states = await task_repo.query_task_states(
                label=Labels.from_strings([f"scheduled_schedule_id={schedule_id}"])
            )
            for fs in fresh_states:
                candidate = getattr(fs.assigned_to, "name", None)
                if candidate:
                    robot_name = candidate
                    break
        except Exception:
            logger.exception(
                "RECOVERY failed to re-fetch task state for robot name task_id=%s",
                task_id,
            )

    if robot_name:
        logger.error(
            "RECOVERY dispatching return-to-charger robot=%s schedule_id=%s",
            robot_name,
            schedule_id,
        )
        await _dispatch_return_to_charger(robot_name)
    else:
        logger.warning(
            "RECOVERY could not resolve robot name for task_id=%s; "
            "charger dispatch skipped",
            task_id,
        )


async def _dispatch_return_to_charger(robot_name: str):

    CHARGER_MAP = {
        "TinyRobot1": "tinyRobot1_charger",
    }
    charger_place = CHARGER_MAP.get(robot_name)

    logger.info("RETURN_TO_CHARGER robot=%s", robot_name)

    request_payload = {
        "type": "robot_task_request",
        "robot": robot_name,
        "fleet": "TinyRobot",
        "request": {
            "category": "patrol",
            "description": {"places": [charger_place], "rounds": 1},
            "unix_millis_earliest_start_time": 0,
            "unix_millis_request_time": now_wall_millis(),
            "labels": ["auto_return_to_charger"],
        },
    }

    try:
        logger.error(
            "RETURN_TO_CHARGER PAYLOAD=%s",
            json.dumps(request_payload, indent=2),
        )

        result = await tasks_service().call(json.dumps(request_payload))

        logger.error(
            "RETURN_TO_CHARGER RESULT=%s",
            result,
        )

        logger.info(
            "RETURN_TO_CHARGER dispatched successfully robot=%s",
            robot_name,
        )

    except Exception:
        logger.exception("RETURN_TO_CHARGER dispatch failed robot=%s", robot_name)


_recovery_dispatched: set[str] = set()

# Schedules whose end-time recovery (cancel + return-to-charger) has already
# fired for the current occurrence. Once a schedule is here, no further loops
# may be chained for it — the robot must completely forget the incomplete task
# until a genuinely new occurrence starts. Re-enabled in
# `_dispatch_scheduled_schedule`, which only runs at the start of a fresh
# scheduler-driven occurrence.
_ended_schedules: set[int] = set()


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
    logger.info(
        "CANCEL_CHECK_ENTER schedule_id=%s task_id=%s status=%s now=%s planned_end=%s",
        schedule_row.get_id(),
        task_state.booking.id,
        task_state.status,
        now_utc,
        schedule_row.planned_end_at,
    )
    task_id = task_state.booking.id
    if task_id in _recovery_dispatched:
        logger.info("CANCEL_SKIP recovery already dispatched task_id=%s", task_id)
        return False

    try:
        logger.info(
            "canceling active scheduled task schedule_id=%s task_id=%s status=%s",
            schedule_id,
            task_state.booking.id,
            task_state.status,
        )
        await tasks_service().call(cancel_request.model_dump_json(exclude_none=True))
        logger.error(
            " CANCEL SUCCESS task_id=%s",
            task_id,
        )
        _recovery_dispatched.add(task_id)
        # Close this schedule's occurrence so no further loops can be chained.
        # The robot is being recalled to the charger; the incomplete loop must
        # be forgotten, not resumed.
        _ended_schedules.add(schedule_id)
        logger.info(
            "marked schedule_id=%s as ended; chaining disabled until next occurrence",
            schedule_id,
        )

        asyncio.create_task(
            _trigger_post_task_recovery(task_state, schedule_row, task_repo, now_utc)
        )

        return True

    except Exception:
        logger.exception(
            "failed to cancel scheduled task schedule_id=%s task_id=%s",
            schedule_id,
            task_id,
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

    # A fresh scheduler-driven occurrence is starting: re-enable chaining for
    # this schedule in case a previous occurrence was ended by end-time recovery.
    _ended_schedules.discard(schedule_id)

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
        # Clear any per-occurrence actual end markers so future occurrences are
        # not influenced by the previous run's finish time when rendering.
        schedule_row.actual_end_time = None
        schedule_row.actual_end_iso = None
        await schedule_row.save(
            update_fields=[
                "next_run_at",
                "dispatched",
                "actual_end_time",
                "actual_end_iso",
            ]
        )
        logger.info(
            "schedule_id=%s next_run updated to %s (will fire again)",
            schedule_id,
            next_run.isoformat(),
        )
    except Exception:
        logger.exception(
            "failed computing or persisting next run for schedule_id=%s", schedule_id
        )


def _schedule_priority_value(schedule_row: ttm.ScheduledTaskSchedule) -> int:
    """Read the priority of a schedule's stored task request.

    `scheduled_task.task_request` is stored as a plain JSON dict, not a
    TaskRequest model, so this mirrors `priority.priority_value` for dicts
    rather than reusing it directly.
    """
    scheduled_task = schedule_row.scheduled_task
    raw_task_request = (
        getattr(scheduled_task, "task_request", None) if scheduled_task else None
    )
    if not isinstance(raw_task_request, dict):
        return NORMAL_PRIORITY_VALUE
    priority = raw_task_request.get("priority")
    if isinstance(priority, dict):
        value = priority.get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    label = task_priority_label(raw_task_request.get("labels"))
    if label is not None:
        return PRIORITY_LABEL_VALUES[label]
    return NORMAL_PRIORITY_VALUE


async def _scheduler_tick(task_repo: TaskRepository) -> None:
    """Run a single scheduler pass: cancel overdue schedules, then claim
    and dispatch schedules due now, highest priority first among ties.
    """
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

        # Gathered here, then claimed/dispatched in priority order below —
        # if two schedules are due in the same tick (e.g. a Normal and a
        # Critical both starting at 10:00), the Critical must be claimed
        # and dispatched first rather than whichever happened to sort
        # first by next_run_at/_id.
        ready_schedules: list[tuple[ttm.ScheduledTaskSchedule, datetime]] = []

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

            ready_schedules.append((schedule_row, schedule_start))

        # Highest priority first among schedules due in this same tick;
        # ties broken by original start time / id for determinism.
        ready_schedules.sort(
            key=lambda item: (
                -_schedule_priority_value(item[0]),
                item[1],
                item[0].get_id(),
            )
        )

        for schedule_row, _schedule_start in ready_schedules:
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

            def _bg_done_callback(t: asyncio.Task, schedule_id: int = schedule_id):
                try:
                    exc = t.exception()
                except asyncio.CancelledError:
                    return
                if exc:
                    tb = "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    )
                    logger.error(
                        "background dispatch failed schedule_id=%s exception=%s\n%s",
                        schedule_id,
                        exc,
                        tb,
                    )

            task.add_done_callback(_bg_done_callback)
    except Exception:
        logger.exception("scheduler tick error")


async def scheduler_loop(poll_interval: float = 1.0):
    logger.info("UTC scheduler loop started poll_interval=%s", poll_interval)
    task_repo = TaskRepository(INTERNAL_USER)
    while True:
        await _scheduler_tick(task_repo)
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
    # Quick debug: log schedule end fields to help frontend verification
    try:
        for r in results:
            await r.fetch_related("schedules")
            for s in r.schedules:
                logger.info(
                    "schedule_debug task_id=%s schedule_id=%s planned_end_at=%s actual_end_time=%s actual_end_iso=%s dispatched=%s",
                    getattr(r, "id", None),
                    getattr(s, "_id", None),
                    getattr(s, "planned_end_at", None),
                    getattr(s, "actual_end_time", None),
                    getattr(s, "actual_end_iso", None),
                    getattr(s, "dispatched", None),
                )
    except Exception:
        logger.exception("failed to log schedule_debug info")
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
