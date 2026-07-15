import tempfile
import unittest

from app.config import Config
from app.database import DatabaseHandler
from app.models.syncplay import SyncplayGroup, SyncplayMembershipConflict


class SyncplayModelTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.previous_database = self.config._database
        self.temp_directory = tempfile.TemporaryDirectory()
        self.config._database = DatabaseHandler("sqlite", {"sqlite": {
            "syncplay_groups": {"create": "CREATE TABLE syncplay_groups (id TEXT PRIMARY KEY, host_user_id TEXT NOT NULL, host_name TEXT NOT NULL, allow_controls INTEGER NOT NULL DEFAULT 0, item_id TEXT, position REAL NOT NULL DEFAULT 0, playing INTEGER NOT NULL DEFAULT 0, resume INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, timeline_revision INTEGER NOT NULL DEFAULT 0, media_generation INTEGER NOT NULL DEFAULT 0, anchor_position REAL NOT NULL DEFAULT 0, anchor_time REAL NOT NULL DEFAULT 0, effective_at REAL NOT NULL DEFAULT 0, playback_state TEXT NOT NULL DEFAULT 'paused', pause_reason TEXT, host_disconnected_at REAL, ended INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)", "columns": {}},
            "syncplay_members": {"create": "CREATE TABLE syncplay_members (group_id TEXT NOT NULL, user_id TEXT NOT NULL, participant_id TEXT NOT NULL DEFAULT 'legacy', username TEXT NOT NULL, watching_together INTEGER NOT NULL DEFAULT 1, viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0, ready_generation INTEGER NOT NULL DEFAULT -1, presence_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, participant_id))", "columns": {}},
            "syncplay_operations": {"create": "CREATE TABLE syncplay_operations (operation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, state TEXT NOT NULL)", "columns": {}},
        }}, db_file=f"{self.temp_directory.name}/syncplay.db")
        for table in self.config._database.create_query["sqlite"].values():
            self.config._database.execute(table["create"])

    def tearDown(self):
        self.config._database.close()
        self.config._database = self.previous_database
        self.temp_directory.cleanup()

    def test_user_can_have_only_one_active_group(self):
        SyncplayGroup.create("host", "Host")
        with self.assertRaises(SyncplayMembershipConflict):
            SyncplayGroup.create("host", "Host")

    def test_host_disconnect_is_paused_then_expires(self):
        group = SyncplayGroup.create("host", "Host")
        marked = group.mark_host_disconnected()
        self.assertEqual(marked["playbackState"], "paused")
        self.assertIsNotNone(marked["hostDisconnectedAt"])
        self.assertIsNone(group.expire_host_disconnect(marked["hostDisconnectedAt"] + 299))
        self.assertTrue(group.expire_host_disconnect(marked["hostDisconnectedAt"] + 300)["ended"])

    def test_host_reconnect_clears_disconnect_without_resuming(self):
        group = SyncplayGroup.create("host", "Host")
        group.mark_host_disconnected()
        reconnected = group.clear_host_disconnected()
        self.assertIsNone(reconnected["hostDisconnectedAt"])
        self.assertFalse(reconnected["playing"])

    def test_viewer_disconnect_removes_only_viewer(self):
        group = SyncplayGroup.create("host", "Host")
        group.mutate("viewer", None, None, lambda cursor, state: cursor.execute("INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES (?,?,?,?)", (group.id, "viewer", "viewer-tab", "Viewer")))
        state = group.remove_disconnected_member("viewer", "viewer-tab")
        self.assertEqual([member["userId"] for member in state["members"]], ["host"])
        self.assertFalse(state["ended"])

    def test_members_default_to_watching_together(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        self.assertTrue(group.state()["members"][0]["watchingTogether"])

    def test_browsing_member_is_excluded_from_initial_readiness(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        def prepare(cursor, state):
            cursor.execute("INSERT INTO syncplay_members (group_id,user_id,participant_id,username,watching_together) VALUES (?,?,?,?,0)", (group.id, "viewer", "viewer-tab", "Viewer"))
            cursor.execute("UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE participant_id='host-tab'")
            group.transition(cursor, state, timeline=True, item_id="episode", media_generation=1, resume=1, playback_state="paused", pause_reason="readiness")
        group.mutate("host", None, None, prepare)
        def release(cursor, state): group.reconcile_readiness(cursor, state)
        state = group.mutate("host", None, None, release)
        self.assertTrue(state["playing"])
        self.assertFalse(state["resumeWhenReady"])

    def test_leaving_initial_barrier_releases_remaining_member(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        def prepare(cursor, state):
            cursor.execute("INSERT INTO syncplay_members (group_id,user_id,participant_id,username,loading) VALUES (?,?,?,?,1)", (group.id, "viewer", "viewer-tab", "Viewer"))
            cursor.execute("UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE participant_id='host-tab'")
            group.transition(cursor, state, timeline=True, item_id="episode", media_generation=1, resume=1, playback_state="paused", pause_reason="readiness")
        group.mutate("host", None, None, prepare)
        state = group.set_participation("viewer", "viewer-tab", False, "leave-barrier")
        self.assertTrue(state["playing"])
        self.assertFalse(next(member for member in state["members"] if member["userId"] == "viewer")["watchingTogether"])

    def test_buffering_after_start_pauses_room_until_ready(self):
        group = SyncplayGroup.create("host", "host-tab", "Host")
        def prepare(cursor, state):
            cursor.execute("UPDATE syncplay_members SET viewing=1,loading=1,ready_generation=-1 WHERE participant_id='host-tab'")
            group.transition(cursor, state, timeline=True, item_id="movie", media_generation=1, playing=1, resume=0, playback_state="playing", anchor_time=state["updatedAt"], effective_at=state["updatedAt"])
        group.mutate("host", None, None, prepare)
        def reconcile(cursor, state): group.reconcile_readiness(cursor, state)
        state = group.mutate("host", None, None, reconcile)
        self.assertFalse(state["playing"])
        self.assertTrue(state["resumeWhenReady"])
        self.assertEqual(state["pauseReason"], "buffering")

        def ready(cursor, state):
            cursor.execute("UPDATE syncplay_members SET loading=0,ready_generation=1 WHERE participant_id='host-tab'")
            group.reconcile_readiness(cursor, state)
        state = group.mutate("host", None, None, ready)
        self.assertTrue(state["playing"])
        self.assertFalse(state["resumeWhenReady"])
