import json
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pydantic

from api_server import models as mdl
from api_server.models import TaskEventLog, TaskState
from api_server.repositories import TaskRepository
from api_server.rmf_io import task_events, tasks_service
from api_server.routes.tasks import tasks as tasks_route
from api_server.test import AppFixture, make_task_log, make_task_state


class TestTasksRoute(AppFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        task_ids = [str(uuid4()), str(uuid4())]
        cls.task_states = [
            make_task_state(
                task_id=task_ids[0],
                labels=[
                    "test_single",
                    "test_single_2=",
                    "test_kv=value",
                    "test_label_sort=zzz",
                    "test_label_sort_2=aaa",
                    "test_label_sort_3=bbb",
                ],
            ),
            make_task_state(
                task_id=task_ids[1],
                labels=["test_label_sort=aaa", "test_label_sort_3=bbb"],
            ),
        ]
        cls.task_logs = [make_task_log(task_id=f"test_{x}") for x in task_ids]
        cls.clsSetupErr: str | None = None

        portal = cls.get_portal()
        repo = TaskRepository(cls.admin_user)
        for x in cls.task_states:
            portal.call(repo.save_task_state, x)
        for x in cls.task_logs:
            portal.call(repo.save_task_log, x)

    def setUp(self):
        super().setUp()
        self.assertIsNone(self.clsSetupErr)

    def test_get_task_state(self):
        resp = self.client.get(f"/tasks/{self.task_states[0].booking.id}/state")
        self.assertEqual(200, resp.status_code)
        task_state = TaskState.model_validate_json(resp.content)
        self.assertEqual(
            self.task_states[0].booking.id,
            task_state.booking.id,
        )
        if task_state.booking.labels is None:
            self.fail("expected label not to be None")
        labels = mdl.Labels.from_strings(task_state.booking.labels)
        self.assertEqual("", labels.root["test_single"])
        self.assertEqual("", labels.root["test_single_2"])
        self.assertEqual("value", labels.root["test_kv"])

    def test_query_task_states(self):
        resp = self.client.get(f"/tasks?task_id={self.task_states[0].booking.id}")
        self.assertEqual(200, resp.status_code)
        results = resp.json()
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0]["booking"]["id"])

    def test_query_task_states_filter_by_label(self):
        resp = self.client.get("/tasks?label=not_existing")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

        resp = self.client.get("/tasks?label=test_single")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_single=wrong_value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

        resp = self.client.get("/tasks?label=test_single_2=")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_kv=value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_kv=wrong_value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

        resp = self.client.get("/tasks?label=test_single,test_kv=value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_single,test_kv=wrong_value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

    def test_query_task_states_sort_by_label(self):
        resp = self.client.get("/tasks?order_by=-label=test_label_sort")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))
        for a, b in zip(self.task_states, results):
            self.assertEqual(a, b)

        resp = self.client.get("/tasks?order_by=label=test_label_sort")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))
        for a, b in zip(self.task_states[::-1], results):
            self.assertEqual(a, b)

        # test sorting by multiple labels
        resp = self.client.get(
            "/tasks?order_by=label=test_label_sort,label=test_label_sort_3"
        )
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))
        for a, b in zip(self.task_states[::-1], results):
            self.assertEqual(a, b)

        # test that tasks without the label are not filtered out
        # we don't test the result order because different db has different behavior
        # of sorting NULL.
        resp = self.client.get("/tasks?order_by=label=test_label_sort_not_existing")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))

    def test_sub_task_state(self):
        task_id = self.task_states[0].booking.id
        with self.subscribe_sio(f"/tasks/{task_id}/state") as sub:
            task_events.task_states.on_next(self.task_states[0])
            state = TaskState(**next(sub))
            self.assertEqual(task_id, cast(TaskState, state).booking.id)

    def test_get_task_log(self):
        resp = self.client.get(
            f"/tasks/{self.task_logs[0].task_id}/log?between=0,1636388414500"
        )
        self.assertEqual(200, resp.status_code)
        logs = mdl.TaskEventLog(**resp.json())
        self.assertEqual(self.task_logs[0].task_id, logs.task_id)

        # check task log
        if logs.log is None:
            self.assertIsNotNone(logs.log)
            return
        self.assertEqual(1, len(logs.log))
        log = logs.log[0]  # pylint: disable=unsubscriptable-object
        self.assertEqual(0, log.seq)
        self.assertEqual(mdl.Tier.info, log.tier)
        self.assertEqual(1636388410000, log.unix_millis_time)
        self.assertEqual("Beginning task", log.text)

        # check number of phases
        if logs.phases is None:
            self.assertIsNotNone(logs.phases)
            return
        self.assertEqual(2, len(logs.phases))
        self.assertIn("1", logs.phases)
        self.assertIn("2", logs.phases)

        # check correct log
        phase1 = logs.phases["1"]  # pylint: disable=unsubscriptable-object
        phase1_log = phase1.log
        if phase1_log is None:
            self.assertIsNotNone(phase1_log)
            return
        self.assertEqual(1, len(phase1_log))
        log = phase1_log[0]
        self.assertEqual(0, log.seq)
        self.assertEqual(mdl.Tier.info, log.tier)
        self.assertEqual(1636388410000, log.unix_millis_time)
        self.assertEqual("Beginning phase", log.text)

        # check number of events
        phase1_events = phase1.events
        if phase1_events is None:
            self.assertIsNotNone(phase1_events)
            return
        self.assertEqual(
            7, len(phase1_events)
        )  # check all events are returned, including those with no logs

        # check event log
        self.assertIn("1", phase1_events)
        self.assertEqual(
            3, len(phase1_events["1"])
        )  # check only logs in the period is returned
        log = phase1_events["1"][0]
        self.assertEqual(0, log.seq)
        self.assertEqual(mdl.Tier.info, log.tier)
        self.assertEqual(1636388409995, log.unix_millis_time)
        self.assertEqual(
            "Generating plan to get from [place:parking_03] to [place:kitchen]",
            log.text,
        )

        # TODO: check relative time is working, this requires the use of
        # dependencies overrides, which requires change in the server architecture.
        # Better to do this after this gets merged into main so we don't make
        # more architecture changes.

    def test_sub_task_log(self):
        task_id = self.task_logs[0].task_id
        with self.subscribe_sio(f"/tasks/{task_id}/log") as sub:
            task_events.task_event_logs.on_next(self.task_logs[0])
            log = TaskEventLog(**next(sub))
            self.assertEqual(task_id, cast(TaskEventLog, log).task_id)

    def test_activity_discovery(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = "{}"
            resp = self.client.post(
                "/tasks/activity_discovery",
                content=mdl.ActivityDiscoveryRequest(
                    type="activitiy_discovery_request",
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_cancel_task(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/activity_discovery",
                content=mdl.ActivityDiscoveryRequest(
                    type="activitiy_discovery_request"
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_interrupt_task(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": True, "token": "token" }'
            resp = self.client.post(
                "/tasks/interrupt_task",
                content=mdl.TaskInterruptionRequest(
                    type="interrupt_task_request", task_id="task_id", labels=None
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_kill_task(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/kill_task",
                content=mdl.TaskKillRequest(
                    type="kill_task_request", task_id="task_id", labels=None
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_resume_task(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/resume_task",
                content=mdl.TaskResumeRequest(
                    type=None, for_task=None, for_tokens=None, labels=None
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_rewind_task(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/rewind_task",
                content=mdl.TaskRewindRequest(
                    type="rewind_task_request", task_id="task_id", phase_id=0
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_skip_phase(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": True, "token": "token" }'
            resp = self.client.post(
                "/tasks/skip_phase",
                content=mdl.TaskPhaseSkipRequest(
                    type="skip_phase_request",
                    task_id="task_id",
                    phase_id=0,
                    labels=None,
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_task_discovery(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = "{}"
            resp = self.client.post(
                "/tasks/task_discovery",
                content=mdl.TaskDiscoveryRequest(
                    type="task_discovery_request"
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_undo_skip_phase(self):
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = '{ "success": True }'
            resp = self.client.post(
                "/tasks/undo_skip_phase",
                content=mdl.UndoPhaseSkipRequest(
                    type="undo_phase_skip_request",
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)


class TestDispatchTask(AppFixture):
    def post_task_request(self):
        return self.client.post(
            "/tasks/dispatch_task",
            content=mdl.DispatchTaskRequest(
                type="dispatch_task_request",
                request=mdl.TaskRequest(
                    category="test",
                    description="description",
                ),
            ).model_dump_json(exclude_none=True),
        )

    def test_success(self):
        task_id = str(uuid4())
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'
            resp = self.post_task_request()
            self.assertEqual(200, resp.status_code, resp.content)

        # check that the task is already in the database by the time the dispatch request returns
        resp = self.client.get(f"/tasks/{task_id}/state")
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual(task_id, resp.json()["booking"]["id"])

    def test_task_request_exist(self):
        task_id = str(uuid4())
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'
            resp = self.post_task_request()
            self.assertEqual(200, resp.status_code, resp.content)

        # check that the task request is in the database
        resp = self.client.get(f"/tasks/{task_id}/request")
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual("test", resp.json()["category"])
        self.assertEqual("description", resp.json()["description"])

    @staticmethod
    def _make_active_task_state(
        task_id: str, fleet_name: str, robot_name: str
    ) -> mdl.TaskState:
        return mdl.TaskState.model_validate(
            {
                "booking": {"id": task_id},
                "status": "underway",
                "assigned_to": {"group": fleet_name, "name": robot_name},
                "dispatch": {
                    "status": "dispatched",
                    "assignment": {
                        "fleet_name": fleet_name,
                        "expected_robot_name": robot_name,
                    },
                },
            }
        )

    def test_critical_preemption_skips_redispatch_when_self_assigned(self):
        # If RMF auctions the critical's own (already-queued) booking onto
        # the freed robot on its own, we must not create any second task for
        # it — only the kill and the victim's requeue should happen.
        portal = self.get_portal()
        fleet_name = "test_fleet"
        robot_name = "test_robot"
        victim_task_id = str(uuid4())
        critical_task_id = str(uuid4())
        requeued_task_id = "requeued_victim_task"

        victim_task = self._make_active_task_state(
            victim_task_id, fleet_name, robot_name
        )
        victim_request = mdl.TaskRequest(
            category="patrol",
            description="victim",
            labels=["priority=urgent"],
        )
        critical_request = mdl.TaskRequest(
            category="patrol",
            description="critical dispatch",
            labels=["priority=critical"],
        )

        call_order = []
        requeue_payloads = []

        async def fake_service_call(payload: str, timeout=None):
            data = json.loads(payload)
            request_type = data["type"]
            if request_type == "kill_task_request":
                call_order.append("kill")
                self.assertEqual(victim_task_id, data["task_id"])
                return '{"success": true}'
            if request_type == "dispatch_task_request":
                call_order.append("requeue_victim")
                requeue_payloads.append(data)
                return f'{{"success": true, "state": {{"booking": {{"id": "{requeued_task_id}"}}}}}}'
            raise AssertionError(
                f"unexpected RMF request type: {request_type} — the critical "
                "self-assigned, nothing else should have been dispatched"
            )

        async def fake_get_fleet_state(name: str):
            return mdl.FleetState(
                name=name,
                robots={
                    robot_name: mdl.RobotState(
                        name=robot_name, status="idle", task_id=""
                    )
                },
            )

        def fake_get_task_request(task_id: str):
            if task_id == critical_task_id:
                return critical_request
            if task_id == victim_task_id:
                return victim_request
            return None

        task_repo = AsyncMock(spec=TaskRepository)
        task_repo.query_task_states.return_value = [victim_task]
        task_repo.get_task_request.side_effect = fake_get_task_request
        task_repo.get_task_state.return_value = None

        fleet_repo = AsyncMock()
        fleet_repo.get_fleet_state.side_effect = fake_get_fleet_state

        with patch.object(
            tasks_route, "tasks_service"
        ) as mock_tasks_service, patch.object(
            tasks_route, "_spawn_background_task", return_value=None
        ), patch.object(
            tasks_route,
            "_wait_for_critical_self_assignment",
            AsyncMock(return_value=True),
        ):
            mock_tasks_service.return_value.call = AsyncMock(
                side_effect=fake_service_call
            )
            portal.call(
                tasks_route._handle_critical_preemption,
                critical_task_id,
                task_repo,
                fleet_repo,
                0,
            )

        self.assertEqual(["kill", "requeue_victim"], call_order)
        requeued_request = requeue_payloads[0]["request"]
        self.assertIn(f"requeued_from={victim_task_id}", requeued_request["labels"])

    def test_critical_preemption_falls_back_to_redispatch_when_stalled(self):
        # If the critical's own booking does NOT get auctioned onto the
        # freed robot within the wait window, the fallback must cleanly
        # cancel it (checked, unlike the old code) and only then re-dispatch
        # through the normal auction — never via a direct robot_task_request,
        # and never without confirming the cancel first.
        portal = self.get_portal()
        fleet_name = "test_fleet"
        robot_name = "test_robot"
        victim_task_id = str(uuid4())
        critical_task_id = str(uuid4())
        redispatched_task_id = "redispatched_critical_task"
        requeued_task_id = "requeued_victim_task"

        victim_task = self._make_active_task_state(
            victim_task_id, fleet_name, robot_name
        )
        victim_request = mdl.TaskRequest(
            category="patrol",
            description="victim",
            labels=["priority=urgent"],
        )
        critical_request = mdl.TaskRequest(
            category="patrol",
            description="critical dispatch",
            labels=["priority=critical"],
        )

        call_order = []
        dispatch_payloads = []

        async def fake_service_call(payload: str, timeout=None):
            data = json.loads(payload)
            request_type = data["type"]
            if request_type == "kill_task_request":
                call_order.append("kill")
                self.assertEqual(victim_task_id, data["task_id"])
                return '{"success": true}'
            if request_type == "robot_task_request":
                raise AssertionError(
                    "must never force-assign via robot_task_request anymore"
                )
            if request_type == "cancel_task_request":
                call_order.append("cancel")
                self.assertEqual(critical_task_id, data["task_id"])
                return '{"success": true}'
            if request_type == "dispatch_task_request":
                labels = data["request"].get("labels") or []
                dispatch_payloads.append(data)
                if any(label == f"forced_from={critical_task_id}" for label in labels):
                    call_order.append("redispatch_critical")
                    self.assertIn(
                        "cancel",
                        call_order,
                        "re-dispatch must only happen after the cancel is " "confirmed",
                    )
                    return (
                        '{"success": true, "state": {"booking": '
                        f'{{"id": "{redispatched_task_id}"}}}}}}'
                    )
                if any(label == f"requeued_from={victim_task_id}" for label in labels):
                    call_order.append("requeue_victim")
                    return (
                        '{"success": true, "state": {"booking": '
                        f'{{"id": "{requeued_task_id}"}}}}}}'
                    )
                raise AssertionError(f"unexpected dispatch labels: {labels}")
            raise AssertionError(f"unexpected RMF request type: {request_type}")

        async def fake_get_fleet_state(name: str):
            return mdl.FleetState(
                name=name,
                robots={
                    robot_name: mdl.RobotState(
                        name=robot_name, status="idle", task_id=""
                    )
                },
            )

        def fake_get_task_request(task_id: str):
            if task_id == critical_task_id:
                return critical_request
            if task_id == victim_task_id:
                return victim_request
            return None

        task_repo = AsyncMock(spec=TaskRepository)
        task_repo.query_task_states.return_value = [victim_task]
        task_repo.get_task_request.side_effect = fake_get_task_request
        task_repo.get_task_state.return_value = None

        fleet_repo = AsyncMock()
        fleet_repo.get_fleet_state.side_effect = fake_get_fleet_state

        with patch.object(
            tasks_route, "tasks_service"
        ) as mock_tasks_service, patch.object(
            tasks_route, "_spawn_background_task", return_value=None
        ), patch.object(
            tasks_route,
            "_wait_for_critical_self_assignment",
            AsyncMock(return_value=False),
        ):
            mock_tasks_service.return_value.call = AsyncMock(
                side_effect=fake_service_call
            )
            portal.call(
                tasks_route._handle_critical_preemption,
                critical_task_id,
                task_repo,
                fleet_repo,
                0,
            )

        self.assertEqual(
            ["kill", "cancel", "redispatch_critical", "requeue_victim"], call_order
        )
        requeue_payload = next(
            p
            for p in dispatch_payloads
            if f"requeued_from={victim_task_id}" in (p["request"].get("labels") or [])
        )
        self.assertEqual(
            {"type": "binary", "value": 1},
            requeue_payload["request"]["priority"],
            "requeued victim must keep its original priority",
        )
        self.assertTrue(task_repo.save_task_request.called)
        self.assertTrue(task_repo.save_task_state.called)

    def test_redispatch_stalled_critical_aborts_without_duplicating_when_cancel_fails(
        self,
    ):
        # If we can't confirm the cancel succeeded, we must not dispatch a
        # fresh copy — that would recreate exactly the duplicate-task bug
        # this whole redesign exists to remove.
        portal = self.get_portal()
        critical_task_id = str(uuid4())

        task_repo = AsyncMock(spec=TaskRepository)
        fleet_repo = AsyncMock()
        service_call = AsyncMock(return_value='{"success": false}')

        with patch.object(tasks_route, "tasks_service") as mock_tasks_service:
            mock_tasks_service.return_value.call = service_call
            result = portal.call(
                tasks_route._redispatch_stalled_critical,
                critical_task_id,
                "test_fleet",
                task_repo,
                fleet_repo,
                0,
            )

        self.assertIsNone(result)
        service_call.assert_awaited_once()
        task_repo.get_task_request.assert_not_called()

    def test_critical_does_not_preempt_equal_priority(self):
        portal = self.get_portal()
        victim_task_id = str(uuid4())
        critical_task_id = str(uuid4())

        victim_task = self._make_active_task_state(
            victim_task_id, "test_fleet", "test_robot"
        )
        running_critical_request = mdl.TaskRequest(
            category="patrol",
            description="running critical",
            labels=["priority=critical"],
        )
        incoming_critical_request = mdl.TaskRequest(
            category="patrol",
            description="incoming critical",
            labels=["priority=critical"],
        )

        def fake_get_task_request(task_id: str):
            if task_id == critical_task_id:
                return incoming_critical_request
            return running_critical_request

        task_repo = AsyncMock(spec=TaskRepository)
        task_repo.query_task_states.return_value = [victim_task]
        task_repo.get_task_request.side_effect = fake_get_task_request
        task_repo.get_task_state.return_value = None

        fleet_repo = AsyncMock()
        service_call = AsyncMock(
            side_effect=AssertionError("a critical must not preempt a critical")
        )

        with patch.object(
            tasks_route, "tasks_service"
        ) as mock_tasks_service, patch.object(
            tasks_route, "_spawn_background_task", return_value=None
        ):
            mock_tasks_service.return_value.call = service_call
            portal.call(
                tasks_route._handle_critical_preemption,
                critical_task_id,
                task_repo,
                fleet_repo,
                0,
            )

        service_call.assert_not_called()

    def test_critical_watcher_stands_down_when_own_task_active(self):
        portal = self.get_portal()
        critical_task_id = str(uuid4())

        task_repo = AsyncMock(spec=TaskRepository)
        task_repo.get_task_state.return_value = self._make_active_task_state(
            critical_task_id, "test_fleet", "test_robot"
        )

        fleet_repo = AsyncMock()
        service_call = AsyncMock()

        with patch.object(
            tasks_route, "tasks_service"
        ) as mock_tasks_service, patch.object(
            tasks_route, "_spawn_background_task", return_value=None
        ):
            mock_tasks_service.return_value.call = service_call
            portal.call(
                tasks_route._handle_critical_preemption,
                critical_task_id,
                task_repo,
                fleet_repo,
                0,
            )

        task_repo.query_task_states.assert_not_called()
        service_call.assert_not_called()

    def test_select_preemption_victim_skips_equal_priority_candidates(self):
        portal = self.get_portal()
        critical_id = str(uuid4())
        urgent_id = str(uuid4())
        candidates = [
            self._make_active_task_state(critical_id, "test_fleet", "robot_1"),
            self._make_active_task_state(urgent_id, "test_fleet", "robot_2"),
        ]
        requests = {
            critical_id: mdl.TaskRequest(
                category="patrol",
                description="running critical",
                labels=["priority=critical"],
            ),
            urgent_id: mdl.TaskRequest(
                category="patrol",
                description="running urgent",
                labels=["priority=urgent"],
            ),
        }

        task_repo = AsyncMock(spec=TaskRepository)
        task_repo.get_task_request.side_effect = lambda task_id: requests.get(task_id)

        victim = portal.call(
            tasks_route._select_preemption_victim,
            candidates,
            "incoming-critical-id",
            2,
            "patrol",
            task_repo,
        )

        self.assertIsNotNone(victim)
        self.assertEqual(urgent_id, victim.booking.id)

    def test_select_preemption_victim_skips_different_category(self):
        # Fleets are function-specific (clean/patrol/deliver); a critical
        # clean must never kill an active patrol just because it happens to
        # be the first active task found — that fleet could never have
        # served the clean anyway.
        portal = self.get_portal()
        patrol_id = str(uuid4())
        clean_id = str(uuid4())
        candidates = [
            self._make_active_task_state(patrol_id, "patrol_fleet", "robot_1"),
            self._make_active_task_state(clean_id, "clean_fleet", "robot_2"),
        ]
        requests = {
            patrol_id: mdl.TaskRequest(
                category="patrol",
                description="running patrol",
                labels=["priority=normal"],
            ),
            clean_id: mdl.TaskRequest(
                category="clean",
                description="running clean",
                labels=["priority=normal"],
            ),
        }

        task_repo = AsyncMock(spec=TaskRepository)
        task_repo.get_task_request.side_effect = lambda task_id: requests.get(task_id)

        victim = portal.call(
            tasks_route._select_preemption_victim,
            candidates,
            "incoming-critical-clean-id",
            2,
            "clean",
            task_repo,
        )

        self.assertIsNotNone(victim)
        self.assertEqual(clean_id, victim.booking.id)

    def test_requeue_skips_schedule_runs(self):
        portal = self.get_portal()
        victim_task_id = str(uuid4())

        task_repo = AsyncMock(spec=TaskRepository)
        task_repo.get_task_request.return_value = mdl.TaskRequest(
            category="patrol",
            description="schedule run",
            labels=["scheduled_schedule_id=3", "priority=normal"],
        )
        fleet_repo = AsyncMock()
        service_call = AsyncMock()

        with patch.object(tasks_route, "tasks_service") as mock_tasks_service:
            mock_tasks_service.return_value.call = service_call
            portal.call(
                tasks_route._requeue_preempted_task,
                victim_task_id,
                "critical-id",
                task_repo,
                fleet_repo,
            )

        service_call.assert_not_called()

    def test_wait_for_robot_stop_uses_task_id_release(self):
        portal = self.get_portal()
        fleet_name = "test_fleet"
        robot_name = "test_robot"
        paused_task_id = str(uuid4())
        released_task_id = str(uuid4())

        fleet_states = [
            mdl.FleetState(
                name=fleet_name,
                robots={
                    robot_name: mdl.RobotState(
                        name=robot_name,
                        status="working",
                        task_id=paused_task_id,
                    )
                },
            ),
            mdl.FleetState(
                name=fleet_name,
                robots={
                    robot_name: mdl.RobotState(
                        name=robot_name,
                        status="working",
                        task_id=released_task_id,
                    )
                },
            ),
        ]

        fleet_repo = AsyncMock()
        fleet_repo.get_fleet_state.side_effect = fleet_states

        ready, status = portal.call(
            tasks_route._wait_for_robot_stop,
            fleet_repo,
            fleet_name,
            robot_name,
            paused_task_id,
            0.5,
        )

        self.assertTrue(ready)
        self.assertEqual("working", status)

    def test_fail_with_multiple_errors(self):
        # fails with multiple errors
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = """{
                "success": false,
                "errors": [
                    { "code": 1, "category": "test_error_1", "detail": "detail 1" },
                    { "code": 2, "category": "test_error_2", "detail": "detail 2" }
                ]
            }
            """
            resp = self.post_task_request()
            self.assertEqual(400, resp.status_code, resp.content)

    def test_fail_with_no_errors(self):
        # fails with multiple errors
        with patch.object(tasks_service(), "call") as mock:
            mock.return_value = """{
                "success": false
            }
            """
            resp = self.post_task_request()
            self.assertEqual(400, resp.status_code, resp.content)

    def test_critical_label_spawns_watcher(self):
        task_id = str(uuid4())
        captured = {"coro": None}

        def fake_spawn(coro):
            captured["coro"] = coro
            return None

        with patch.object(
            tasks_route, "tasks_service"
        ) as mock_tasks_service, patch.object(
            tasks_route, "_spawn_background_task", side_effect=fake_spawn
        ) as mock_spawn:
            mock_tasks_service.return_value.call = AsyncMock(
                return_value=(
                    f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'
                )
            )
            resp = self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="test",
                        description="description",
                        labels=["priority=critical"],
                    ),
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)
            mock_spawn.assert_called_once()
            sent_payload = json.loads(
                mock_tasks_service.return_value.call.await_args.args[0]
            )
            self.assertEqual(
                {"type": "binary", "value": 2},
                sent_payload["request"]["priority"],
            )
        if captured["coro"] is not None:
            captured["coro"].close()

    def test_urgent_priority_does_not_spawn_watcher(self):
        task_id = str(uuid4())

        with patch.object(
            tasks_route, "tasks_service"
        ) as mock_tasks_service, patch.object(
            tasks_route, "_spawn_background_task"
        ) as mock_spawn:
            mock_tasks_service.return_value.call = AsyncMock(
                return_value=(
                    f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'
                )
            )
            resp = self.client.post(
                "/tasks/dispatch_task",
                content=mdl.DispatchTaskRequest(
                    type="dispatch_task_request",
                    request=mdl.TaskRequest(
                        category="test",
                        description="description",
                        labels=["priority=urgent"],
                    ),
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)
            mock_spawn.assert_not_called()

        sent_payload = json.loads(
            mock_tasks_service.return_value.call.await_args.args[0]
        )
        self.assertEqual(
            {"type": "binary", "value": 1},
            sent_payload["request"]["priority"],
        )

    def test_extract_assigned_robot_name_from_dispatch_assignment(self):
        task_state = mdl.TaskState.model_validate(
            {
                "booking": {"id": str(uuid4())},
                "dispatch": {
                    "status": "dispatched",
                    "assignment": {
                        "fleet_name": "TinyRobot",
                        "expected_robot_name": "TinyRobot1",
                    },
                },
            }
        )
        self.assertEqual(
            "TinyRobot1", tasks_route._extract_assigned_robot_name(task_state)
        )

    def test_extract_assigned_robot_name_falls_back_to_assigned_to(self):
        task_state = mdl.TaskState.model_validate(
            {
                "booking": {"id": str(uuid4())},
                "assigned_to": {"group": "TinyRobot", "name": "TinyRobot1"},
                "status": "underway",
                "dispatch": None,
            }
        )
        self.assertEqual(
            "TinyRobot1", tasks_route._extract_assigned_robot_name(task_state)
        )
