import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple, cast

from fastapi import Body, Depends, HTTPException, Path, Query
from reactivex import operators as rxops

from api_server import models as mdl
from api_server.dependencies import (
    between_query,
    finish_time_between_query,
    pagination_query,
    sio_user,
    start_time_between_query,
)
from api_server.fast_io import FastIORouter, SubscriptionRequest
from api_server.logger import logger
from api_server.repositories import (
    FleetRepository,
    TaskRepository,
    fleet_repo_dep,
    task_repo_dep,
)
from api_server.response import RawJSONResponse
from api_server.rmf_io import task_events, tasks_service
from api_server.ros import ros_node
from api_server.utils.time_utils import real_to_sim

from .priority import (
    CRITICAL_PRIORITY_VALUE,
    PRIORITY_LABEL_VALUES,
    can_preempt,
    priority_value,
    task_priority_label,
)

router = FastIORouter(tags=["Tasks"])


def _use_sim_time() -> bool:
    raw = os.environ.get("RMF_SERVER_USE_SIM_TIME")
    if not raw:
        node = ros_node()
        if node is None:
            return False
        try:
            clock = node.get_clock()
            if hasattr(clock, "ros_time_is_active"):
                return bool(clock.ros_time_is_active)
        except Exception:
            logger.debug("failed to read ros_time_is_active", exc_info=True)
        try:
            param = node.get_parameter("use_sim_time")
            return bool(getattr(param, "value", False))
        except Exception:
            logger.debug("failed to read use_sim_time parameter", exc_info=True)
        return False
    return raw.lower() not in ("0", "false")


def _convert_request_times_to_sim(request: mdl.TaskRequest) -> None:
    if not _use_sim_time():
        return
    node = ros_node()
    if node is None:
        return

    now_sim = node.get_clock().now().nanoseconds // 1_000_000
    now_real = int(time.time() * 1000)

    if request.unix_millis_earliest_start_time:
        request.unix_millis_earliest_start_time = real_to_sim(
            request.unix_millis_earliest_start_time,
            now_real,
            now_sim,
        )
    if request.unix_millis_request_time:
        request.unix_millis_request_time = real_to_sim(
            request.unix_millis_request_time,
            now_real,
            now_sim,
        )


@router.get("/{task_id}/request", response_model=mdl.TaskRequest)
async def get_task_request(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: str = Path(..., description="task_id"),
):
    result = await task_repo.get_task_request(task_id)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.get("", response_model=List[mdl.TaskState])
async def query_task_states(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: Optional[str] = Query(
        None, description="comma separated list of task ids"
    ),
    category: Optional[str] = Query(
        None, description="comma separated list of task categories"
    ),
    assigned_to: Optional[str] = Query(
        None, description="comma separated list of assigned robot names"
    ),
    start_time_between: Optional[Tuple[datetime, datetime]] = Depends(
        start_time_between_query
    ),
    finish_time_between: Optional[Tuple[datetime, datetime]] = Depends(
        finish_time_between_query
    ),
    status: Optional[str] = Query(None, description="comma separated list of statuses"),
    label: str
    | None = Query(
        None,
        description="comma separated list of labels, each item must be in the form <key>=<value>, multiple items will filter tasks with all the labels",
    ),
    pagination: mdl.Pagination = Depends(pagination_query),
):
    return await task_repo.query_task_states(
        task_id=task_id.split(",") if task_id else None,
        category=category.split(",") if category else None,
        assigned_to=assigned_to.split(",") if assigned_to else None,
        start_time_between=start_time_between,
        finish_time_between=finish_time_between,
        status=status.split(",") if status else None,
        label=mdl.Labels.from_strings(label.split(",")) if label else None,
        pagination=pagination,
    )


@router.get("/{task_id}/state", response_model=mdl.TaskState)
async def get_task_state(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: str = Path(..., description="task_id"),
):
    """
    Available in socket.io
    """
    result = await task_repo.get_task_state(task_id)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.sub("/{task_id}/state", response_model=mdl.TaskState)
async def sub_task_state(req: SubscriptionRequest, task_id: str):
    user = sio_user(req)
    task_repo = TaskRepository(user)
    obs = task_events.task_states.pipe(rxops.filter(lambda x: x.booking.id == task_id))
    current_state = await get_task_state(task_repo, task_id)
    if current_state:
        return obs.pipe(rxops.start_with(current_state))
    return obs


@router.get("/{task_id}/log", response_model=mdl.TaskEventLog)
async def get_task_log(
    task_repo: TaskRepository = Depends(task_repo_dep),
    task_id: str = Path(..., description="task_id"),
    between: Tuple[int, int] = Depends(between_query),
):
    """
    Available in socket.io
    """

    result = await task_repo.get_task_log(task_id, between)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.sub("/{task_id}/log", response_model=mdl.TaskEventLog)
async def sub_task_log(_req: SubscriptionRequest, task_id: str):
    return task_events.task_event_logs.pipe(
        rxops.filter(lambda x: x.task_id == task_id)
    )


@router.post("/activity_discovery", response_model=mdl.ActivityDiscovery)
async def post_activity_discovery(
    request: mdl.ActivityDiscoveryRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/cancel_task", response_model=mdl.TaskCancelResponse)
async def post_cancel_task(
    request: mdl.CancelTaskRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post(
    "/dispatch_task",
    response_model=mdl.TaskDispatchResponse,
    responses={400: {"model": mdl.TaskDispatchResponse}},
)
async def post_dispatch_task(
    request: mdl.DispatchTaskRequest = Body(...),
    task_repo: TaskRepository = Depends(task_repo_dep),
    fleet_repo: FleetRepository = Depends(fleet_repo_dep),
):
    _convert_request_times_to_sim(request.request)
    priority_label = _apply_priority_labels(request.request)
    is_critical_request = _is_critical_task_request(request.request)
    logger.info(
        "post_dispatch_task() calling RMF service task_id=%s request_type=%s",
        getattr(request.request, "task_id", None),
        request.type,
    )
    resp = mdl.TaskDispatchResponse.model_validate_json(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
    if not resp.root.success:
        logger.info("post_dispatch_task() RMF service returned failure")
        return RawJSONResponse(resp.model_dump_json(), 400)
    task_state = cast(mdl.TaskDispatchResponse1, resp.root).state
    # Save the original dispatch request first so save_task_state() can
    # fall back to it for labels when RMF does not echo labels in booking.
    await task_repo.save_task_request(task_state.booking.id, request.request)
    await task_repo.save_task_state(task_state)
    logger.info(
        "post_dispatch_task() RMF dispatch success booking_id=%s task_id=%s",
        task_state.booking.id,
        task_state.booking.id,
    )
    # If the original request is critical, spawn a watcher to preempt a
    # lower-priority active task and requeue it after the critical is placed.
    try:
        if is_critical_request:
            # spawn background watcher (fire-and-forget)
            _spawn_background_task(
                _handle_critical_preemption(
                    task_state.booking.id,
                    task_repo,
                    fleet_repo,
                )
            )
    except Exception:
        logger.exception("failed to spawn critical preemption watcher")
    return resp


def _apply_priority_labels(task_request: mdl.TaskRequest) -> Optional[str]:
    priority_label = task_priority_label(task_request.labels)
    if priority_label is None:
        return None

    task_request.priority = {
        "type": "binary",
        "value": PRIORITY_LABEL_VALUES[priority_label],
    }
    return priority_label


def _is_critical_task_request(task_request: mdl.TaskRequest) -> bool:
    return priority_value(task_request) >= CRITICAL_PRIORITY_VALUE


def _spawn_background_task(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "critical watcher: no running loop available; skipping background task"
        )
        return None
    return loop.create_task(coro)


def _status_text(status: object | None) -> Optional[str]:
    if status is None:
        return None
    return str(getattr(status, "value", status))


def _is_service_success(response_json: str) -> bool:
    try:
        response = json.loads(response_json)
    except Exception:
        return False

    if isinstance(response, dict):
        success = response.get("success", False)
        if hasattr(success, "root"):
            return bool(success.root)
        return bool(success)

    return False


def _robot_is_released(
    robot_task_id: Optional[str],
    paused_task_id: str,
) -> bool:
    return robot_task_id != paused_task_id


async def _wait_for_robot_stop(
    fleet_repo: FleetRepository,
    fleet_name: str,
    robot_name: str,
    paused_task_id: str,
    timeout: float = 5.0,
) -> Tuple[bool, Optional[str]]:
    start = time.monotonic()
    last_status: Optional[str] = None

    while time.monotonic() - start < timeout:
        fleet_state = await fleet_repo.get_fleet_state(fleet_name)
        robot = (
            fleet_state.robots.get(robot_name)
            if fleet_state and fleet_state.robots
            else None
        )

        if robot:
            last_status = _status_text(robot.status)
            if _robot_is_released(robot.task_id, paused_task_id):
                return True, last_status

        await asyncio.sleep(0.2)

    return False, last_status


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _robot_state_debug(
    fleet_state: Optional[mdl.FleetState], robot_name: Optional[str]
) -> str:
    if fleet_state is None:
        return "fleet_state=None"

    robots = fleet_state.robots or {}
    if robot_name and robot_name in robots:
        robot_state = robots[robot_name]
        robot_status = getattr(robot_state.status, "value", robot_state.status)
        return (
            f"fleet={fleet_state.name} robot={robot_name} "
            f"status={robot_status} task_id={robot_state.task_id}"
        )

    parts = []
    for name, robot_state in robots.items():
        robot_status = getattr(robot_state.status, "value", robot_state.status)
        parts.append(f"{name}:{robot_status}:{robot_state.task_id}")
    robots_repr = "; ".join(parts) if parts else "<no robots>"
    return f"fleet={fleet_state.name} robots=[{robots_repr}]"


async def _debug_poll_fleet_state(
    fleet_repo: FleetRepository,
    task_repo: TaskRepository,
    fleet_name: Optional[str],
    robot_name: Optional[str],
    task_id: str,
    stop_event: asyncio.Event,
    interval_s: float = 0.2,
    max_duration_s: float = 5.0,
) -> None:
    if not fleet_name:
        logger.info(
            "FLEET DEBUG POLL skipped ts=%s task_id=%s robot_name=%s reason=no_fleet_name",
            _utc_now_iso(),
            task_id,
            robot_name,
        )
        return

    deadline = time.monotonic() + max_duration_s
    while time.monotonic() < deadline and not stop_event.is_set():
        try:
            fleet_state = await fleet_repo.get_fleet_state(fleet_name)
            task_state = await task_repo.get_task_state(task_id)
            task_status = getattr(getattr(task_state, "status", None), "value", None)
            dispatch_status = getattr(
                getattr(getattr(task_state, "dispatch", None), "status", None),
                "value",
                None,
            )
            interruption_tokens = []
            if task_state and task_state.interruptions:
                interruption_tokens = list(task_state.interruptions.keys())
            logger.info(
                "FLEET DEBUG POLL ts=%s task_id=%s task_status=%s dispatch_status=%s interruptions=%s %s raw=%s task_raw=%s",
                _utc_now_iso(),
                task_id,
                task_status,
                dispatch_status,
                interruption_tokens,
                _robot_state_debug(fleet_state, robot_name),
                None
                if fleet_state is None
                else fleet_state.model_dump_json(exclude_none=True),
                None
                if task_state is None
                else task_state.model_dump_json(exclude_none=True),
            )
        except Exception:
            logger.exception(
                "FLEET DEBUG POLL failed ts=%s task_id=%s fleet_name=%s robot_name=%s",
                _utc_now_iso(),
                task_id,
                fleet_name,
                robot_name,
            )
        await asyncio.sleep(interval_s)

    logger.info(
        "FLEET DEBUG POLL ended ts=%s task_id=%s fleet_name=%s robot_name=%s",
        _utc_now_iso(),
        task_id,
        fleet_name,
        robot_name,
    )


def _extract_assigned_robot_name(task_state: mdl.TaskState) -> Optional[str]:
    """Get the assigned robot from dispatch assignment or task assignment."""
    dispatch = getattr(task_state, "dispatch", None)
    assignment = getattr(dispatch, "assignment", None) if dispatch else None
    expected = getattr(assignment, "expected_robot_name", None) if assignment else None
    if expected:
        return expected
    assigned_to = getattr(task_state, "assigned_to", None)
    return getattr(assigned_to, "name", None) if assigned_to else None


TERMINAL_TASK_STATUSES = {"completed", "failed", "canceled", "killed"}
ACTIVE_TASK_STATUSES = {"underway", "blocked", "delayed", "standby"}
PREEMPTION_WATCH_WINDOW_S = 30.0
PREEMPTION_POLL_INTERVAL_S = 1.0
# Every individual RMF call made by the preemption pipeline (kill, forced
# dispatch, cancel) must be bounded well below RmfService.call's 120s
# default. Without this, a single hung call keeps the preemption lock held
# for up to two minutes, silently blocking every other critical in the
# meantime (observed as "criticals stop preempting until the server is
# restarted").
PREEMPTION_RMF_CALL_TIMEOUT_S = 5.0
# Hard ceiling on the whole post-kill sequence (wait-for-stop, forced
# dispatch, requeue), run outside the lock. Belt-and-suspenders: bounds
# total time even if a step misbehaves in a way per-call timeouts don't
# catch.
PREEMPTION_FINISH_TIMEOUT_S = 20.0
# How long to wait, after requeueing a killed victim, for RMF to actually
# assign it a robot before logging a diagnostic. This never re-dispatches —
# the requeued task may correctly be waiting for its own fleet's only robot
# to finish the critical, and resubmitting here would risk a duplicate
# physical task.
REQUEUE_VERIFICATION_TIMEOUT_S = 10.0
# After the victim is killed and the robot is confirmed free, how long to
# wait for the critical's own (already-queued) booking to be auctioned onto
# it before falling back to a clean cancel + re-dispatch. RMF's dispatcher
# does not appear to automatically re-bid a task once its only capable
# robot frees up, so this window is expected to be exhausted often — the
# fallback exists so the critical still runs even when that's true, without
# ever leaving two live RMF tasks for one critical (unlike the old
# unconditional robot_task_request "direct assign", which always created a
# second task id and cleaned up the first with an unverified cancel).
CRITICAL_SELF_ASSIGN_TIMEOUT_S = 5.0
CRITICAL_SELF_ASSIGN_POLL_INTERVAL_S = 0.5
# Labels the preemption pipeline stamps on requests it creates. They must be
# stripped before a request is re-dispatched and mark tasks that must never be
# picked as preemption victims.
_PREEMPTION_LABEL_PREFIXES = (
    "forced_from=",
    "preempted_by=",
    "requeued_from=",
    "killed_for_critical=",
)

# Serializes victim selection and preemption so two concurrently dispatched
# critical tasks cannot kill two tasks (or the same task twice) for one robot.
_preemption_lock = asyncio.Lock()


def _task_state_status(task_state: Optional[mdl.TaskState]) -> Optional[str]:
    if task_state is None:
        return None
    return _status_text(getattr(task_state, "status", None))


async def _handle_critical_preemption(
    task_id: str,
    task_repo: TaskRepository,
    fleet_repo: FleetRepository,
    watch_window: float = PREEMPTION_WATCH_WINDOW_S,
):
    """Ensure the critical task `task_id` starts as soon as possible.

    Watches for up to `watch_window` seconds: if the critical task starts (or
    reaches a terminal state) on its own there is nothing to do; if a
    lower-priority task is (or becomes) active while the critical is still
    pending, that task is killed, the critical is force-assigned to the freed
    robot, and the killed task is re-dispatched from the start.
    This is best-effort: failures are logged but do not raise.
    """
    try:
        incoming_request = await task_repo.get_task_request(task_id)
        # The watcher is only spawned for critical requests, so never let a
        # missing stored request weaken the incoming priority below critical.
        incoming_priority = max(
            priority_value(incoming_request), CRITICAL_PRIORITY_VALUE
        )
        incoming_category = getattr(incoming_request, "category", None)

        deadline = time.monotonic() + watch_window
        while True:
            critical_status = _task_state_status(
                await task_repo.get_task_state(task_id)
            )
            if critical_status in ACTIVE_TASK_STATUSES:
                logger.info(
                    "critical watcher: %s already active (%s); no preemption needed",
                    task_id,
                    critical_status,
                )
                return
            if critical_status in TERMINAL_TASK_STATUSES:
                logger.info(
                    "critical watcher: %s reached terminal state (%s); standing down",
                    task_id,
                    critical_status,
                )
                return

            # Only victim selection and the kill itself need to be atomic
            # with respect to other concurrent critical watchers (so two
            # criticals cannot select the same active task, or both try to
            # kill work meant for one robot). Everything after a successful
            # kill is specific to this critical/victim pair and runs outside
            # the lock, so a slow RMF call there cannot block unrelated
            # preemptions — this is what previously let one hung call
            # silently freeze critical preemption fleet-wide for up to 120s.
            kill_result: Optional[Tuple[str, str, str, float]] = None
            async with _preemption_lock:
                # Re-read inside the lock: another watcher may have preempted
                # while we waited, changing which tasks are active.
                critical_status = _task_state_status(
                    await task_repo.get_task_state(task_id)
                )
                if (
                    critical_status in ACTIVE_TASK_STATUSES
                    or critical_status in TERMINAL_TASK_STATUSES
                ):
                    logger.info(
                        "critical watcher: %s settled (%s) while waiting for the "
                        "preemption lock; standing down",
                        task_id,
                        critical_status,
                    )
                    return
                candidates = await task_repo.query_task_states()
                victim = await _select_preemption_victim(
                    candidates,
                    task_id,
                    incoming_priority,
                    incoming_category,
                    task_repo,
                )
                if victim is not None:
                    kill_result = await _kill_victim_for_critical(
                        victim, task_id, task_repo, fleet_repo
                    )

            if kill_result is not None:
                victim_id, fleet_name, robot_name, interrupt_started_at = kill_result
                try:
                    await asyncio.wait_for(
                        _finish_preemption_after_kill(
                            victim_id,
                            fleet_name,
                            robot_name,
                            task_id,
                            interrupt_started_at,
                            task_repo,
                            fleet_repo,
                        ),
                        timeout=PREEMPTION_FINISH_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "critical watcher: finishing preemption of %s for %s "
                        "timed out after %ss",
                        victim_id,
                        task_id,
                        PREEMPTION_FINISH_TIMEOUT_S,
                    )
                return

            if time.monotonic() >= deadline:
                logger.info(
                    "critical watcher: no preemptable task appeared for %s within "
                    "%ss; leaving it to queue order",
                    task_id,
                    watch_window,
                )
                return
            await asyncio.sleep(PREEMPTION_POLL_INTERVAL_S)
    except Exception:
        logger.exception("critical watcher failed for %s", task_id)


async def _select_preemption_victim(
    candidates: List[mdl.TaskState],
    critical_task_id: str,
    incoming_priority: int,
    incoming_category: Optional[str],
    task_repo: TaskRepository,
) -> Optional[mdl.TaskState]:
    """Pick an active task the incoming critical is allowed to interrupt.

    Only interrupt active work, never queued or stale tasks; only work of
    strictly lower priority (a critical must not kill another critical);
    and only within the same task category, since fleets are function-
    specific (clean/patrol/deliver) — a critical clean must never kill a
    patrol on a fleet that could never have served the clean anyway.
    """
    for candidate in candidates:
        if candidate.booking.id == critical_task_id:
            continue
        if not _extract_assigned_robot_name(candidate):
            continue
        if _task_state_status(candidate) not in ACTIVE_TASK_STATUSES:
            continue
        victim_request = await task_repo.get_task_request(candidate.booking.id)
        if getattr(victim_request, "category", None) != incoming_category:
            continue
        victim_labels = list(getattr(victim_request, "labels", None) or [])
        if any(
            label.startswith(("forced_from=", "preempted_by="))
            for label in victim_labels
        ):
            continue
        if not can_preempt(
            incoming_priority, priority_value(victim_request, victim_labels)
        ):
            continue
        return candidate
    return None


async def _kill_victim_for_critical(
    victim: mdl.TaskState,
    critical_task_id: str,
    task_repo: TaskRepository,
    fleet_repo: FleetRepository,
) -> Optional[Tuple[str, str, str, float]]:
    """Hard-stop the victim so RMF frees its robot.

    Must be called while holding `_preemption_lock`. Returns
    (victim_id, fleet_name, robot_name, interrupt_started_at) on success, or
    None if the victim could not be killed (missing fleet/robot info, or the
    kill call itself failed or timed out) — the caller retries with a fresh
    victim selection on the next poll, bounded by the overall watch window.
    """
    victim_id = victim.booking.id
    robot_name = _extract_assigned_robot_name(victim)
    fleet_name = victim.assigned_to.group if victim.assigned_to else None
    if not fleet_name or not robot_name:
        logger.warning(
            "critical watcher: missing fleet or robot name for victim %s; "
            "aborting preemption for %s",
            victim_id,
            critical_task_id,
        )
        return None

    interrupt_started_at = time.monotonic()
    logger.info(
        "INTERRUPT BEGIN ts=%s robot_name=%s active_task_id=%s",
        _utc_now_iso(),
        robot_name,
        victim_id,
    )

    try:
        kill_req = mdl.TaskKillRequest(
            type="kill_task_request",
            task_id=victim_id,
            labels=[f"killed_for_critical={critical_task_id}"],
        )
        kill_response_json = await tasks_service().call(
            kill_req.model_dump_json(exclude_none=True),
            timeout=PREEMPTION_RMF_CALL_TIMEOUT_S,
        )
    except Exception:
        logger.exception(
            "critical watcher: kill call failed for %s (critical %s)",
            victim_id,
            critical_task_id,
        )
        return None
    if not _is_service_success(kill_response_json):
        logger.error(
            "critical watcher: kill failed for %s; will retry within the "
            "watch window for critical %s: %s",
            victim_id,
            critical_task_id,
            kill_response_json,
        )
        return None
    logger.info(
        "critical watcher: killed %s for critical %s",
        victim_id,
        critical_task_id,
    )
    return victim_id, fleet_name, robot_name, interrupt_started_at


async def _finish_preemption_after_kill(
    victim_id: str,
    fleet_name: str,
    robot_name: str,
    critical_task_id: str,
    interrupt_started_at: float,
    task_repo: TaskRepository,
    fleet_repo: FleetRepository,
) -> None:
    """Let the critical's own booking take the freed robot, falling back to
    a clean cancel + re-dispatch only if it doesn't; then re-dispatch the
    killed victim from the start so its work is not lost.

    Runs outside `_preemption_lock` (the victim is already dead and cannot
    be re-selected by another watcher) and is wrapped by the caller in a
    hard timeout, so a slow RMF call here cannot block other preemptions.
    """
    poll_stop_event = asyncio.Event()
    _spawn_background_task(
        _debug_poll_fleet_state(
            fleet_repo,
            task_repo,
            fleet_name,
            robot_name,
            victim_id,
            poll_stop_event,
        )
    )

    try:
        ready, status = await _wait_for_robot_stop(
            fleet_repo,
            fleet_name,
            robot_name,
            victim_id,
            timeout=5.0,
        )
        logger.info(
            "critical watcher: robot ready=%s status=%s after kill",
            ready,
            status,
        )

        self_assigned = await _wait_for_critical_self_assignment(
            critical_task_id, task_repo
        )
        if self_assigned:
            logger.info(
                "CRITICAL SELF-ASSIGNED ts=%s critical_task_id=%s "
                "interrupt_to_active_ms=%s",
                _utc_now_iso(),
                critical_task_id,
                int((time.monotonic() - interrupt_started_at) * 1000),
            )
        else:
            logger.info(
                "critical watcher: %s did not self-assign within %ss; "
                "falling back to cancel + re-dispatch",
                critical_task_id,
                CRITICAL_SELF_ASSIGN_TIMEOUT_S,
            )
            await _redispatch_stalled_critical(
                critical_task_id,
                fleet_name,
                task_repo,
                fleet_repo,
                interrupt_started_at,
            )
        # The victim was killed regardless of whether the critical actually
        # ended up running (on failure it stays queued at high priority), so
        # always bring the victim's work back.
        await _requeue_preempted_task(
            victim_id, critical_task_id, task_repo, fleet_repo
        )
    except Exception:
        logger.exception(
            "critical watcher: failed during preemption of %s for %s",
            victim_id,
            critical_task_id,
        )
    finally:
        poll_stop_event.set()


async def _wait_for_critical_self_assignment(
    critical_task_id: str,
    task_repo: TaskRepository,
    timeout: float = CRITICAL_SELF_ASSIGN_TIMEOUT_S,
    poll_interval: float = CRITICAL_SELF_ASSIGN_POLL_INTERVAL_S,
) -> bool:
    """Poll the critical's own (already-queued) booking to see if RMF's
    dispatcher auctions it onto the now-freed robot without any
    intervention from us. Returns True as soon as it goes active (or
    terminal, which would be unusual this early but is still "handled").
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _task_state_status(await task_repo.get_task_state(critical_task_id))
        if status in ACTIVE_TASK_STATUSES or status in TERMINAL_TASK_STATUSES:
            return True
        await asyncio.sleep(poll_interval)
    return False


async def _redispatch_stalled_critical(
    critical_task_id: str,
    fleet_name: str,
    task_repo: TaskRepository,
    fleet_repo: FleetRepository,
    interrupt_started_at: float,
) -> Optional[str]:
    """Fallback for when the critical's own booking does not get auctioned
    onto the freed robot on its own (RMF's dispatcher does not appear to
    retry a bid that had no capable robot at submission time).

    Cancels the stalled booking — verifying success, unlike the previous
    "direct assign + fire-and-forget cancel" approach — and only then
    re-submits the same request through the normal auction. If the cancel
    can't be confirmed, this aborts rather than risk a second live RMF task
    for the same critical: the original stays queued, visibly, instead of
    silently duplicating.
    """
    try:
        cancel_req = mdl.CancelTaskRequest(
            type="cancel_task_request",
            task_id=critical_task_id,
            labels=["critical_self_assign_timeout"],
        )
        cancel_response_json = await tasks_service().call(
            cancel_req.model_dump_json(exclude_none=True),
            timeout=PREEMPTION_RMF_CALL_TIMEOUT_S,
        )
    except Exception:
        logger.exception(
            "critical watcher: cancel call failed for stalled critical %s; "
            "leaving it queued rather than risk a duplicate task",
            critical_task_id,
        )
        return None
    if not _is_service_success(cancel_response_json):
        logger.error(
            "critical watcher: could not confirm cancellation of stalled "
            "critical %s; leaving it queued rather than risk a duplicate "
            "task: %s",
            critical_task_id,
            cancel_response_json,
        )
        return None
    logger.info(
        "critical watcher: confirmed cancel of stalled critical %s; "
        "re-dispatching through the normal auction",
        critical_task_id,
    )

    try:
        critical_request = await task_repo.get_task_request(critical_task_id)
        if not critical_request:
            logger.warning(
                "critical watcher: no stored request for %s; cannot re-dispatch",
                critical_task_id,
            )
            return None
        _apply_priority_labels(critical_request)
        labels = [
            label
            for label in (critical_request.labels or [])
            if not label.startswith(_PREEMPTION_LABEL_PREFIXES)
        ]
        # Reuses the same label the bridge's remap logic already looks for
        # (see rmf_demos_bridges status_service._find_successor_task_id) so
        # a digiBASE-submitted critical's work order follows this redispatch
        # without any bridge-side change.
        labels.append(f"forced_from={critical_task_id}")
        critical_request.labels = labels
        critical_request.fleet_name = fleet_name
        critical_request.unix_millis_request_time = int(time.time() * 1000)
        critical_request.unix_millis_earliest_start_time = None

        dispatch_request = mdl.DispatchTaskRequest(
            type="dispatch_task_request",
            request=critical_request,
        )
        # Re-enters post_dispatch_task, which will see this is still a
        # critical request and spawn a fresh watcher for it. That watcher
        # will find the robot already idle (nothing else to preempt) and
        # stand down as soon as this booking goes active — harmless.
        resp = await post_dispatch_task(dispatch_request, task_repo, fleet_repo)
        if isinstance(resp, RawJSONResponse):
            logger.error(
                "critical watcher: re-dispatch failed for stalled critical %s",
                critical_task_id,
            )
            return None
        redispatched_state = cast(mdl.TaskDispatchResponse1, resp.root).state
        redispatched_task_id = redispatched_state.booking.id
        logger.info(
            "CRITICAL RE-DISPATCH COMPLETED ts=%s original_task_id=%s "
            "new_task_id=%s interrupt_to_dispatch_ms=%s",
            _utc_now_iso(),
            critical_task_id,
            redispatched_task_id,
            int((time.monotonic() - interrupt_started_at) * 1000),
        )
        return redispatched_task_id
    except Exception:
        logger.exception(
            "critical watcher: failed to re-dispatch stalled critical %s",
            critical_task_id,
        )
        return None


async def _requeue_preempted_task(
    victim_id: str,
    critical_task_id: str,
    task_repo: TaskRepository,
    fleet_repo: FleetRepository,
) -> None:
    """Re-dispatch a killed victim from the start so its work is not lost."""
    try:
        victim_request = await task_repo.get_task_request(victim_id)
        if victim_request is None:
            logger.warning(
                "critical watcher: no stored request for killed %s; cannot requeue",
                victim_id,
            )
            return
        labels = [
            label
            for label in (victim_request.labels or [])
            if not label.startswith(_PREEMPTION_LABEL_PREFIXES)
        ]
        if any(label.startswith("scheduled_schedule_id=") for label in labels):
            # A schedule run: its next occurrence (or chaining) re-dispatches
            # it; requeueing here would double-book the schedule.
            logger.info(
                "critical watcher: not requeueing schedule run %s killed for %s",
                victim_id,
                critical_task_id,
            )
            return
        labels.append(f"requeued_from={victim_id}")
        victim_request.labels = labels
        victim_request.unix_millis_request_time = int(time.time() * 1000)
        victim_request.unix_millis_earliest_start_time = None
        dispatch_request = mdl.DispatchTaskRequest(
            type="dispatch_task_request",
            request=victim_request,
        )
        resp = await post_dispatch_task(dispatch_request, task_repo, fleet_repo)
        if isinstance(resp, RawJSONResponse):
            logger.error(
                "critical watcher: requeue dispatch failed for %s (killed for %s)",
                victim_id,
                critical_task_id,
            )
            return
        requeued_state = cast(mdl.TaskDispatchResponse1, resp.root).state
        logger.info(
            "critical watcher: requeued %s as %s after critical %s",
            victim_id,
            requeued_state.booking.id,
            critical_task_id,
        )
        # Fire-and-forget diagnostic: confirm RMF actually assigns a robot to
        # the requeued task. Deliberately does not re-dispatch on failure —
        # the requeue may correctly be waiting for its own fleet's only robot
        # to free up, and resubmitting here would risk a duplicate physical
        # task. It only makes a stuck requeue visible in the logs.
        _spawn_background_task(
            _verify_requeued_task_assignment(
                requeued_state.booking.id, victim_id, critical_task_id, task_repo
            )
        )
    except Exception:
        logger.exception(
            "critical watcher: failed to requeue %s after critical %s",
            victim_id,
            critical_task_id,
        )


async def _verify_requeued_task_assignment(
    requeued_task_id: str,
    victim_id: str,
    critical_task_id: str,
    task_repo: TaskRepository,
    timeout: float = REQUEUE_VERIFICATION_TIMEOUT_S,
    poll_interval: float = 1.0,
) -> None:
    """Best-effort diagnostic: log whether the requeued victim ever gets a
    robot. Never re-dispatches — see the call site for why."""
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await task_repo.get_task_state(requeued_task_id)
            status = _task_state_status(state)
            if status in ACTIVE_TASK_STATUSES or status in TERMINAL_TASK_STATUSES:
                logger.info(
                    "critical watcher: requeued %s (victim %s, critical %s) "
                    "reached status=%s",
                    requeued_task_id,
                    victim_id,
                    critical_task_id,
                    status,
                )
                return
            dispatch = getattr(state, "dispatch", None) if state else None
            dispatch_status = getattr(getattr(dispatch, "status", None), "value", None)
            if dispatch_status == "failed":
                logger.error(
                    "critical watcher: requeued %s (victim %s, critical %s) "
                    "was rejected by RMF's dispatcher; it may need manual "
                    "re-submission",
                    requeued_task_id,
                    victim_id,
                    critical_task_id,
                )
                return
            await asyncio.sleep(poll_interval)
        logger.warning(
            "critical watcher: requeued %s (victim %s, critical %s) is still "
            "unassigned after %ss; it may correctly be waiting for its "
            "fleet's own robot to free up — check GET /tasks/%s/state "
            "(dispatch.status/assignment) if this persists",
            requeued_task_id,
            victim_id,
            critical_task_id,
            timeout,
            requeued_task_id,
        )
    except Exception:
        logger.exception(
            "critical watcher: failed to verify requeue %s (victim %s, " "critical %s)",
            requeued_task_id,
            victim_id,
            critical_task_id,
        )


@router.post(
    "/robot_task",
    response_model=mdl.RobotTaskResponse,
    responses={400: {"model": mdl.RobotTaskResponse}},
)
async def post_robot_task(
    request: mdl.RobotTaskRequest = Body(...),
    task_repo: TaskRepository = Depends(task_repo_dep),
):
    resp = mdl.RobotTaskResponse.model_validate_json(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
    if not resp.root.root.success:
        return RawJSONResponse(resp.model_dump_json(), 400)
    await task_repo.save_task_state(
        cast(mdl.TaskDispatchResponse1, resp.root.root).state
    )
    return resp


@router.post("/interrupt_task", response_model=mdl.TaskInterruptionResponse)
async def post_interrupt_task(
    request: mdl.TaskInterruptionRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/kill_task", response_model=mdl.TaskKillResponse)
async def post_kill_task(
    request: mdl.TaskKillRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/resume_task", response_model=mdl.TaskResumeResponse)
async def post_resume_task(
    request: mdl.TaskResumeRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/rewind_task", response_model=mdl.TaskRewindResponse)
async def post_rewind_task(
    request: mdl.TaskRewindRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/skip_phase", response_model=mdl.SkipPhaseResponse)
async def post_skip_phase(
    request: mdl.TaskPhaseSkipRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/task_discovery", response_model=mdl.TaskDiscovery)
async def post_task_discovery(
    request: mdl.TaskDiscoveryRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )


@router.post("/undo_skip_phase", response_model=mdl.UndoPhaseSkipResponse)
async def post_undo_skip_phase(
    request: mdl.UndoPhaseSkipRequest = Body(...),
):
    return RawJSONResponse(
        await tasks_service().call(request.model_dump_json(exclude_none=True))
    )
