from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from api_server import models as mdl
from api_server.models import tortoise_models as ttm
from api_server.routes import internal
from api_server.test import AppFixture, make_task_state


class TestInternalRoute(AppFixture):
    def test_completed_task_triggers_scheduled_task_chain_hook(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp_1", "wp_2"],
                        "rounds": 3,
                    },
                },
                created_by="test",
            )
            schedule = await ttm.ScheduledTaskSchedule.create(
                scheduled_task=task,
                period=ttm.ScheduledTaskSchedule.Period.Day,
                start_from=datetime.now(timezone.utc),
                dispatched=True,
            )
            await schedule.fetch_related("scheduled_task")
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)

        task_state = make_task_state(
            task_id="completed_task_1",
            labels=[f"scheduled_schedule_id={schedule_id}"],
        )
        task_state.status = mdl.TaskStatus.completed

        chain_spy = AsyncMock(return_value=True)

        with patch.object(
            internal.task_repo, "save_task_state", new=AsyncMock()
        ), patch.object(
            internal.alert_repo, "create_alert", new=AsyncMock(return_value=None)
        ), patch.object(
            internal.scheduled_tasks_route,
            "try_dispatch_chained_schedule_run",
            new=chain_spy,
        ):

            async def run() -> None:
                await internal.process_msg(
                    {
                        "type": "task_state_update",
                        "data": task_state.model_dump(round_trip=True),
                    },
                    MagicMock(),
                )

            portal.call(run)

        chain_spy.assert_awaited_once()
        schedule_row = chain_spy.await_args.args[0]
        self.assertEqual(schedule_id, schedule_row.get_id())
        self.assertEqual(
            "completed_task_1",
            chain_spy.await_args.kwargs["completed_task_id"],
        )
        self.assertEqual(
            internal.CHAIN_ALLOWED_CATEGORIES,
            chain_spy.await_args.kwargs["allowed_categories"],
        )

    def test_completed_task_uses_saved_request_labels_when_booking_is_empty(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp_1", "wp_2"],
                        "rounds": 3,
                    },
                    "labels": ["scheduled_schedule_id=123"],
                },
                created_by="test",
            )
            schedule = await ttm.ScheduledTaskSchedule.create(
                scheduled_task=task,
                period=ttm.ScheduledTaskSchedule.Period.Day,
                start_from=datetime.now(timezone.utc),
                dispatched=True,
            )
            await schedule.fetch_related("scheduled_task")
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)

        async def save_request() -> None:
            await internal.task_repo.save_task_request(
                "completed_task_2",
                mdl.TaskRequest(
                    category="patrol",
                    description={
                        "places": ["wp_1", "wp_2"],
                        "rounds": 3,
                    },
                    labels=[f"scheduled_schedule_id={schedule_id}"],
                ),
            )

        portal.call(save_request)

        task_state = make_task_state(task_id="completed_task_2", labels=[])
        task_state.booking.labels = None
        task_state.status = mdl.TaskStatus.completed

        chain_spy = AsyncMock(return_value=True)

        with patch.object(
            internal.task_repo, "save_task_state", new=AsyncMock()
        ), patch.object(
            internal.alert_repo, "create_alert", new=AsyncMock(return_value=None)
        ), patch.object(
            internal.scheduled_tasks_route,
            "try_dispatch_chained_schedule_run",
            new=chain_spy,
        ):

            async def run() -> None:
                await internal.process_msg(
                    {
                        "type": "task_state_update",
                        "data": task_state.model_dump(round_trip=True),
                    },
                    MagicMock(),
                )

            portal.call(run)

        chain_spy.assert_awaited_once()
        self.assertEqual(schedule_id, chain_spy.await_args.args[0].get_id())
