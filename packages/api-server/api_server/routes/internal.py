# NOTE: This will eventually replace `gateway.py``
import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api_server import models as mdl
from api_server.logger import logger as base_logger
from api_server.models import tortoise_models as ttm
from api_server.repositories import AlertRepository, FleetRepository, TaskRepository
from api_server.rmf_io import alert_events, fleet_events, task_events

from .tasks import scheduled_tasks as scheduled_tasks_route

router = APIRouter(tags=["_internal"])
logger = base_logger.getChild("RmfGatewayApp")
user: mdl.User = mdl.User(username="__rmf_internal__", is_admin=True)
task_repo = TaskRepository(user)
alert_repo = AlertRepository(user, task_repo)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


def _env_category_allowlist() -> set[str]:
    # Default behavior chains recurring task categories that are typically
    # schedule-driven. Delivery is intentionally excluded by default because
    # deployments often treat it as ad-hoc demand work.
    raw = os.getenv("RMF_CHAIN_ALLOWED_CATEGORIES", "patrol,clean,loop,compose")
    result = {x.strip() for x in raw.split(",") if x.strip()}
    return result or {"patrol", "clean", "loop", "compose"}


# Scheduled task chaining is always enabled; the allow-list still controls
# which categories are eligible for automatic chaining.
CHAIN_ALLOWED_CATEGORIES = _env_category_allowlist()

# Log chaining mode at import so operators can confirm behavior.
logger.info(
    "scheduled task chaining enabled categories=%s (env RMF_CHAIN_ALLOWED_CATEGORIES)",
    sorted(CHAIN_ALLOWED_CATEGORIES),
)


def log_phase_has_error(phase: mdl.Phases) -> bool:
    if phase.log:
        for log in phase.log:
            if log.tier == mdl.Tier.error:
                return True
    if phase.events:
        for _, event_logs in phase.events.items():
            for event_log in event_logs:
                if event_log.tier == mdl.Tier.error:
                    return True
    return False


def task_log_has_error(task_log: mdl.TaskEventLog) -> bool:
    if task_log.log:
        for log in task_log.log:
            if log.tier == mdl.Tier.error:
                return True

    if task_log.phases:
        for _, phase in task_log.phases.items():
            if log_phase_has_error(phase):
                return True
    return False


async def process_msg(msg: Dict[str, Any], fleet_repo: FleetRepository) -> None:
    if "type" not in msg:
        logger.warning("Ignoring message (missing 'type'): %s", msg)
        raise ValueError("missing 'type' field in message")
    payload_type: str = msg["type"]
    if not isinstance(payload_type, str):
        logger.warning("error processing message, 'type' must be a string: %s", msg)
        raise ValueError("'type' field must be a string")
    logger.debug(msg)

    if payload_type == "task_state_update":
        task_state = mdl.TaskState(**msg["data"])
        # RULE: Convert RMF input ONCE at ingestion boundary and persist
        logger.info(
            "RMF task_state_update received booking_id=%s status=%s",
            task_state.booking.id,
            task_state.status,
        )
        await task_repo.save_task_state(task_state)
        task_events.task_states.on_next(task_state)

        if task_state.status in scheduled_tasks_route.ACTIVE_SCHEDULED_TASK_STATUSES:
            try:
                labels = task_state.booking.labels or []
                if not labels:
                    req = await task_repo.get_task_request(task_state.booking.id)
                    if req and req.labels:
                        labels = req.labels
                sched_label = next(
                    (
                        x
                        for x in labels
                        if isinstance(x, str) and x.startswith("scheduled_schedule_id=")
                    ),
                    None,
                )
                if sched_label:
                    _, _, id_str = sched_label.partition("=")
                    schedule_id = int(id_str)
                    schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                        _id=schedule_id
                    ).select_related("scheduled_task")
                    if schedule_row and schedule_row.scheduled_task:
                        await scheduled_tasks_route.cancel_active_scheduled_task_if_overdue(
                            schedule_row,
                            task_state,
                            task_repo,
                            datetime.now(timezone.utc),
                        )
            except Exception:
                logger.exception(
                    "chain: failed to evaluate overdue active scheduled task"
                )

        if task_state.status == mdl.TaskStatus.completed:
            alert = await alert_repo.create_alert(task_state.booking.id, "task")
            if alert is not None:
                alert_events.alerts.on_next(alert)

            # Optional chaining: enqueue another scheduled task run on completion
            # while the schedule window remains valid.
            try:
                labels = task_state.booking.labels or []
                logger.info(
                    "chain: completion hook booking_id=%s labels=%s",
                    task_state.booking.id,
                    labels,
                )
                # Fallback: if RMF did not echo labels into booking, use the
                # stored request (saved when we dispatched the task).
                if not labels:
                    try:
                        req = await task_repo.get_task_request(task_state.booking.id)
                        if req and req.labels:
                            labels = req.labels
                    except Exception:
                        logger.exception(
                            "chain: failed to load saved task request for labels"
                        )
                sched_label = next(
                    (
                        x
                        for x in labels
                        if isinstance(x, str) and x.startswith("scheduled_schedule_id=")
                    ),
                    None,
                )
                if sched_label:
                    _, _, id_str = sched_label.partition("=")
                    schedule_id = int(id_str)
                    schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                        _id=schedule_id
                    ).select_related("scheduled_task")
                    if schedule_row and schedule_row.scheduled_task:
                        await scheduled_tasks_route.try_dispatch_chained_schedule_run(
                            schedule_row,
                            task_repo,
                            completed_task_id=task_state.booking.id,
                            allowed_categories=CHAIN_ALLOWED_CATEGORIES,
                        )
            except Exception:
                logger.exception("chain: failed to dispatch chained scheduled task")

    elif payload_type == "task_log_update":
        task_log = mdl.TaskEventLog(**msg["data"])
        await task_repo.save_task_log(task_log)
        task_events.task_event_logs.on_next(task_log)

        if task_log_has_error(task_log):
            alert = await alert_repo.create_alert(task_log.task_id, "task")
            if alert is not None:
                alert_events.alerts.on_next(alert)

    elif payload_type == "fleet_state_update":
        fleet_state = mdl.FleetState(**msg["data"])
        await fleet_repo.save_fleet_state(fleet_state)
        fleet_events.fleet_states.on_next(fleet_state)

    elif payload_type == "fleet_log_update":
        fleet_log = mdl.FleetLog(**msg["data"])
        await fleet_repo.save_fleet_log(fleet_log)
        fleet_events.fleet_logs.on_next(fleet_log)


@router.websocket("")
async def rmf_gateway(websocket: WebSocket):
    await websocket.accept()
    fleet_repo = FleetRepository(user)
    try:
        while True:
            try:
                msg: Dict[str, Any] = await websocket.receive_json()
            except WebSocketDisconnect as e:
                # Client disconnected; exit gracefully without sending on closed socket
                logger.warning(
                    "Gateway websocket disconnected: code=%s reason=%s",
                    e.code,
                    getattr(e, "reason", None),
                )
                break
            except Exception as e:
                # Could not parse JSON — log and attempt to notify client if still open
                logger.warning("Failed to receive/parse websocket message: %s", e)
                try:
                    await websocket.send_json(
                        {"type": "error", "message": "invalid json"}
                    )
                except Exception:
                    # Socket likely already closing/closed; suppress to avoid ASGI error
                    logger.debug("Websocket likely closed; skipping error send")
                continue

            try:
                await process_msg(msg, fleet_repo)
            except ValueError as e:
                # Malformed payload — log and notify client
                logger.warning("Malformed gateway message: %s error=%s", msg, e)
                try:
                    await websocket.send_json({"type": "error", "message": str(e)})
                except Exception:
                    logger.debug("Websocket likely closed; skipping error send")
                continue
            except Exception:
                logger.exception("Unexpected error processing gateway message")
                try:
                    await websocket.send_json(
                        {"type": "error", "message": "internal server error"}
                    )
                except Exception:
                    logger.debug("Websocket likely closed; skipping error send")
                continue
    except WebSocketDisconnect:
        pass
