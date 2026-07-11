import os
import sqlite3
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from app.models.syncplay import SyncplayGroup


class MemoryDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE syncplay_groups (
                id TEXT PRIMARY KEY, host_user_id TEXT, host_name TEXT,
                allow_controls INTEGER, item_id TEXT, position REAL, playing INTEGER,
                resume INTEGER, revision INTEGER, media_generation INTEGER, ended INTEGER, updated REAL
            );
            CREATE TABLE syncplay_members (
                group_id TEXT, user_id TEXT, username TEXT, viewing INTEGER,
                loading INTEGER, ready_generation INTEGER DEFAULT -1, presence_sequence INTEGER DEFAULT 0, PRIMARY KEY (group_id, user_id)
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
            "INSERT INTO syncplay_groups VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("group", "host", "Alex", 0, "old-item", 3, 1, 0, 4, 0, 0, 0),
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


if __name__ == "__main__":
    unittest.main()
