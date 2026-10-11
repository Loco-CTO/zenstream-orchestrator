import ast
import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from api.zenstream import application_routes
from api.zenstream.application_routes import WebSocketHub
from app import foreground
from app.foreground import WorkerLaneOverloaded, run_control
from app.models.syncplay import SyncplayGroup


class _FakeWebSocket:
    def __init__(self, stalled=False):
        self.stalled = stalled
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.messages = []

    @staticmethod
    async def accept():
        return None

    async def send_json(self, payload):
        if self.stalled:
            self.started.set()
            await self.release.wait()
        self.messages.append(payload)


class _RouteWebSocket:
    def __init__(self, participant="tab-1", incoming=None):
        self.headers = {"x-zenstream-participant": participant}
        self.query_params = {}
        self.accept_count = 0
        self.closed = []
        self.incoming = list(incoming or [])

    async def accept(self):
        self.accept_count += 1

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))

    async def receive_json(self):
        if not self.incoming:
            raise application_routes.WebSocketDisconnect()
        return self.incoming.pop(0)


class ForegroundAndSyncplayBoundaryTests(unittest.TestCase):
    def test_all_worker_lanes_bound_admission_and_release_slots(self):
        lanes = (
            ("foreground", foreground.run_foreground),
            ("control", foreground.run_control),
            ("auth", foreground.run_auth),
        )
        for lane, submit in lanes:
            with (
                self.subTest(lane=lane),
                patch.dict(
                    foreground._lane_slots,
                    {lane: threading.BoundedSemaphore(1)},
                ),
            ):

                async def exercise():
                    started = threading.Event()
                    release = threading.Event()

                    def block():
                        started.set()
                        return release.wait(10)

                    first = asyncio.create_task(submit(block))
                    try:
                        self.assertTrue(
                            await asyncio.wait_for(
                                asyncio.to_thread(started.wait, 2), 3
                            )
                        )
                        self.assertEqual(foreground.metrics()[lane]["queue_depth"], 0)
                        with self.assertRaises(WorkerLaneOverloaded) as raised:
                            await asyncio.wait_for(submit(lambda: None), 0.25)
                        self.assertEqual(raised.exception.lane, lane)
                        self.assertEqual(raised.exception.status_code, 503)
                        self.assertEqual(
                            raised.exception.detail,
                            "Orchestrator is busy. Retry shortly.",
                        )
                        self.assertEqual(raised.exception.headers["Retry-After"], "1")
                    finally:
                        release.set()
                    self.assertTrue(await asyncio.wait_for(first, 2))
                    self.assertTrue(await submit(lambda: True))

                    cancelled_started = threading.Event()
                    release_cancelled = threading.Event()

                    def block_cancelled():
                        cancelled_started.set()
                        release_cancelled.wait(10)
                        raise RuntimeError("worker failed after request cancellation")

                    cancelled = asyncio.create_task(submit(block_cancelled))
                    try:
                        self.assertTrue(
                            await asyncio.wait_for(
                                asyncio.to_thread(cancelled_started.wait, 2), 3
                            )
                        )
                        cancelled.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(cancelled, 1)
                        with self.assertRaises(WorkerLaneOverloaded):
                            await asyncio.wait_for(submit(lambda: None), 0.25)
                    finally:
                        release_cancelled.set()
                        await foreground.wait_for_shutdown(timeout=2)
                    self.assertTrue(await submit(lambda: True))
                    self.assertEqual(foreground.metrics()[lane]["queue_depth"], 0)

                asyncio.run(exercise())

    def test_worker_submission_failure_releases_queue_and_admission(self):
        async def exercise():
            executor = MagicMock()
            executor.submit.side_effect = RuntimeError("executor is shutting down")
            slot = threading.BoundedSemaphore(1)
            with patch.dict(foreground._lane_slots, {"foreground": slot}):
                with self.assertRaisesRegex(RuntimeError, "shutting down"):
                    await foreground._run("foreground", executor, lambda: None)

            self.assertEqual(foreground.metrics()["foreground"]["queue_depth"], 0)
            self.assertTrue(slot.acquire(blocking=False))
            slot.release()

        asyncio.run(exercise())

    def test_disconnect_cleanup_retries_overload_until_transition_succeeds(self):
        async def exercise():
            user = "user-1"
            participant = "tab-1"
            key = (user, participant)
            hub = WebSocketHub()
            async with hub.lock:
                epoch = hub._advance_epoch_locked(key)
                hub._disconnect_tasks[key] = asyncio.current_task()
            state = {"id": "group-1", "revision": 3}
            group_state = {"id": "group-2", "revision": 5}
            ended_state = {"id": "group-3", "revision": 6}
            run_control = AsyncMock(
                side_effect=[
                    WorkerLaneOverloaded("control"),
                    [state],
                    [("group", group_state), ("ended", ended_state)],
                ]
            )
            broadcast = AsyncMock()
            wire_broadcast = AsyncMock()
            hub.broadcast = wire_broadcast
            sleep = AsyncMock()
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(application_routes, "run_control", new=run_control),
                patch.object(application_routes, "_broadcast_group", new=broadcast),
                patch.object(application_routes.asyncio, "sleep", new=sleep),
            ):
                tasks_before = asyncio.all_tasks()
                await application_routes._disconnect_cleanup(user, participant, epoch)
                self.assertEqual(asyncio.all_tasks(), tasks_before)

            self.assertEqual(run_control.await_count, 3)
            self.assertIs(
                run_control.await_args_list[0].args[0],
                application_routes._mark_disconnected_sync,
            )
            self.assertIs(
                run_control.await_args_list[1].args[0],
                application_routes._mark_disconnected_sync,
            )
            self.assertIs(
                run_control.await_args_list[2].args[0],
                application_routes._expire_disconnected_sync,
            )
            broadcast.assert_has_awaits(
                [
                    call(state, identity=(user, participant), epoch=epoch),
                    call(group_state, identity=(user, participant), epoch=epoch),
                ]
            )
            wire_broadcast.assert_awaited_once_with(
                {
                    "version": 1,
                    "type": "group-ended",
                    "id": ended_state["id"],
                    "revision": ended_state["revision"],
                },
                identity=(user, participant),
                epoch=epoch,
            )
            sleep.assert_has_awaits([call(30), call(0.25), call(270)])
            self.assertEqual(hub._lifecycle_tasks, {})
            self.assertEqual(hub._disconnect_tasks, {})

        asyncio.run(exercise())

    def test_lifecycle_rejects_stale_epochs_and_runs_current_socket_work(self):
        async def exercise():
            user = "user-1"
            participant = "tab-1"
            key = (user, participant)
            hub = WebSocketHub()
            state = {"id": "group-1", "revision": 4}
            run_control = AsyncMock(return_value=([state], [state]))
            logger = MagicMock()
            socket = _FakeWebSocket()
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(application_routes, "run_control", new=run_control),
                patch.object(
                    application_routes, "get_logger", return_value=logger
                ),
            ):
                self.assertIsNone(
                    await hub.run_lifecycle(
                        user,
                        participant,
                        0,
                        application_routes._mark_disconnected_sync,
                    )
                )
                async with hub.lock:
                    epoch = hub._advance_epoch_locked(key)
                self.assertIsNone(
                    await hub.run_lifecycle(
                        user,
                        participant,
                        epoch + 1,
                        application_routes._mark_disconnected_sync,
                    )
                )
                self.assertIsNone(
                    await hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        application_routes._mark_disconnected_sync,
                        connected=True,
                    )
                )
                token = hub._epoch_tokens[key]
                token.set()
                self.assertIsNone(
                    await hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        application_routes._mark_disconnected_sync,
                    )
                )
                token.clear()
                hub._shutting_down = True
                self.assertIsNone(
                    await hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        application_routes._mark_disconnected_sync,
                    )
                )
                hub._shutting_down = False

                epoch = await hub.connect(socket, user, participant)
                self.assertIsNone(
                    await hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        application_routes._mark_disconnected_sync,
                    )
                )
                self.assertEqual(
                    await hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        application_routes._syncplay_socket_initial_sync,
                        connected=True,
                    ),
                    ([state], [state]),
                )
                run_control.assert_awaited_once()
                self.assertEqual(
                    logger.debug.call_args.args[-1], [(state["id"], state["revision"])]
                )
                await hub.remove(socket)
                await hub.shutdown()

        asyncio.run(exercise())

    def test_lifecycle_waiting_on_serial_lock_skips_superseded_work(self):
        async def exercise():
            user = "user-1"
            participant = "tab-1"
            key = (user, participant)
            hub = WebSocketHub()
            async with hub.lock:
                epoch = hub._advance_epoch_locked(key)
                serial = asyncio.Lock()
                await serial.acquire()
                hub._lifecycle_locks[key] = serial
            run_control = AsyncMock(return_value=[])
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(application_routes, "run_control", new=run_control),
            ):
                operation = asyncio.create_task(
                    hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        application_routes._mark_disconnected_sync,
                    )
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                hub._epoch_tokens[key].set()
                serial.release()
                self.assertIsNone(await operation)

            run_control.assert_not_awaited()
            self.assertEqual(hub._lifecycle_tasks, {})

        asyncio.run(exercise())

    def test_lifecycle_control_errors_propagate_and_release_tracking(self):
        async def exercise():
            user = "user-1"
            participant = "tab-1"
            key = (user, participant)
            hub = WebSocketHub()
            async with hub.lock:
                epoch = hub._advance_epoch_locked(key)
            run_control = AsyncMock(side_effect=RuntimeError("database failed"))
            logger = MagicMock()
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(application_routes, "run_control", new=run_control),
                patch.object(application_routes, "get_logger", return_value=logger),
            ):
                with self.assertRaisesRegex(RuntimeError, "database failed"):
                    await hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        application_routes._mark_disconnected_sync,
                    )

            logger.exception.assert_called_once()
            self.assertEqual(hub._lifecycle_tasks, {})
            self.assertNotIn(key, hub._epoch_tokens)

        asyncio.run(exercise())

    def test_syncplay_socket_closes_auth_overload_and_invalid_identity(self):
        async def exercise():
            overloaded = _RouteWebSocket()
            with patch.object(
                application_routes,
                "run_auth",
                new=AsyncMock(side_effect=WorkerLaneOverloaded("auth")),
            ):
                await application_routes.syncplay_socket(overloaded)
            self.assertEqual(overloaded.accept_count, 1)
            self.assertEqual(
                overloaded.closed,
                [(1013, "Orchestrator is busy. Retry shortly.")],
            )

            invalid = _RouteWebSocket("tab-\x00-1")
            with patch.object(
                application_routes,
                "run_auth",
                new=AsyncMock(return_value={"id": "user-1"}),
            ):
                await application_routes.syncplay_socket(invalid)
            self.assertEqual(invalid.accept_count, 0)
            self.assertEqual(invalid.closed, [(1008, "")])

        asyncio.run(exercise())

    def test_syncplay_socket_closes_on_control_overload_and_cleans_up(self):
        async def exercise():
            socket = _RouteWebSocket()
            hub = WebSocketHub()
            schedule_cleanup = AsyncMock()
            hub.schedule_disconnect_cleanup = schedule_cleanup
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(
                    application_routes,
                    "run_auth",
                    new=AsyncMock(return_value={"id": "user-1"}),
                ),
                patch.object(
                    application_routes,
                    "run_control",
                    new=AsyncMock(side_effect=WorkerLaneOverloaded("control")),
                ),
            ):
                await application_routes.syncplay_socket(socket)
                self.assertEqual(await hub.sockets_for("user-1", "tab-1"), ())
                await hub.shutdown()

            self.assertEqual(socket.accept_count, 1)
            self.assertEqual(
                socket.closed,
                [(1013, "Orchestrator is busy. Retry shortly.")],
            )
            schedule_cleanup.assert_awaited_once_with("user-1", "tab-1", 2)

        asyncio.run(exercise())

    def test_syncplay_socket_initializes_broadcasts_and_answers_clock(self):
        async def exercise():
            socket = _RouteWebSocket(
                incoming=[{"type": "clock", "clientSentAt": 1234}]
            )
            hub = WebSocketHub()
            state = {"id": "group-1", "revision": 4}
            hub.run_lifecycle = AsyncMock(return_value=([state], [state]))
            hub.finish_initial = AsyncMock(return_value=True)
            hub.send = AsyncMock(return_value=True)
            schedule_cleanup = AsyncMock()
            hub.schedule_disconnect_cleanup = schedule_cleanup
            broadcast = AsyncMock()
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(
                    application_routes,
                    "run_auth",
                    new=AsyncMock(return_value={"id": "user-1"}),
                ),
                patch.object(
                    application_routes, "_broadcast_group", new=broadcast
                ),
            ):
                await application_routes.syncplay_socket(socket)
                await hub.shutdown()

            self.assertEqual(socket.accept_count, 1)
            broadcast.assert_awaited_once_with(
                state, identity=("user-1", "tab-1"), epoch=1
            )
            hub.finish_initial.assert_awaited_once_with(
                socket, {"version": 1, "type": "groups", "groups": [state]}
            )
            hub.send.assert_awaited_once()
            schedule_cleanup.assert_awaited_once_with("user-1", "tab-1", 2)

        asyncio.run(exercise())

    def test_syncplay_socket_stops_when_initial_lifecycle_is_stale(self):
        async def exercise():
            socket = _RouteWebSocket(incoming=[{"type": "clock"}])
            hub = WebSocketHub()
            hub.run_lifecycle = AsyncMock(return_value=None)
            hub.finish_initial = AsyncMock(return_value=True)
            schedule_cleanup = AsyncMock()
            hub.schedule_disconnect_cleanup = schedule_cleanup
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(
                    application_routes,
                    "run_auth",
                    new=AsyncMock(return_value={"id": "user-1"}),
                ),
            ):
                await application_routes.syncplay_socket(socket)
                await hub.shutdown()

            self.assertEqual(socket.incoming, [{"type": "clock"}])
            hub.finish_initial.assert_not_awaited()
            schedule_cleanup.assert_awaited_once_with("user-1", "tab-1", 2)

        asyncio.run(exercise())

    def test_disconnect_retry_stops_when_lifecycle_is_superseded(self):
        async def exercise():
            user = "user-1"
            participant = "tab-1"
            key = (user, participant)
            hub = WebSocketHub()
            async with hub.lock:
                epoch = hub._advance_epoch_locked(key)
                hub._disconnect_tasks[key] = asyncio.current_task()
            run_control = AsyncMock(side_effect=WorkerLaneOverloaded("control"))

            async def reconnect(delay):
                self.assertEqual(delay, 0.25)
                async with hub.lock:
                    self.assertEqual(hub._advance_epoch_locked(key), epoch + 1)

            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(application_routes, "run_control", new=run_control),
                patch.object(application_routes.asyncio, "sleep", new=reconnect),
            ):
                tasks_before = asyncio.all_tasks()
                result = await application_routes._run_disconnect_lifecycle(
                    user,
                    participant,
                    epoch,
                    application_routes._mark_disconnected_sync,
                )
                self.assertIsNone(result)
                self.assertEqual(asyncio.all_tasks(), tasks_before)
                await hub.finish_disconnect_cleanup(
                    user, participant, epoch, asyncio.current_task()
                )

            run_control.assert_awaited_once()
            self.assertEqual(hub._lifecycle_tasks, {})
            self.assertEqual(hub._disconnect_tasks, {})

        asyncio.run(exercise())

    def test_control_work_does_not_stop_the_event_loop(self):
        gate = threading.Event()

        async def exercise():
            task = asyncio.create_task(run_control(gate.wait, 2))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            gate.set()
            self.assertTrue(await task)

        asyncio.run(exercise())

    def test_syncplay_state_and_member_use_reader_path(self):
        group = object.__new__(SyncplayGroup)
        group.id = "missing-group"
        database = MagicMock()
        database.read_execute.return_value = []
        group.db = database
        with patch.object(database, "transaction") as writer:
            self.assertIsNone(group.state())
            self.assertFalse(group.member("user", "participant"))
        writer.assert_not_called()

    def test_syncplay_group_inventory_reads_use_reader_path(self):
        database = MagicMock()
        database.read_execute.return_value = [("group-1",)]

        def initialize(group, group_id):
            group.id = group_id
            group.db = database

        with patch.object(SyncplayGroup, "__init__", initialize):
            groups = SyncplayGroup.active_groups_for_user("user", "participant")

        self.assertEqual([group.id for group in groups], ["group-1"])
        database.execute.assert_not_called()

    def test_stalled_socket_isolated_by_bounded_client_queue(self):
        async def exercise():
            hub = WebSocketHub()
            stalled = _FakeWebSocket(stalled=True)
            fast = _FakeWebSocket()
            await hub.connect(stalled, "user-1", "tab-1")
            await hub.connect(fast, "user-2", "tab-2")
            await hub.broadcast({"version": 1, "type": "presence"})
            await asyncio.wait_for(stalled.started.wait(), 1)
            for revision in range(160):
                await asyncio.wait_for(
                    hub.broadcast(
                        {
                            "version": 1,
                            "type": "group",
                            "group": {
                                "id": "group-1",
                                "revision": revision,
                                "members": [],
                            },
                        }
                    ),
                    1,
                )
            await asyncio.sleep(0.05)
            metrics = await hub.queue_metrics()
            self.assertGreater(metrics["queue_overflows"], 0)
            self.assertGreater(len(fast.messages), 0)
            await hub.remove(stalled)
            await hub.remove(fast)

        asyncio.run(exercise())


class AsyncRouteBlockingGuardTests(unittest.TestCase):
    """Keep synchronous DB/filesystem calls out of top-level async handlers."""

    _ROUTE_FILES = (
        "api/zenstream/application_routes.py",
        "api/zenstream/client_routes.py",
        "api/zenstream/library_routes.py",
        "app/app.py",
    )
    _BRIDGES = {"run_control", "run_auth", "run_foreground", "to_thread"}
    _BLOCKING_ATTRIBUTES = {
        "execute",
        "read_execute",
        "transaction",
        "write",
        "stat",
        "is_file",
        "mkdir",
        "resolve",
        "open",
        "exists",
        "unlink",
        "read_text",
        "read_bytes",
        "write_text",
        "replace",
    }
    _DOMAIN_ATTRIBUTES = {
        "authenticate_password",
        "authenticate_token",
        "create_session",
        "revoke",
        "login",
        "logout",
        "profile",
        "update_profile",
        "list_accounts",
        "set_disabled",
        "set_library_ids",
        "set_password",
        "change_password",
        "register",
        "validate",
        "definitions",
        "runs",
        "library_runs",
        "definition",
        "update_definition",
        "add_trigger",
        "remove_trigger",
        "run_now",
        "terminate",
        "terminate_library",
        "refresh_watchers",
        "enqueue",
        "job",
        "sources",
        "lyrics",
        "jobs",
        "require_entity",
        "allowed_libraries",
        "update_state",
        "source_metadata",
        "refresh_access",
        "direct_path",
        "session_file",
        "session_status",
        "cancel_session",
        "manifest",
        "sheet_path",
        "segments",
        "inspection",
        "preview_clip",
        "configured",
        "language_options",
        "client",
        "search",
        "refresh_roots",
        "resolve_raw",
        "settings",
        "clear_segments",
        "update_settings",
        "enqueue_intro_outro_detection",
        "mutate",
        "member",
        "state",
        "set_participation",
        "apply_presence",
        "reconcile_readiness",
        "version",
        "save",
        "remove",
        "resolve",
    }
    _DOMAIN_ROOTS = {
        "Account",
        "Admin",
        "Invite",
        "LibraryStore",
        "SyncplayGroup",
        "PlaybackSettings",
        "MetadataLanguageSettings",
        "MetadataService",
        "MetadataReadService",
        "MetadataIngestService",
        "TrickplayExtractor",
        "IntroOutroStore",
        "LocalArtworkCache",
        "catalog",
        "credentials",
        "group",
        "media",
        "runtime",
        "scheduler",
        "store",
        "trickplay",
        "intro_outro",
        "UserAvatarStore",
    }

    @staticmethod
    def _root_name(expression):
        while isinstance(expression, (ast.Attribute, ast.Subscript)):
            expression = expression.value
        if isinstance(expression, ast.Call):
            expression = expression.func
        return expression.id if isinstance(expression, ast.Name) else None

    def _violations(self, tree):
        violations = []

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.async_handler = None
                self.bridge_depth = 0

            def visit_AsyncFunctionDef(self, node):
                if self.async_handler is not None:
                    return
                previous = self.async_handler
                self.async_handler = node.name
                for statement in node.body:
                    self.visit(statement)
                self.async_handler = previous

            def visit_FunctionDef(self, node):
                # Nested synchronous helpers are worker callables in the
                # migrated handlers; inspect them through their bridge call.
                if self.async_handler is not None:
                    return
                self.generic_visit(node)

            def visit_Call(self, node):
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name in AsyncRouteBlockingGuardTests._BRIDGES:
                    self.bridge_depth += 1
                    self.generic_visit(node)
                    self.bridge_depth -= 1
                    return
                attribute = (
                    node.func.attr if isinstance(node.func, ast.Attribute) else None
                )
                root = (
                    AsyncRouteBlockingGuardTests._root_name(node.func.value)
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                if (
                    self.async_handler is not None
                    and self.bridge_depth == 0
                    and (
                        attribute in AsyncRouteBlockingGuardTests._BLOCKING_ATTRIBUTES
                        or (
                            attribute in AsyncRouteBlockingGuardTests._DOMAIN_ATTRIBUTES
                            and root in AsyncRouteBlockingGuardTests._DOMAIN_ROOTS
                        )
                    )
                ):
                    violations.append((self.async_handler, attribute, node.lineno))
                self.generic_visit(node)

        Visitor().visit(tree)
        return violations

    def test_route_handlers_use_the_blocking_work_bridge(self):
        root = Path(__file__).resolve().parents[1]
        violations = []
        for relative in self._ROUTE_FILES:
            source_path = root / relative
            tree = ast.parse(source_path.read_text(encoding="utf-8"), str(source_path))
            violations.extend((relative, *value) for value in self._violations(tree))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
