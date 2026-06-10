from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from api_server import models as mdl
from api_server.models import tortoise_models as ttm
from api_server.routes import internal
from api_server.test import AppFixture, make_task_state
from api_server.utils.time_utils import datetime_to_wall_millis


class TestInternalRoute(AppFixture):
    def test_env_category_allowlist_normalizes_and_excludes_clean(self):
        with patch.dict(
            "os.environ",
            {"RMF_CHAIN_ALLOWED_CATEGORIES": "patrol, clean, LOOP"},
        ):
            self.assertEqual({"patrol", "loop"}, internal._env_category_allowlist())

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

    def test_completed_scheduled_clean_updates_planned_end_to_finish_time(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "clean",
                    "description": {
                        "zone": "clean_inno_room",
                    },
                },
                created_by="test",
            )
            schedule = await ttm.ScheduledTaskSchedule.create(
                scheduled_task=task,
                period=ttm.ScheduledTaskSchedule.Period.Day,
                start_from=datetime(2026, 6, 8, 14, 38, tzinfo=timezone.utc),
                planned_end_at=None,
                dispatched=True,
            )
            await schedule.fetch_related("scheduled_task")
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        finish_time = datetime(2026, 6, 8, 14, 48, tzinfo=timezone.utc)
        task_state = make_task_state(
            task_id="completed_clean_task",
            labels=[f"scheduled_schedule_id={schedule_id}"],
        )
        task_state.status = mdl.TaskStatus.completed
        task_state.unix_millis_finish_time = datetime_to_wall_millis(finish_time)

        with patch.object(
            internal.task_repo, "save_task_state", new=AsyncMock()
        ), patch.object(
            internal.alert_repo, "create_alert", new=AsyncMock(return_value=None)
        ), patch.object(
            internal.scheduled_tasks_route,
            "try_dispatch_chained_schedule_run",
            new=AsyncMock(return_value=False),
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

        async def load_planned_end() -> str | None:
            schedule = await ttm.ScheduledTaskSchedule.get(_id=schedule_id)
            return schedule.planned_end_at

        local_finish_time = finish_time.astimezone(datetime.now().astimezone().tzinfo)
        self.assertEqual(
            local_finish_time.strftime("%H:%M"), portal.call(load_planned_end)
        )
