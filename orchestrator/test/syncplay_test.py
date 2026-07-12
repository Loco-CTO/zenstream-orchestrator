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
            "syncplay_members": {"create": "CREATE TABLE syncplay_members (group_id TEXT NOT NULL, user_id TEXT NOT NULL, username TEXT NOT NULL, viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0, ready_generation INTEGER NOT NULL DEFAULT -1, presence_sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, user_id))", "columns": {}},
            "syncplay_operations": {"create": "CREATE TABLE syncplay_operations (operation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, state TEXT NOT NULL)", "columns": {}},
        }}, db_file=f"{self.temp_directory.name}/syncplay.db")

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
        group.mutate("viewer", None, None, lambda cursor, state: cursor.execute("INSERT INTO syncplay_members (group_id,user_id,username) VALUES (?,?,?)", (group.id, "viewer", "Viewer")))
        state = group.remove_disconnected_member("viewer")
        self.assertEqual([member["userId"] for member in state["members"]], ["host"])
        self.assertFalse(state["ended"])
