import ast
import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from api.zenstream.application_routes import WebSocketHub
from app.foreground import run_control
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
