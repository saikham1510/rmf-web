import asyncio
from datetime import datetime
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
from api_server.repositories import TaskRepository, task_repo_dep
from api_server.response import RawJSONResponse
from api_server.rmf_io import task_events, tasks_service

router = FastIORouter(tags=["Tasks"])

CRITICAL_PRIORITY_VALUE = 100


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
):
    if _has_critical_label(request.request.labels):
        _ensure_critical_priority(request.request)
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
    # If the original request had a critical/preempt label, spawn a watcher to
    # interrupt the awarded robot's active task and resume it after completion.
    try:
        if _has_critical_label(request.request.labels):
            # spawn background watcher (fire-and-forget)
            _spawn_background_task(
                _handle_critical_preemption(
                    task_state.booking.id,
                    task_repo,
                    interrupt_immediately=True,
                )
            )
    except Exception:
        logger.exception("failed to spawn critical preemption watcher")
    return resp


def _has_critical_label(labels: Optional[List[str]]) -> bool:
    if not labels:
        return False
    for label in labels:
        if label.lower() in ("critical=true", "preempt=interrupt"):
            return True
    return False


def _ensure_critical_priority(task_request: mdl.TaskRequest) -> None:
    priority = getattr(task_request, "priority", None)
    current_value = None
    if isinstance(priority, dict):
        current_value = priority.get("value")
    if (
        not isinstance(current_value, (int, float))
        or current_value < CRITICAL_PRIORITY_VALUE
    ):
        task_request.priority = {"type": "binary", "value": CRITICAL_PRIORITY_VALUE}


def _spawn_background_task(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "critical watcher: no running loop available; skipping background task"
        )
        return None
    return loop.create_task(coro)


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
    interrupt_immediately: bool = False,
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

            # subscribe to task states for this task
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
        logger.info(
            "critical watcher: pausing task %s for critical %s", paused_id, task_id
        )

        hard_preempted = False

        # send interrupt request
        try:
            intr_req = mdl.TaskInterruptionRequest(
                type="interrupt_task_request",
                task_id=paused_id,
                labels=[f"preempted_by={task_id}"],
            )
            intr_resp = mdl.TaskInterruptionResponse.model_validate_json(
                await tasks_service().call(intr_req.model_dump_json(exclude_none=True))
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
                    "critical watcher: interrupted %s with token %s", paused_id, token
                )
        except Exception:
            logger.exception("critical watcher: failed to interrupt %s", paused_id)

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
                hard_preempted = True
                await task_repo.delete_interruption_token(paused_id)
                logger.info(
                    "critical watcher: killed %s for critical %s", paused_id, task_id
                )
            except Exception:
                logger.warning(
                    "critical watcher: failed to kill %s for critical %s",
                    paused_id,
                    task_id,
                )

        # If we are preempting immediately, force-assign the critical task to
        # the same robot so it starts right away, then cancel the queued original.
        if interrupt_immediately and paused_task.assigned_to:
            try:
                critical_request = await task_repo.get_task_request(task_id)
                if critical_request:
                    if _has_critical_label(critical_request.labels):
                        _ensure_critical_priority(critical_request)
                    forced_labels = list(critical_request.labels or [])
                    forced_labels.append(f"forced_from={task_id}")
                    critical_request.labels = forced_labels
                    robot_req = mdl.RobotTaskRequest(
                        type="robot_task_request",
                        robot=paused_task.assigned_to.name,
                        fleet=paused_task.assigned_to.group,
                        request=critical_request,
                    )
                    forced_resp = mdl.RobotTaskResponse.model_validate_json(
                        await tasks_service().call(
                            robot_req.model_dump_json(exclude_none=True)
                        )
                    )
                    if forced_resp.root.root.success:
                        forced_state = cast(
                            mdl.TaskDispatchResponse1, forced_resp.root.root
                        ).state
                        critical_task_id = forced_state.booking.id
                        await task_repo.save_task_request(
                            critical_task_id, critical_request
                        )
                        await task_repo.save_task_state(forced_state)
                        logger.info(
                            "critical watcher: forced critical task %s on %s",
                            critical_task_id,
                            paused_task.assigned_to.name,
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
                    "critical watcher: failed to force-assign critical %s", task_id
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
            await asyncio.wait_for(comp_evt.wait(), timeout=3600)
        except asyncio.TimeoutError:
            logger.info(
                "critical watcher: timed out waiting for critical task %s to complete",
                critical_task_id,
            )
        finally:
            sub2.dispose()

        # attempt resume if we saved a token
        if not hard_preempted:
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

    # Prefer interrupting the robot's currently active work instead of queued items.
    active_candidates: list[mdl.TaskState] = []
    fallback_candidates: list[mdl.TaskState] = []
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
        if status_value != "queued":
            fallback_candidates.append(c)

    if active_candidates:
        return active_candidates[0]
    if fallback_candidates:
        return fallback_candidates[0]
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
