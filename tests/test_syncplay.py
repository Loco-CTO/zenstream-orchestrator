import os
import sqlite3
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from app.models.syncplay import StaleSyncplayState, SyncplayGroup


class MemoryDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE syncplay_groups (
                id TEXT PRIMARY KEY, host_user_id TEXT, host_name TEXT,
                allow_controls INTEGER, item_id TEXT, position REAL, playing INTEGER,
                resume INTEGER, revision INTEGER, timeline_revision INTEGER, media_generation INTEGER,
                anchor_position REAL, anchor_time REAL, effective_at REAL, playback_state TEXT, pause_reason TEXT,
                ended INTEGER, updated REAL
            );
            CREATE TABLE syncplay_members (
                group_id TEXT, user_id TEXT, username TEXT, viewing INTEGER,
                loading INTEGER, ready_generation INTEGER DEFAULT -1, presence_sequence INTEGER DEFAULT 0, PRIMARY KEY (group_id, user_id)
            );
            CREATE TABLE syncplay_operations (
                operation_id TEXT PRIMARY KEY, group_id TEXT, user_id TEXT, state TEXT
            );
        """)

    def execute(self, query, params):
        cursor = self.connection.execute(query, params)
        self.connection.commit()
        return cursor.fetchall() if query.lstrip().upper().startswith("SELECT") else []

    @contextmanager
    def transaction(self):
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        finally:
            cursor.close()


class SyncplayReadinessTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()
        self.database.execute(
            "INSERT INTO syncplay_groups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("group", "host", "Alex", 0, "old-item", 3, 1, 0, 4, 0, 0, 3, 0, 0, "playing", None, 0, 0),
        )
        for user in ("host", "viewer"):
            self.database.execute(
                "INSERT INTO syncplay_members VALUES (?,?,?,?,?,?,?)",
                ("group", user, user, 1, 0, 0, 0),
            )
        self.group = SyncplayGroup("group")
        self.group.db = self.database

    def test_new_media_waits_until_every_member_is_ready(self):
        def apply(cursor, old):
            cursor.execute("UPDATE syncplay_members SET viewing=0,loading=1,ready_generation=-1 WHERE group_id=?", ("group",))
            self.group.transition(cursor, old, item_id="new-item", position=0, playing=0, resume=1, media_generation=1)
        state = self.group.mutate("host", 4, "new-media", apply)

        self.assertEqual(state["itemId"], "new-item")
        self.assertFalse(state["playing"])
        self.assertTrue(state["resumeWhenReady"])
        with self.database.transaction() as cursor:
            self.assertTrue(self.group.waiting_for_members(cursor, 1))

        self.database.execute(
            "UPDATE syncplay_members SET viewing=1, loading=0 WHERE group_id=? AND user_id=?",
            ("group", "host"),
        )
        with self.database.transaction() as cursor:
            self.assertTrue(self.group.waiting_for_members(cursor, 1))
        self.database.execute(
            "UPDATE syncplay_members SET viewing=1, loading=0 WHERE group_id=? AND user_id=?",
            ("group", "viewer"),
        )
        self.database.execute("UPDATE syncplay_members SET ready_generation=1 WHERE group_id=?", ("group",))
        with self.database.transaction() as cursor:
            self.assertFalse(self.group.waiting_for_members(cursor, 1))

    def test_duplicate_operation_returns_the_original_snapshot(self):
        def apply(cursor, old):
            self.group.transition(cursor, old, playing=0)

        first = self.group.mutate("host", 4, "pause-once", apply)
        repeated = self.group.mutate("host", 5, "pause-once", apply)
        self.assertEqual(first["revision"], 5)
        self.assertEqual(repeated["revision"], 5)

    def test_stale_revision_cannot_change_group_state(self):
        with self.assertRaises(StaleSyncplayState):
            self.group.mutate("host", 3, "stale", lambda cursor, old: self.group.transition(cursor, old, playing=0))
        self.assertEqual(self.group.state()["revision"], 4)


if __name__ == "__main__":
    unittest.main()
