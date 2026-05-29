import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from api_server import models as mdl
from api_server.models import tortoise_models as ttm
from api_server.routes.tasks import scheduled_tasks as scheduled_tasks_module
from api_server.test import AppFixture, make_task_state


class TestScheduledTasksRoute(AppFixture):
    def test_scheduled_task_crud(self):
        task_until = (datetime.now() + timedelta(days=30)).timestamp()

        scheduled_task = {
            "task_request": {
                "category": "test",
                "description": "test",
            },
            "schedules": [
                {
                    "period": "day",
                    "start_from": 1000,
                    "until": task_until,
                },
                {
                    "period": "day",
                    "start_from": 0,
                    "until": 999,
                },
                {
                    "period": "monday",
                    "start_from": 1000,
                    "until": task_until,
                },
            ],
        }
        resp = self.client.post("/scheduled_tasks", json=scheduled_task)
        self.assertEqual(201, resp.status_code, resp.json())
        task1 = resp.json()
        self.assertEqual(len(task1["schedules"]), 3, task1)

        scheduled_task_2 = {
            "task_request": {
                "category": "test",
                "description": "test",
            },
            "schedules": [
                {
                    "period": "day",
                    "start_from": 2000,
                    "until": task_until,
                },
            ],
        }
        resp = self.client.post("/scheduled_tasks", json=scheduled_task_2)
        self.assertEqual(201, resp.status_code, resp.json())
        task2 = resp.json()
        self.assertEqual(len(task2["schedules"]), 1, task2)

        # check each task id only appears once
        resp = self.client.get(
            f"/scheduled_tasks?start_before=2000&until_after={task_until}"
        )
        self.assertEqual(200, resp.status_code)
        task_ids = [x["id"] for x in resp.json()]
        unique_ids = set(task_ids)
        self.assertEqual(
            len(task_ids), len(unique_ids), "one or more task appears multiple times"
        )

        resp = self.client.get(
            f"/scheduled_tasks?start_before=1000&until_after={task_until}"
        )
        self.assertEqual(200, resp.status_code, resp.json())
        tasks = {x["id"]: x for x in resp.json()}
        self.assertIn(task1["id"], tasks)
        # task2 starts after `start_before`` so should not be included
        self.assertNotIn(task2["id"], tasks)

        resp = self.client.get(
            f"/scheduled_tasks?start_before=2000&until_after={task_until}"
        )
        self.assertEqual(200, resp.status_code, resp.json())
        after = resp.json()
        tasks = {x["id"]: x for x in after}
        self.assertIn(task1["id"], tasks)
        self.assertIn(task2["id"], tasks)

        resp = self.client.get(
            f"/scheduled_tasks?start_before=2000&until_after={task_until+1}"
        )
        self.assertEqual(200, resp.status_code, resp.json())
        after = resp.json()
        tasks = {x["id"]: x for x in after}
        # neither task should be returned as they stop before `until_after`
        self.assertNotIn(task1["id"], tasks)
        self.assertNotIn(task2["id"], tasks)

        resp = self.client.get(f"/scheduled_tasks/{task1['id']}")
        self.assertEqual(200, resp.status_code)
        resp = self.client.get(f"/scheduled_tasks/{task2['id']}")
        self.assertEqual(200, resp.status_code)

        resp = self.client.delete(f"/scheduled_tasks/{task1['id']}")
        self.assertEqual(200, resp.status_code)
        resp = self.client.get(f"/scheduled_tasks/{task1['id']}")
        self.assertEqual(404, resp.status_code)
        resp = self.client.get(
            f"/scheduled_tasks?start_before=2000&until_after={task_until}"
        )
        tasks = {x["id"]: x for x in resp.json()}
        self.assertNotIn(task1["id"], tasks)
        # task 2 should not be deleted
        self.assertIn(task2["id"], tasks)

        resp = self.client.delete(f"/scheduled_tasks/{task2['id']}")
        self.assertEqual(200, resp.status_code)
        resp = self.client.get(f"/scheduled_tasks/{task2['id']}")
        self.assertEqual(404, resp.status_code)
        resp = self.client.get(
            f"/scheduled_tasks?start_before=2000&until_after={task_until}"
        )
        tasks = {x["id"]: x for x in resp.json()}
        self.assertNotIn(task1["id"], tasks)
        self.assertNotIn(task2["id"], tasks)

    def test_cannot_create_task_that_never_runs(self):
        scheduled_task = {
            "task_request": {
                "category": "test",
                "description": "test",
            },
            "schedules": [
                {
                    "start_from": 0,
                    "until": 0,
                    "period": "day",
                }
            ],
        }
        resp = self.client.post("/scheduled_tasks", json=scheduled_task)
        self.assertEqual(422, resp.status_code)

    def test_get_scheduled_tasks_return_indefinite_tasks(self):
        scheduled_task = {
            "task_request": {
                "category": "test",
                "description": "test",
            },
            "schedules": [
                {
                    "period": "day",
                }
            ],
        }
        resp = self.client.post("/scheduled_tasks", json=scheduled_task)
        self.assertEqual(201, resp.status_code, resp.json())
        task = resp.json()

        resp = self.client.get("/scheduled_tasks?start_before=0&until_after=0")
        self.assertEqual(200, resp.status_code, resp.json())
        tasks = {x["id"]: x for x in resp.json()}
        self.assertIn(task["id"], tasks)

    def test_create_scheduled_task_persists_computed_next_run(self):
        scheduled_task = {
            "task_request": {
                "category": "test",
                "description": "test",
            },
            "schedules": [
                {
                    "period": "day",
                    "start_from": 1000,
                }
            ],
        }

        expected_start = datetime.now(timezone.utc) + timedelta(days=1)
        with patch.object(
            scheduled_tasks_module,
            "compute_next_run",
            return_value=expected_start,
        ) as mock_compute:
            resp = self.client.post("/scheduled_tasks", json=scheduled_task)

        self.assertEqual(201, resp.status_code, resp.json())
        mock_compute.assert_called()
        task = resp.json()
        self.assertEqual(len(task["schedules"]), 1)
        schedule_id = task["schedules"][0]["id"]
        portal = self.get_portal()

        async def load_schedule():
            return await ttm.ScheduledTaskSchedule.get_or_none(_id=schedule_id)

        schedule_row = portal.call(load_schedule)
        self.assertIsNotNone(schedule_row)
        self.assertEqual(schedule_row.next_run_at, expected_start)

    def test_dispatch_scheduled_patrol_forces_single_round(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp1", "wp2"],
                        "rounds": 7,
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
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        captured_requests = []

        async def fake_post_dispatch_task(dispatch_request, _task_repo):
            captured_requests.append(dispatch_request)

        with patch.object(
            scheduled_tasks_module,
            "post_dispatch_task",
            side_effect=fake_post_dispatch_task,
        ), patch.object(
            scheduled_tasks_module,
            "compute_next_run",
            return_value=None,
        ):
            portal.call(
                scheduled_tasks_module._dispatch_scheduled_schedule, schedule_id
            )

        self.assertEqual(1, len(captured_requests))
        request_payload = captured_requests[0].request
        self.assertEqual("patrol", request_payload.category)
        self.assertEqual(1, request_payload.description["rounds"])

    def test_cancel_overdue_schedule_tasks_skips_finished_tasks(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "test",
                    "description": "test",
                },
                created_by="test",
            )
            schedule = await ttm.ScheduledTaskSchedule.create(
                scheduled_task=task,
                period=ttm.ScheduledTaskSchedule.Period.Day,
                start_from=datetime.now(timezone.utc),
                planned_end_at="00:00",
                dispatched=True,
            )
            await schedule.fetch_related("scheduled_task")
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)

        active_state = make_task_state(
            task_id="active_task",
            labels=[f"scheduled_schedule_id={schedule_id}"],
        )
        active_state.status = mdl.TaskStatus.underway
        active_state.unix_millis_finish_time = None

        finished_state = make_task_state(
            task_id="finished_task",
            labels=[f"scheduled_schedule_id={schedule_id}"],
        )
        finished_state.status = mdl.TaskStatus.completed
        finished_state.unix_millis_finish_time = int(
            datetime.now(timezone.utc).timestamp() * 1000
        )

        fake_repo = MagicMock()
        fake_repo.query_task_states = AsyncMock(
            return_value=[active_state, finished_state]
        )

        mock_service = MagicMock()
        mock_service.call = AsyncMock(return_value='{"success": true}')

        with patch.object(
            scheduled_tasks_module,
            "should_skip_due_to_planned_end",
            return_value=True,
        ), patch.object(
            scheduled_tasks_module,
            "tasks_service",
            return_value=mock_service,
        ):

            async def run_cancel() -> None:
                schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                    _id=schedule_id
                )
                assert schedule_row is not None
                await schedule_row.fetch_related("scheduled_task")
                await scheduled_tasks_module._cancel_overdue_schedule_tasks(
                    schedule_row,
                    fake_repo,
                    datetime.now(timezone.utc),
                )

            portal.call(run_cancel)

        fake_repo.query_task_states.assert_awaited_once()
        mock_service.call.assert_awaited_once()
        cancel_payload = json.loads(mock_service.call.await_args.args[0])
        self.assertEqual("active_task", cancel_payload["task_id"])
        self.assertEqual(
            [
                f"scheduled_schedule_id={schedule_id}",
                "scheduled_end_time_auto_cancel",
            ],
            cancel_payload["labels"],
        )

    def test_try_dispatch_chained_schedule_run_dispatches_with_chain_label(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp1", "wp2"],
                        "rounds": 9,
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
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        captured_requests = []

        fake_repo = MagicMock()

        async def fake_query_task_states(**kwargs):
            _ = kwargs
            return []

        fake_repo.query_task_states = AsyncMock(side_effect=fake_query_task_states)

        async def fake_post_dispatch_task(dispatch_request, _task_repo):
            captured_requests.append(dispatch_request)

        with patch.object(
            scheduled_tasks_module,
            "post_dispatch_task",
            side_effect=fake_post_dispatch_task,
        ), patch.object(
            scheduled_tasks_module,
            "should_skip_due_to_planned_end",
            return_value=False,
        ), patch.object(
            scheduled_tasks_module,
            "_occurrence_allowed",
            return_value=True,
        ):

            async def run_chain() -> bool:
                schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                    _id=schedule_id
                ).select_related("scheduled_task")
                assert schedule_row is not None
                return await scheduled_tasks_module.try_dispatch_chained_schedule_run(
                    schedule_row,
                    fake_repo,
                    completed_task_id="completed_1",
                    allowed_categories={"patrol"},
                )

            dispatched = portal.call(run_chain)

        self.assertTrue(dispatched)
        self.assertEqual(1, len(captured_requests))
        request_payload = captured_requests[0].request
        self.assertEqual("patrol", request_payload.category)
        self.assertEqual(1, request_payload.description["rounds"])
        self.assertIn(
            "scheduled_schedule_id=" + str(schedule_id), request_payload.labels
        )
        self.assertIn("chain_parent_task_id=completed_1", request_payload.labels)

    def test_try_dispatch_chained_schedule_run_skips_when_active_exists(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp1", "wp2"],
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
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        captured_requests = []

        active_state = make_task_state(
            task_id="active_task",
            labels=[f"scheduled_schedule_id={schedule_id}"],
        )
        active_state.status = mdl.TaskStatus.underway
        active_state.unix_millis_finish_time = None

        fake_repo = MagicMock()

        async def fake_query_task_states(**kwargs):
            label = kwargs.get("label")
            label_map = label.root if label is not None else {}
            if "chain_parent_task_id" in label_map:
                return []
            return [active_state]

        fake_repo.query_task_states = AsyncMock(side_effect=fake_query_task_states)

        async def fake_post_dispatch_task(dispatch_request, _task_repo):
            captured_requests.append(dispatch_request)

        with patch.object(
            scheduled_tasks_module,
            "post_dispatch_task",
            side_effect=fake_post_dispatch_task,
        ), patch.object(
            scheduled_tasks_module,
            "should_skip_due_to_planned_end",
            return_value=False,
        ), patch.object(
            scheduled_tasks_module,
            "_occurrence_allowed",
            return_value=True,
        ):

            async def run_chain() -> bool:
                schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                    _id=schedule_id
                ).select_related("scheduled_task")
                assert schedule_row is not None
                return await scheduled_tasks_module.try_dispatch_chained_schedule_run(
                    schedule_row,
                    fake_repo,
                    completed_task_id="completed_2",
                    allowed_categories={"patrol"},
                )

            dispatched = portal.call(run_chain)

        self.assertFalse(dispatched)
        self.assertEqual(0, len(captured_requests))

    def test_try_dispatch_chained_schedule_run_skips_duplicate_completion(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp1", "wp2"],
                        "rounds": 2,
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
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        captured_requests = []

        duplicate_state = make_task_state(
            task_id="child_task",
            labels=[
                f"scheduled_schedule_id={schedule_id}",
                "chain_parent_task_id=completed_3",
            ],
        )
        duplicate_state.status = mdl.TaskStatus.queued

        fake_repo = MagicMock()

        async def fake_query_task_states(**kwargs):
            label = kwargs.get("label")
            label_map = label.root if label is not None else {}
            if "chain_parent_task_id" in label_map:
                return [duplicate_state]
            return []

        fake_repo.query_task_states = AsyncMock(side_effect=fake_query_task_states)

        async def fake_post_dispatch_task(dispatch_request, _task_repo):
            captured_requests.append(dispatch_request)

        with patch.object(
            scheduled_tasks_module,
            "post_dispatch_task",
            side_effect=fake_post_dispatch_task,
        ), patch.object(
            scheduled_tasks_module,
            "should_skip_due_to_planned_end",
            return_value=False,
        ), patch.object(
            scheduled_tasks_module,
            "_occurrence_allowed",
            return_value=True,
        ):

            async def run_chain() -> bool:
                schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                    _id=schedule_id
                ).select_related("scheduled_task")
                assert schedule_row is not None
                return await scheduled_tasks_module.try_dispatch_chained_schedule_run(
                    schedule_row,
                    fake_repo,
                    completed_task_id="completed_3",
                    allowed_categories={"patrol"},
                )

            dispatched = portal.call(run_chain)

        self.assertFalse(dispatched)
        self.assertEqual(0, len(captured_requests))

    def test_try_dispatch_chained_schedule_run_allows_clean_category(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "clean",
                    "description": {
                        "zone": "zone_a",
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
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        captured_requests = []

        fake_repo = MagicMock()

        async def fake_query_task_states(**kwargs):
            _ = kwargs
            return []

        fake_repo.query_task_states = AsyncMock(side_effect=fake_query_task_states)

        async def fake_post_dispatch_task(dispatch_request, _task_repo):
            captured_requests.append(dispatch_request)

        with patch.object(
            scheduled_tasks_module,
            "post_dispatch_task",
            side_effect=fake_post_dispatch_task,
        ), patch.object(
            scheduled_tasks_module,
            "should_skip_due_to_planned_end",
            return_value=False,
        ), patch.object(
            scheduled_tasks_module,
            "_occurrence_allowed",
            return_value=True,
        ):

            async def run_chain() -> bool:
                schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                    _id=schedule_id
                ).select_related("scheduled_task")
                assert schedule_row is not None
                return await scheduled_tasks_module.try_dispatch_chained_schedule_run(
                    schedule_row,
                    fake_repo,
                    completed_task_id="completed_clean_1",
                    allowed_categories={"patrol", "clean", "loop", "compose"},
                )

            dispatched = portal.call(run_chain)

        self.assertTrue(dispatched)
        self.assertEqual(1, len(captured_requests))
        request_payload = captured_requests[0].request
        self.assertEqual("clean", request_payload.category)
        self.assertIn(
            "scheduled_schedule_id=" + str(schedule_id), request_payload.labels
        )

    def test_cancel_active_scheduled_task_if_overdue_cancels_live_task(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp1", "wp2"],
                        "rounds": 4,
                    },
                },
                created_by="test",
            )
            schedule = await ttm.ScheduledTaskSchedule.create(
                scheduled_task=task,
                period=ttm.ScheduledTaskSchedule.Period.Day,
                start_from=datetime.now(timezone.utc),
                planned_end_at="00:00",
                dispatched=True,
            )
            await schedule.fetch_related("scheduled_task")
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        active_state = make_task_state(
            task_id="active_task_overdue",
            labels=[f"scheduled_schedule_id={schedule_id}"],
        )
        active_state.status = mdl.TaskStatus.underway
        active_state.unix_millis_finish_time = None

        mock_service = MagicMock()
        mock_service.call = AsyncMock(return_value='{"success": true}')

        with patch.object(
            scheduled_tasks_module,
            "should_skip_due_to_planned_end",
            return_value=True,
        ), patch.object(
            scheduled_tasks_module,
            "tasks_service",
            return_value=mock_service,
        ):

            async def run_cancel() -> bool:
                schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                    _id=schedule_id
                ).select_related("scheduled_task")
                assert schedule_row is not None
                return await scheduled_tasks_module.cancel_active_scheduled_task_if_overdue(
                    schedule_row,
                    active_state,
                    MagicMock(),
                    datetime.now(timezone.utc),
                )

            cancelled = portal.call(run_cancel)

        self.assertTrue(cancelled)
        mock_service.call.assert_awaited_once()
        cancel_payload = json.loads(mock_service.call.await_args.args[0])
        self.assertEqual("active_task_overdue", cancel_payload["task_id"])
        self.assertEqual(
            [
                f"scheduled_schedule_id={schedule_id}",
                "scheduled_end_time_auto_cancel",
            ],
            cancel_payload["labels"],
        )

    def test_cancel_active_scheduled_task_if_overdue_ignores_task_start_time(self):
        portal = self.get_portal()

        async def create_schedule() -> int:
            task = await ttm.ScheduledTask.create(
                task_request={
                    "category": "patrol",
                    "description": {
                        "places": ["wp1", "wp2"],
                        "rounds": 2,
                    },
                },
                created_by="test",
            )
            schedule = await ttm.ScheduledTaskSchedule.create(
                scheduled_task=task,
                period=ttm.ScheduledTaskSchedule.Period.Day,
                start_from=datetime.now(timezone.utc),
                planned_end_at="00:00",
                dispatched=True,
            )
            await schedule.fetch_related("scheduled_task")
            return schedule.get_id()

        schedule_id = portal.call(create_schedule)
        active_state = make_task_state(
            task_id="active_task_started_early",
            labels=[f"scheduled_schedule_id={schedule_id}"],
        )
        active_state.status = mdl.TaskStatus.underway
        active_state.unix_millis_finish_time = None
        active_state.unix_millis_start_time = int(
            (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000
        )

        mock_service = MagicMock()
        mock_service.call = AsyncMock(return_value='{"success": true}')

        def _assert_skip_due_to_planned_end(
            _schedule_row,
            _now_utc,
            candidate_dt_utc=None,
        ) -> bool:
            self.assertIsNone(candidate_dt_utc)
            return True

        with patch.object(
            scheduled_tasks_module,
            "should_skip_due_to_planned_end",
            side_effect=_assert_skip_due_to_planned_end,
        ), patch.object(
            scheduled_tasks_module,
            "tasks_service",
            return_value=mock_service,
        ):

            async def run_cancel() -> bool:
                schedule_row = await ttm.ScheduledTaskSchedule.get_or_none(
                    _id=schedule_id
                ).select_related("scheduled_task")
                assert schedule_row is not None
                return await scheduled_tasks_module.cancel_active_scheduled_task_if_overdue(
                    schedule_row,
                    active_state,
                    MagicMock(),
                    datetime.now(timezone.utc),
                )

            cancelled = portal.call(run_cancel)

        self.assertTrue(cancelled)
        mock_service.call.assert_awaited_once()
