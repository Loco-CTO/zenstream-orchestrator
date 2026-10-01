import asyncio
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from api.zenstream import application_routes as routes
from app.config import Config
from app.database import DatabaseHandler
from app.models.syncplay import (
    StaleSyncplayState,
    SyncplayGroup,
    SyncplayMembershipConflict,
    pause,
    schedule,
)


class SyncplayModelTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.previous_database = self.config._database
        self.temp_directory = tempfile.TemporaryDirectory()
        self.config._database = DatabaseHandler(
            "sqlite",
            {
                "sqlite": {
                    "syncplay_groups": {
                        "create": "CREATE TABLE syncplay_groups (id TEXT PRIMARY KEY, host_user_id TEXT NOT NULL, host_name TEXT NOT NULL, allow_controls INTEGER NOT NULL DEFAULT 0, item_id TEXT, position REAL NOT NULL DEFAULT 0, playing INTEGER NOT NULL DEFAULT 0, resume INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, timeline_revision INTEGER NOT NULL DEFAULT 0, media_generation INTEGER NOT NULL DEFAULT 0, anchor_position REAL NOT NULL DEFAULT 0, anchor_time REAL NOT NULL DEFAULT 0, effective_at REAL NOT NULL DEFAULT 0, playback_state TEXT NOT NULL DEFAULT 'paused', pause_reason TEXT, host_disconnected_at REAL, ended INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)",
                        "columns": {},
                    },
                    "syncplay_members": {
                        "create": "CREATE TABLE syncplay_members (group_id TEXT NOT NULL, user_id TEXT NOT NULL, participant_id TEXT NOT NULL DEFAULT 'legacy', username TEXT NOT NULL, watching_together INTEGER NOT NULL DEFAULT 1, viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0, ready_generation INTEGER NOT NULL DEFAULT -1, presence_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, participant_id))",
                        "columns": {},
                    },
                    "syncplay_operations": {
                        "create": "CREATE TABLE syncplay_operations (operation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, state TEXT NOT NULL)",
                        "columns": {},
                    },
                }
            },
            db_file=f"{self.temp_directory.name}/syncplay.db",
        )
        for table in self.config._database.create_query["sqlite"].values():
            self.config._database.execute(table["create"])

    def tearDown(self):
        self.config._database.close()
        self.config._database = self.previous_database
        self.temp_directory.cleanup()

    def test_lifecycle_guards_run_after_transaction_admission(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        before = group.state()
        original = group.db.transaction
        admitted = False

        @contextmanager
        def transaction():
            nonlocal admitted
            with original() as cursor:
                admitted = True
                try:
                    yield cursor
                finally:
                    admitted = False

        def guard():
            self.assertTrue(admitted)
            return False

        with patch.object(group.db, "transaction", transaction):
            self.assertIsNone(group.mark_host_disconnected(guard=guard))
            self.assertIsNone(group.clear_host_disconnected(guard=guard))
            self.assertIsNone(
                group.mark_member_backgrounded("host", "host-tab", guard=guard)
            )
            self.assertIsNone(
                group.remove_disconnected_member("host", "host-tab", guard=guard)
            )
            self.assertIsNone(group.expire_host_disconnect(guard=guard))
        self.assertEqual(group.state(), before)

    def _exercise_cancelled_cleanup(self, user, expire):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username,viewing,loading,ready_generation) VALUES (?,?,?,?,1,0,1)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            )
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE group_id=?",
                (group.id,),
            )
            group.transition(cursor, state, item_id="movie", media_generation=1)

        group.mutate("host", None, None, prepare)
        if expire and user == "host":
            group.mark_host_disconnected()
            group.db.execute(
                "UPDATE syncplay_groups SET host_disconnected_at=? WHERE id=?",
                (time.time() - 400, group.id),
            )
        started = threading.Event()
        release = threading.Event()
        original = group.db.transaction
        first = [True]

        @contextmanager
        def delayed_transaction():
            if threading.current_thread() is not threading.main_thread() and first:
                first.pop()
                started.set()
                if not release.wait(5):
                    raise TimeoutError("cleanup worker was not released")
            with original() as cursor:
                yield cursor

        class Socket:
            @staticmethod
            async def accept():
                return None

            @staticmethod
            async def send_json(payload):
                return None

            @staticmethod
            async def close(**kwargs):
                return None

        async def scenario():
            hub = routes.WebSocketHub()
            old, new, other = Socket(), Socket(), Socket()
            participant = user + "-tab"
            await hub.connect(old, user, participant)
            _, old_epoch = await hub.remove(old)
            operation = (
                routes._expire_disconnected_sync
                if expire
                else routes._mark_disconnected_sync
            )
            waiting = asyncio.create_task(
                hub.run_lifecycle(user, participant, old_epoch, operation)
            )
            initial = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
                epoch = await hub.connect(new, user, participant)
                initial = asyncio.create_task(
                    hub.run_lifecycle(
                        user,
                        participant,
                        epoch,
                        routes._syncplay_socket_initial_sync,
                        connected=True,
                    )
                )
                await asyncio.sleep(0.02)
                self.assertFalse(initial.done())
                other_epoch = await hub.connect(other, "unrelated", "other-tab")
                await asyncio.wait_for(
                    hub.run_lifecycle(
                        "unrelated",
                        "other-tab",
                        other_epoch,
                        routes._syncplay_socket_initial_sync,
                        connected=True,
                    ),
                    1,
                )
                release.set()
                result = await asyncio.wait_for(initial, 2)
                self.assertIsNotNone(result)
                restored = group.state()
                self.assertIsNotNone(restored)
                self.assertIsNone(restored["hostDisconnectedAt"])
                self.assertTrue(group.member(user, participant))
                member = next(
                    value for value in restored["members"] if value["userId"] == user
                )
                if not (expire and user == "host"):
                    self.assertFalse(member["loading"])
                self.assertEqual(await hub.sockets_for(user, participant), (new,))
            finally:
                release.set()
                if initial is not None:
                    await asyncio.gather(initial, return_exceptions=True)
                await hub.shutdown()
            self.assertFalse(hub._lifecycle_tasks)
            self.assertFalse(hub._lifecycle_locks)
            self.assertFalse(hub._epoch_tokens)

        with patch.object(group.db, "transaction", delayed_transaction):
            asyncio.run(scenario())

    def test_cancelled_host_cleanup_cannot_overwrite_reconnect(self):
        self._exercise_cancelled_cleanup("host", expire=False)

    def test_cancelled_viewer_cleanup_cannot_overwrite_reconnect(self):
        self._exercise_cancelled_cleanup("viewer", expire=False)

    def test_cancelled_host_expiry_cannot_end_reconnected_group(self):
        self._exercise_cancelled_cleanup("host", expire=True)

    def test_cancelled_viewer_expiry_cannot_remove_reconnected_member(self):
        self._exercise_cancelled_cleanup("viewer", expire=True)

    def test_guest_socket_initialization_does_not_clear_host_disconnect(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        group.mutate(
            "viewer",
            None,
            None,
            lambda cursor, state: cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            ),
        )
        marked = group.mark_host_disconnected()
        changed, _ = routes._syncplay_socket_initial_sync("viewer", "viewer-tab")
        self.assertEqual(changed, [])
        self.assertEqual(
            group.state()["hostDisconnectedAt"], marked["hostDisconnectedAt"]
        )
        changed, _ = routes._syncplay_socket_initial_sync("host", "host-tab")
        self.assertEqual(len(changed), 1)
        self.assertIsNone(group.state()["hostDisconnectedAt"])
        self.assertFalse(group.state()["playing"])

    def test_user_can_have_only_one_active_group(self):
        SyncplayGroup.create("host", "Host")
        with self.assertRaises(SyncplayMembershipConflict):
            SyncplayGroup.create("host", "Host")

    def test_host_disconnect_is_paused_then_expires(self):
        group = SyncplayGroup.create("host", "Host")
        marked = group.mark_host_disconnected()
        self.assertEqual(marked["playbackState"], "paused")
        self.assertIsNotNone(marked["hostDisconnectedAt"])
        self.assertIsNone(
            group.expire_host_disconnect(marked["hostDisconnectedAt"] + 299)
        )
        self.assertTrue(
            group.expire_host_disconnect(marked["hostDisconnectedAt"] + 300)["ended"]
        )

    def test_host_reconnect_clears_disconnect_without_resuming(self):
        group = SyncplayGroup.create("host", "Host")
        group.mark_host_disconnected()
        reconnected = group.clear_host_disconnected()
        self.assertIsNone(reconnected["hostDisconnectedAt"])
        self.assertFalse(reconnected["playing"])

    def test_viewer_disconnect_removes_only_viewer(self):
        group = SyncplayGroup.create("host", "Host")
        group.mutate(
            "viewer",
            None,
            None,
            lambda cursor, state: cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            ),
        )
        state = group.remove_disconnected_member("viewer", "viewer-tab")
        self.assertEqual([member["userId"] for member in state["members"]], ["host"])
        self.assertFalse(state["ended"])

    def test_stale_lifecycle_release_clears_current_member_barrier(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=1,ready_generation=-1 WHERE group_id=?",
                (group.id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=1,
                anchor_position=10,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="playing",
            )

        before = group.mutate("host", None, None, prepare)

        def release(cursor, state):
            self.assertTrue(
                group.apply_presence(
                    cursor,
                    state,
                    "host",
                    "host-tab",
                    generation=0,
                    timeline_revision=0,
                    sequence=1,
                    viewing=False,
                    loading=False,
                )
            )

        released = group.mutate("host", None, None, release)
        member = released["members"][0]
        self.assertFalse(member["viewing"])
        self.assertFalse(member["loading"])
        self.assertEqual(released["timelineRevision"], before["timelineRevision"])
        self.assertTrue(released["playing"])

    def test_background_presence_pauses_until_host_resumes_after_member_returns(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        group.mutate(
            "viewer",
            None,
            None,
            lambda cursor, state: cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            ),
        )

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE group_id=?",
                (group.id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=1,
                anchor_position=10,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="playing",
            )

        group.mutate("host", None, None, prepare)

        def background(cursor, state):
            self.assertTrue(
                group.apply_presence(
                    cursor,
                    state,
                    "viewer",
                    "viewer-tab",
                    generation=0,
                    timeline_revision=0,
                    sequence=1,
                    viewing=False,
                    loading=False,
                    pause_room=True,
                )
            )

        paused = group.mutate("viewer", None, None, background)
        viewer = next(
            member for member in paused["members"] if member["userId"] == "viewer"
        )
        self.assertFalse(paused["playing"])
        self.assertFalse(paused["resumeWhenReady"])
        self.assertEqual(paused["pauseReason"], "background")
        self.assertFalse(viewer["viewing"])
        self.assertTrue(viewer["loading"])

        def return_ready(cursor, state):
            self.assertTrue(
                group.apply_presence(
                    cursor,
                    state,
                    "viewer",
                    "viewer-tab",
                    generation=state["mediaGeneration"],
                    timeline_revision=state["timelineRevision"],
                    sequence=2,
                    viewing=True,
                    loading=False,
                )
            )

        ready = group.mutate("viewer", None, None, return_ready)
        self.assertFalse(ready["playing"])
        self.assertFalse(ready["resumeWhenReady"])

        def host_resume(cursor, state):
            self.assertFalse(
                group.waiting_for_members(cursor, state["mediaGeneration"])
            )
            schedule(group, cursor, state, state["anchorPosition"])

        resumed = group.mutate("host", None, None, host_resume)
        self.assertTrue(resumed["playing"])

    def test_repeated_background_presence_does_not_change_timeline(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE group_id=?",
                (group.id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=1,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="playing",
            )

        group.mutate("host", None, None, prepare)

        def background(cursor, state, sequence):
            return group.apply_presence(
                cursor,
                state,
                "host",
                "host-tab",
                generation=0,
                timeline_revision=0,
                sequence=sequence,
                viewing=False,
                loading=False,
                pause_room=True,
            )

        paused = group.mutate(
            "host", None, None, lambda cursor, state: background(cursor, state, 1)
        )
        unchanged = group.mutate(
            "host", None, None, lambda cursor, state: background(cursor, state, 2)
        )
        self.assertEqual(unchanged["revision"], paused["revision"])
        self.assertEqual(unchanged["timelineRevision"], paused["timelineRevision"])
        self.assertEqual(unchanged["pauseReason"], "background")

    def test_disconnected_watching_member_gets_background_barrier(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        group.mutate(
            "viewer",
            None,
            None,
            lambda cursor, state: cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            ),
        )

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE group_id=?",
                (group.id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=1,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="playing",
            )

        group.mutate("host", None, None, prepare)
        state = group.mark_member_backgrounded("viewer", "viewer-tab")
        viewer = next(
            member for member in state["members"] if member["userId"] == "viewer"
        )
        self.assertFalse(state["playing"])
        self.assertEqual(state["pauseReason"], "background")
        self.assertTrue(viewer["loading"])

    def test_backgrounding_non_watching_member_does_not_pause_room(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        group.mutate(
            "viewer",
            None,
            None,
            lambda cursor, state: cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username,watching_together) VALUES (?,?,?,?,0)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            ),
        )

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE participant_id='host-tab'"
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=1,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="playing",
            )

        group.mutate("host", None, None, prepare)
        state = group.mark_member_backgrounded("viewer", "viewer-tab")
        self.assertTrue(state["playing"])
        self.assertNotEqual(state["pauseReason"], "background")

    def test_members_default_to_watching_together(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        self.assertTrue(group.state()["members"][0]["watchingTogether"])

    def test_browsing_member_is_excluded_from_initial_readiness(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username,watching_together) VALUES (?,?,?,?,0)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            )
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE participant_id='host-tab'"
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="episode",
                media_generation=1,
                resume=1,
                playback_state="paused",
                pause_reason="readiness",
            )

        group.mutate("host", None, None, prepare)

        def release(cursor, state):
            group.reconcile_readiness(cursor, state)

        state = group.mutate("host", None, None, release)
        self.assertTrue(state["playing"])
        self.assertFalse(state["resumeWhenReady"])

    def test_leaving_initial_barrier_releases_remaining_member(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username,loading) VALUES (?,?,?,?,1)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            )
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE participant_id='host-tab'"
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="episode",
                media_generation=1,
                resume=1,
                playback_state="paused",
                pause_reason="readiness",
            )

        group.mutate("host", None, None, prepare)
        state = group.set_participation("viewer", "viewer-tab", False, "leave-barrier")
        self.assertTrue(state["playing"])
        self.assertFalse(
            next(member for member in state["members"] if member["userId"] == "viewer")[
                "watchingTogether"
            ]
        )

    def test_buffering_after_start_pauses_room_until_ready(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=1,ready_generation=-1 WHERE participant_id='host-tab'"
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=1,
                resume=0,
                playback_state="playing",
                anchor_time=state["updatedAt"],
                effective_at=state["updatedAt"],
            )

        group.mutate("host", None, None, prepare)

        def reconcile(cursor, state):
            group.reconcile_readiness(cursor, state)

        state = group.mutate("host", None, None, reconcile)
        self.assertFalse(state["playing"])
        self.assertTrue(state["resumeWhenReady"])
        self.assertEqual(state["pauseReason"], "buffering")

        def ready(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET loading=0,ready_generation=1 WHERE participant_id='host-tab'"
            )
            group.reconcile_readiness(cursor, state)

        state = group.mutate("host", None, None, ready)
        self.assertTrue(state["playing"])
        self.assertFalse(state["resumeWhenReady"])

    def test_seek_resumes_after_every_watching_member_is_ready(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        group.mutate(
            "viewer",
            None,
            None,
            lambda cursor, state: cursor.execute(
                "INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)",
                (group.id, "viewer", "viewer-tab", "Viewer"),
            ),
        )

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE group_id=?",
                (group.id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=1,
                anchor_position=10,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="playing",
            )

        group.mutate("host", None, None, prepare)

        def seek(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET loading=1,ready_generation=-1,presence_sequence=0 WHERE group_id=?",
                (group.id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                position=35,
                playing=0,
                resume=1,
                anchor_position=35,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="paused",
                pause_reason="seek",
            )

        sought = group.mutate("host", None, None, seek)
        self.assertFalse(sought["playing"])
        self.assertTrue(sought["resumeWhenReady"])
        self.assertEqual(sought["pauseReason"], "seek")

        def host_ready(cursor, state):
            self.assertTrue(
                group.apply_presence(
                    cursor,
                    state,
                    "host",
                    "host-tab",
                    state["mediaGeneration"],
                    state["timelineRevision"],
                    1,
                    True,
                    False,
                )
            )

        waiting = group.mutate("host", None, None, host_ready)
        self.assertFalse(waiting["playing"])
        self.assertTrue(waiting["resumeWhenReady"])

        def viewer_ready(cursor, state):
            self.assertTrue(
                group.apply_presence(
                    cursor,
                    state,
                    "viewer",
                    "viewer-tab",
                    state["mediaGeneration"],
                    state["timelineRevision"],
                    1,
                    True,
                    False,
                )
            )

        resumed = group.mutate("viewer", None, None, viewer_ready)
        self.assertTrue(resumed["playing"])
        self.assertFalse(resumed["resumeWhenReady"])
        self.assertEqual(resumed["playbackState"], "playing")
        self.assertEqual(resumed["anchorPosition"], 35)

    def test_paused_seek_remains_paused_after_readiness(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=1,ready_generation=-1 WHERE group_id=?",
                (group.id,),
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=0,
                resume=0,
                anchor_position=35,
                anchor_time=time.time(),
                effective_at=0,
                playback_state="paused",
                pause_reason="seek",
            )

        group.mutate("host", None, None, prepare)

        def ready(cursor, state):
            self.assertTrue(
                group.apply_presence(
                    cursor,
                    state,
                    "host",
                    "host-tab",
                    state["mediaGeneration"],
                    state["timelineRevision"],
                    1,
                    True,
                    False,
                )
            )

        state = group.mutate("host", None, None, ready)
        self.assertFalse(state["playing"])
        self.assertFalse(state["resumeWhenReady"])
        self.assertEqual(state["pauseReason"], "seek")

    def test_explicit_pause_is_not_released_by_readiness(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")

        def prepare(cursor, state):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE participant_id='host-tab'"
            )
            group.transition(
                cursor,
                state,
                timeline=True,
                item_id="movie",
                media_generation=1,
                playing=0,
                resume=0,
                playback_state="paused",
                pause_reason="command",
            )

        group.mutate("host", None, None, prepare)

        def reconcile(cursor, state):
            group.reconcile_readiness(cursor, state)

        state = group.mutate("host", None, None, reconcile)
        self.assertFalse(state["playing"])
        self.assertFalse(state["resumeWhenReady"])
        self.assertEqual(state["pauseReason"], "command")

    def test_schedule_and_pause_preserve_authoritative_timeline(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        state = group.mutate(
            "host",
            None,
            None,
            lambda cursor, current: schedule(group, cursor, current, 42),
        )
        self.assertTrue(state["playing"])
        self.assertEqual(state["anchorPosition"], 42)
        self.assertEqual(state["playbackState"], "playing")
        paused = group.mutate(
            "host",
            state["revision"],
            None,
            lambda cursor, current: pause(group, cursor, current, "command"),
        )
        self.assertFalse(paused["playing"])
        self.assertEqual(paused["playbackState"], "paused")
        self.assertEqual(paused["pauseReason"], "command")
        self.assertGreaterEqual(paused["anchorPosition"], 42)

    def test_stale_command_revision_is_rejected(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        latest = group.mutate(
            "host", None, None, lambda cursor, state: schedule(group, cursor, state, 10)
        )
        with self.assertRaises(StaleSyncplayState) as error:
            group.mutate(
                "host",
                latest["revision"] - 1,
                "stale-command",
                lambda cursor, state: pause(group, cursor, state, "command"),
            )

    def test_identical_presence_heartbeat_does_not_change_revision(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        initial = group.state()

        def heartbeat(cursor, state):
            self.assertFalse(
                group.apply_presence(
                    cursor,
                    state,
                    "host",
                    "host-tab",
                    initial["mediaGeneration"],
                    initial["timelineRevision"],
                    1,
                    False,
                    False,
                )
            )

        unchanged = group.mutate(
            "host", initial["revision"], "presence-heartbeat", heartbeat
        )
        self.assertEqual(unchanged["revision"], initial["revision"])
        self.assertEqual(unchanged["timelineRevision"], initial["timelineRevision"])

        next_state = group.mutate(
            "host",
            unchanged["revision"],
            "play-after-heartbeat",
            lambda cursor, state: schedule(group, cursor, state, 10),
        )
        self.assertEqual(next_state["revision"], initial["revision"] + 1)

    def test_real_readiness_change_advances_revision_without_timeline_change(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        initial = group.state()

        def ready(cursor, state):
            self.assertTrue(
                group.apply_presence(
                    cursor,
                    state,
                    "host",
                    "host-tab",
                    state["mediaGeneration"],
                    state["timelineRevision"],
                    1,
                    True,
                    False,
                )
            )

        changed = group.mutate("host", initial["revision"], "presence-ready", ready)
        self.assertEqual(changed["revision"], initial["revision"] + 1)
        self.assertEqual(changed["timelineRevision"], initial["timelineRevision"])
        self.assertTrue(changed["members"][0]["viewing"])
        self.assertFalse(changed["members"][0]["loading"])

        def heartbeat(cursor, state):
            self.assertFalse(
                group.apply_presence(
                    cursor,
                    state,
                    "host",
                    "host-tab",
                    state["mediaGeneration"],
                    state["timelineRevision"],
                    2,
                    True,
                    False,
                )
            )

        unchanged = group.mutate(
            "host", changed["revision"], "second-presence-heartbeat", heartbeat
        )
        self.assertEqual(unchanged["revision"], changed["revision"])
        self.assertEqual(unchanged["timelineRevision"], changed["timelineRevision"])
