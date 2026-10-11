import ast
import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import api.zenstream.application_routes as application_routes
from api.zenstream.application_routes import WebSocketHub
from app import foreground
from app.foreground import WorkerLaneOverloaded
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


class ForegroundAndSyncplayBoundaryTests(unittest.TestCase):
    def test_all_worker_lanes_bound_admission_and_release_slots(self):
        lanes = (
            ("foreground", foreground.run_foreground),
            ("control", foreground.run_control),
            ("auth", foreground.run_auth),
        )
        for lane, submit in lanes:
            with self.subTest(lane=lane):
                with patch.dict(
                    foreground._lane_slots,
                    {lane: threading.BoundedSemaphore(1)},
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
                            self.assertEqual(
                                foreground.metrics()[lane]["queue_depth"], 0
                            )
                            with self.assertRaises(WorkerLaneOverloaded) as raised:
                                await asyncio.wait_for(submit(lambda: None), 0.25)
                            self.assertEqual(raised.exception.lane, lane)
                            self.assertEqual(raised.exception.status_code, 503)
                            self.assertEqual(
                                raised.exception.detail,
                                "Orchestrator is busy. Retry shortly.",
                            )
                            self.assertEqual(
                                raised.exception.headers["Retry-After"], "1"
                            )
                        finally:
                            release.set()
                        self.assertTrue(await asyncio.wait_for(first, 2))
                        self.assertTrue(await submit(lambda: True))

                        cancelled_started = threading.Event()
                        release_cancelled = threading.Event()

                        def block_cancelled():
                            cancelled_started.set()
                            return release_cancelled.wait(10)

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
                        self.assertEqual(
                            foreground.metrics()[lane]["queue_depth"], 0
                        )

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
            run_control = AsyncMock(
                side_effect=[WorkerLaneOverloaded("control"), [state], []]
            )
            broadcast = AsyncMock()
            sleep = AsyncMock()
            with (
                patch.object(application_routes, "hub", new=hub),
                patch.object(application_routes, "run_control", new=run_control),
                patch.object(application_routes, "_broadcast_group", new=broadcast),
                patch.object(application_routes.asyncio, "sleep", new=sleep),
            ):
                tasks_before = asyncio.all_tasks()
                await application_routes._disconnect_cleanup(
                    user, participant, epoch
                )
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
            broadcast.assert_awaited_once_with(
                state, identity=(user, participant), epoch=epoch
            )
            sleep.assert_has_awaits([call(30), call(0.25), call(270)])
            self.assertEqual(hub._lifecycle_tasks, {})
            self.assertEqual(hub._disconnect_tasks, {})

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
