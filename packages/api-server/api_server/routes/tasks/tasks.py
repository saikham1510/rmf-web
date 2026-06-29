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

router = FastIORouter(tags=["Tasks"])

NORMAL_PRIORITY_VALUE = 0
URGENT_PRIORITY_VALUE = 1
CRITICAL_PRIORITY_VALUE = 2

PRIORITY_LABEL_VALUES = {
    "normal": NORMAL_PRIORITY_VALUE,
    "urgent": URGENT_PRIORITY_VALUE,
    "critical": CRITICAL_PRIORITY_VALUE,
}


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
    # If the original request is critical, spawn a watcher to interrupt the
    # awarded robot's active task and resume it after completion.
    try:
        if is_critical_request:
            # spawn background watcher (fire-and-forget)
            _spawn_background_task(
                _handle_critical_preemption(
                    task_state.booking.id,
                    task_repo,
                    fleet_repo,
                    interrupt_immediately=True,
                )
            )
    except Exception:
        logger.exception("failed to spawn critical preemption watcher")
    return resp


def _task_priority_label(labels: Optional[List[str]]) -> Optional[str]:
    if not labels:
        return None

    priority_label = None
    for label in labels:
        normalized = label.lower().strip()
        if normalized in ("critical=true", "preempt=interrupt", "priority=critical"):
            return "critical"
        if normalized in ("urgent=true", "priority=urgent"):
            priority_label = priority_label or "urgent"
            continue
        if normalized in ("normal=true", "priority=normal"):
            priority_label = priority_label or "normal"
            continue
        if normalized == "critical":
            return "critical"
        if normalized == "urgent":
            priority_label = priority_label or "urgent"
            continue
        if normalized == "normal":
            priority_label = priority_label or "normal"
    return priority_label


def _apply_priority_labels(task_request: mdl.TaskRequest) -> Optional[str]:
    priority_label = _task_priority_label(task_request.labels)
    if priority_label is None:
        return None

    task_request.priority = {
        "type": "binary",
        "value": PRIORITY_LABEL_VALUES[priority_label],
    }
    return priority_label


def _is_critical_task_request(task_request: mdl.TaskRequest) -> bool:
    priority = getattr(task_request, "priority", None)
    if isinstance(priority, dict) and priority.get("value") == CRITICAL_PRIORITY_VALUE:
        return True
    return _task_priority_label(getattr(task_request, "labels", None)) == "critical"


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


def _is_interrupt_success(interrupt_response_json: str) -> bool:
    try:
        interrupt_response = json.loads(interrupt_response_json)
    except Exception:
        return False

    if isinstance(interrupt_response, dict):
        success = interrupt_response.get("success", False)
        if hasattr(success, "root"):
            return bool(success.root)
        return bool(success)

    return False


def _can_dispatch_critical(
    robot_status: object | None,
    robot_task_id: Optional[str],
    paused_task_id: str,
) -> bool:
    if robot_task_id == paused_task_id:
        return False

    return _status_text(robot_status) in {
        "idle",
        "paused",
        "interrupted",
        "cancelled",
    }


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


async def _interrupt_watchdog(
    robot_name: Optional[str],
    paused_task_id: str,
    ready_event: asyncio.Event,
    timeout: float = 10.0,
) -> None:
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "interrupt watchdog: robot=%s task_id=%s still not ready after %ss",
            robot_name,
            paused_task_id,
            timeout,
        )


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


async def _handle_critical_preemption(
    task_id: str,
    task_repo: TaskRepository,
    fleet_repo: FleetRepository,
    interrupt_immediately: bool = False,
    timeout: float = 10.0,
):
    """Watch for which robot wins the bid, interrupt that robot's active task,
    then resume it after the critical task completes.
    This is best-effort: failures are logged but do not raise.
    """
    try:
        assigned_robot: Optional[str] = None
        terminal_statuses = {"completed", "failed", "canceled", "killed"}
        preferred_active_statuses = {"underway", "blocked", "delayed", "standby"}
        paused_task: Optional[mdl.TaskState] = None
        critical_task_id = task_id
        # When we immediately preempt, the paused task is *killed* (not merely
        # paused). A killed task must never be resumed afterwards, otherwise the
        # robot drives back to finish work it was meant to abandon (e.g. resuming
        # a patrol after an end-of-shift return-to-charger preemption).
        paused_task_killed = False

        if interrupt_immediately:
            candidates = await task_repo.query_task_states()
            paused_task = _select_paused_task(
                candidates,
                task_id,
                terminal_statuses,
                preferred_active_statuses,
                assigned_robot=None,
            )
            if paused_task and paused_task.assigned_to:
                assigned_robot = paused_task.assigned_to.name
        else:
            evt = asyncio.Event()

            def on_state(s: mdl.TaskState):
                try:
                    nonlocal assigned_robot
                    if s.booking.id != task_id:
                        return
                    robot = _extract_assigned_robot_name(s)
                    if not robot:
                        return
                    # Prefer states where dispatch has selected/dispatched, but allow
                    # fallback to assigned_to for scheduled tasks where dispatch is null.
                    if s.dispatch:
                        status = getattr(s.dispatch, "status", None)
                        status_value = getattr(status, "value", None)
                        if status_value not in ("selected", "dispatched"):
                            return
                    assigned_robot = robot
                    evt.set()
                except Exception:
                    logger.exception("error in critical watcher on_state")

            # subscribe to task states for this tas timeout=timeoutk
            sub = task_events.task_states.pipe(
                rxops.filter(lambda x: x.booking.id == task_id)
            ).subscribe(on_state)

            # The dispatch response may already be persisted before this watcher
            # starts, so seed the watcher with the current stored state to avoid
            # missing the initial assignment event.
            try:
                current_state = await task_repo.get_task_state(task_id)
                if current_state is not None:
                    on_state(current_state)
            except Exception:
                logger.exception(
                    "critical watcher: failed to read current state for %s", task_id
                )

            try:
                await asyncio.wait_for(evt.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.info(
                    "critical watcher: timed out waiting for assignment for %s",
                    task_id,
                )
                sub.dispose()
                return
            sub.dispose()

        if not paused_task:
            if not assigned_robot:
                logger.info("critical watcher: no assigned robot found for %s", task_id)
                return

            logger.info(
                "critical watcher: assigned robot %s for %s", assigned_robot, task_id
            )

            # find active non-critical task on that robot
            candidates = await task_repo.query_task_states()
            paused_task = _select_paused_task(
                candidates,
                task_id,
                terminal_statuses,
                preferred_active_statuses,
                assigned_robot=assigned_robot,
            )

        if not paused_task:
            logger.info(
                "critical watcher: no paused task found for critical %s", task_id
            )
            return

        paused_id = paused_task.booking.id
        robot_name = (
            paused_task.assigned_to.name if paused_task.assigned_to else assigned_robot
        )
        fleet_name = paused_task.assigned_to.group if paused_task.assigned_to else None
        logger.info(
            "critical watcher: pausing task %s for critical %s", paused_id, task_id
        )

        interrupt_started_at = time.monotonic()
        logger.info(
            "INTERRUPT BEGIN ts=%s robot_name=%s active_task_id=%s",
            _utc_now_iso(),
            robot_name,
            paused_id,
        )

        poll_stop_event = asyncio.Event()
        _spawn_background_task(
            _debug_poll_fleet_state(
                fleet_repo,
                task_repo,
                fleet_name,
                robot_name,
                paused_id,
                poll_stop_event,
            )
        )

        # send interrupt request
        try:
            interrupt_ready_event = asyncio.Event()
            _spawn_background_task(
                _interrupt_watchdog(robot_name, paused_id, interrupt_ready_event)
            )
            try:
                if not fleet_name or not robot_name:
                    logger.warning(
                        "critical watcher: missing fleet or robot name for %s; aborting preemptive dispatch",
                        paused_id,
                    )
                    return

                before_interrupt_fleet_state = await fleet_repo.get_fleet_state(
                    fleet_name
                )
                logger.info(
                    "ROBOT SNAPSHOT stage=before_interrupt ts=%s robot=%s status=%s task_id=%s %s",
                    _utc_now_iso(),
                    robot_name,
                    None
                    if before_interrupt_fleet_state is None
                    or before_interrupt_fleet_state.robots is None
                    or before_interrupt_fleet_state.robots.get(robot_name) is None
                    else _status_text(
                        before_interrupt_fleet_state.robots[robot_name].status
                    ),
                    None
                    if before_interrupt_fleet_state is None
                    or before_interrupt_fleet_state.robots is None
                    or before_interrupt_fleet_state.robots.get(robot_name) is None
                    else before_interrupt_fleet_state.robots[robot_name].task_id,
                    _robot_state_debug(before_interrupt_fleet_state, robot_name),
                )

                intr_req = mdl.TaskInterruptionRequest(
                    type="interrupt_task_request",
                    task_id=paused_id,
                    labels=[f"preempted_by={task_id}"],
                )
                interrupt_request_json = intr_req.model_dump_json(exclude_none=True)
                interrupt_response_json = await tasks_service().call(
                    interrupt_request_json
                )
                interrupt_request_id = None
                try:
                    parsed_interrupt_response = json.loads(interrupt_response_json)
                    if isinstance(parsed_interrupt_response, dict):
                        interrupt_request_id = parsed_interrupt_response.get(
                            "request_id"
                        )
                except Exception:
                    parsed_interrupt_response = None
                logger.info(
                    "INTERRUPT RAW RESPONSE ts=%s request_id=%s body=%s",
                    _utc_now_iso(),
                    interrupt_request_id,
                    interrupt_response_json,
                )
                if not _is_interrupt_success(interrupt_response_json):
                    logger.error(
                        "critical watcher: interrupt failed for %s; aborting critical dispatch",
                        paused_id,
                    )
                    return
                intr_resp = mdl.TaskInterruptionResponse.model_validate_json(
                    interrupt_response_json
                )
                logger.info(
                    "INTERRUPT VERIFIED ts=%s task_id=%s robot_name=%s request_id=%s parsed_response=%s",
                    _utc_now_iso(),
                    paused_id,
                    robot_name,
                    interrupt_request_id,
                    parsed_interrupt_response,
                )
                # on success, save token
                root = intr_resp.root
                token = None
                try:
                    token = root.root.token
                except Exception:
                    token = None
                if token:
                    await task_repo.save_interruption_token(paused_id, token)
                    logger.info(
                        "critical watcher: interrupted %s with token %s",
                        paused_id,
                        token,
                    )

                # For immediate preemption, hard stop the paused task so RMF frees the robot.
                if interrupt_immediately:
                    try:
                        kill_req = mdl.TaskKillRequest(
                            type="kill_task_request",
                            task_id=paused_id,
                            labels=[f"killed_for_critical={task_id}"],
                        )
                        await tasks_service().call(
                            kill_req.model_dump_json(exclude_none=True),
                            timeout=5,
                        )
                        paused_task_killed = True
                        logger.info(
                            "critical watcher: killed %s for critical %s",
                            paused_id,
                            task_id,
                        )
                        ready, status = await _wait_for_robot_stop(
                            fleet_repo,
                            fleet_name,
                            robot_name,
                            paused_id,
                            timeout=5.0,
                        )

                        logger.info(
                            "critical watcher: robot ready=%s status=%s after kill",
                            ready,
                            status,
                        )

                    except Exception:
                        logger.warning(
                            "critical watcher: failed to kill %s for critical %s",
                            paused_id,
                            task_id,
                        )

                    after_interrupt_fleet_state = await fleet_repo.get_fleet_state(
                        fleet_name
                    )
                    logger.info(
                        "ROBOT SNAPSHOT stage=after_interrupt ts=%s robot=%s status=%s task_id=%s %s",
                        _utc_now_iso(),
                        robot_name,
                        None
                        if after_interrupt_fleet_state is None
                        or after_interrupt_fleet_state.robots is None
                        or after_interrupt_fleet_state.robots.get(robot_name) is None
                        else _status_text(
                            after_interrupt_fleet_state.robots[robot_name].status
                        ),
                        None
                        if after_interrupt_fleet_state is None
                        or after_interrupt_fleet_state.robots is None
                        or after_interrupt_fleet_state.robots.get(robot_name) is None
                        else after_interrupt_fleet_state.robots[robot_name].task_id,
                        _robot_state_debug(after_interrupt_fleet_state, robot_name),
                    )

                    logger.info(
                        "PREEMPT PIPELINE TIMING robot=%s interrupt_to_dispatch_ms=%s",
                        robot_name,
                        int((time.monotonic() - interrupt_started_at) * 1000),
                    )

                # If we are preempting immediately, force-assign the critical task to
                # the same robot so it starts right away, then cancel the queued original.
                if interrupt_immediately and paused_task.assigned_to:
                    try:
                        critical_dispatch_started_at = time.monotonic()
                        logger.info(
                            "CRITICAL DISPATCH BEGIN ts=%s robot_name=%s critical_task_id=%s paused_task_id=%s gap_since_interrupt_ms=%s",
                            _utc_now_iso(),
                            robot_name,
                            task_id,
                            paused_id,
                            int(
                                (critical_dispatch_started_at - interrupt_started_at)
                                * 1000
                            ),
                        )
                        critical_request = await task_repo.get_task_request(task_id)
                        if critical_request:
                            _apply_priority_labels(critical_request)
                            forced_labels = list(critical_request.labels or [])
                            forced_labels.append(f"forced_from={task_id}")
                            critical_request.labels = forced_labels
                            latest_fleet_state = await fleet_repo.get_fleet_state(
                                fleet_name
                            )
                            if latest_fleet_state and latest_fleet_state.robots:
                                latest_robot = latest_fleet_state.robots.get(robot_name)
                                if latest_robot and latest_robot.name:
                                    robot_name = latest_robot.name
                            robot_req = mdl.RobotTaskRequest(
                                type="robot_task_request",
                                robot=robot_name,
                                fleet=fleet_name,
                                request=critical_request,
                            )
                            forced_resp: Optional[mdl.RobotTaskResponse] = None
                            for _ in range(2):
                                forced_resp = mdl.RobotTaskResponse.model_validate_json(
                                    await tasks_service().call(
                                        robot_req.model_dump_json(exclude_none=True)
                                    )
                                )
                                if forced_resp.root.root.success:
                                    break
                                await asyncio.sleep(0.3)

                            if forced_resp and forced_resp.root.root.success:
                                forced_state = cast(
                                    mdl.TaskDispatchResponse1, forced_resp.root.root
                                ).state
                                critical_task_id = forced_state.booking.id
                                await task_repo.save_task_request(
                                    critical_task_id, critical_request
                                )
                                await task_repo.save_task_state(forced_state)
                                logger.info(
                                    "CRITICAL DISPATCH COMPLETED ts=%s critical_task_id=%s robot_name=%s",
                                    _utc_now_iso(),
                                    critical_task_id,
                                    robot_name,
                                )
                                logger.info(
                                    "CRITICAL PREEMPTION SUMMARY task_id=%s robot_name=%s interrupt_to_dispatch_ms=%s",
                                    task_id,
                                    robot_name,
                                    int(
                                        (time.monotonic() - interrupt_started_at) * 1000
                                    ),
                                )
                                poll_stop_event.set()
                                logger.info(
                                    "critical watcher: forced critical task %s on %s",
                                    critical_task_id,
                                    robot_name,
                                )
                                try:
                                    cancel_req = mdl.CancelTaskRequest(
                                        type="cancel_task_request",
                                        task_id=task_id,
                                        labels=[f"replaced_by={critical_task_id}"],
                                    )
                                    await tasks_service().call(
                                        cancel_req.model_dump_json(exclude_none=True),
                                        timeout=5,
                                    )
                                    logger.info(
                                        "critical watcher: canceled queued critical %s",
                                        task_id,
                                    )
                                except Exception:
                                    logger.warning(
                                        "critical watcher: cancel cleanup failed for queued critical %s",
                                        task_id,
                                    )
                            else:
                                logger.warning(
                                    "critical watcher: forced dispatch rejected for critical %s",
                                    task_id,
                                )
                    except Exception:
                        logger.exception(
                            "critical watcher: failed to force-assign critical %s",
                            task_id,
                        )
            finally:
                interrupt_ready_event.set()

        except Exception:
            logger.exception(
                "critical watcher: failed during interrupt preemption for %s",
                paused_id,
            )

        # wait for critical task to complete
        comp_evt = asyncio.Event()

        def on_done(s: mdl.TaskState):
            try:
                if s.booking.id != critical_task_id:
                    return
                st = getattr(s, "status", None)
                if st and getattr(st, "value", None) in terminal_statuses:
                    comp_evt.set()
            except Exception:
                logger.exception("error in critical watcher on_done")

        sub2 = task_events.task_states.pipe(
            rxops.filter(lambda x: x.booking.id == critical_task_id)
        ).subscribe(on_done)

        try:
            current_state = await task_repo.get_task_state(critical_task_id)
            if current_state is not None:
                on_done(current_state)
        except Exception:
            logger.exception(
                "critical watcher: failed to read current completion state for %s",
                critical_task_id,
            )

        try:
            await asyncio.wait_for(comp_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.info(
                "critical watcher: timed out waiting for critical task %s to complete",
                critical_task_id,
            )
        finally:
            sub2.dispose()

        # attempt resume if we saved a token — but never resume a task we killed.
        # Immediate preemption (e.g. end-of-shift return-to-charger) kills the
        # paused task; resuming it would send the robot back to abandon work.
        if paused_task_killed:
            logger.info(
                "critical watcher: not resuming %s — it was killed for critical %s",
                paused_id,
                task_id,
            )
            # Clean up any stale interruption token so it cannot be reused later.
            try:
                await task_repo.delete_interruption_token(paused_id)
            except Exception:
                logger.exception(
                    "critical watcher: failed to clear token for killed %s", paused_id
                )
        else:
            try:
                token = await task_repo.get_interruption_token(paused_id)
                if token:
                    resume_req = mdl.TaskResumeRequest(
                        type="resume_task_request",
                        task_id=paused_id,
                        token=token,
                        labels=[f"resumed_after={task_id}"],
                    )
                    await tasks_service().call(
                        resume_req.model_dump_json(exclude_none=True)
                    )
                    await task_repo.delete_interruption_token(paused_id)
                    logger.info(
                        "critical watcher: resumed %s after %s", paused_id, task_id
                    )
            except Exception:
                logger.exception(
                    "critical watcher: failed to resume %s after %s", paused_id, task_id
                )

    except Exception:
        logger.exception("critical watcher failed for %s", task_id)


def _select_paused_task(
    candidates: List[mdl.TaskState],
    critical_task_id: str,
    terminal_statuses: set[str],
    preferred_active_statuses: set[str],
    assigned_robot: Optional[str],
) -> Optional[mdl.TaskState]:
    def _status_value(task_state: mdl.TaskState) -> Optional[str]:
        status = getattr(task_state, "status", None)
        return getattr(status, "value", None)

    # Only interrupt active work. Avoid queued or stale tasks.
    active_candidates: list[mdl.TaskState] = []
    for c in candidates:
        if c.booking.id == critical_task_id:
            continue
        robot = _extract_assigned_robot_name(c)
        if not robot:
            continue
        if assigned_robot and robot != assigned_robot:
            continue
        status_value = _status_value(c)
        if not status_value or status_value in terminal_statuses:
            continue
        if status_value in preferred_active_statuses:
            active_candidates.append(c)
            continue

    if active_candidates:
        return active_candidates[0]
    return None


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
