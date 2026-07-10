import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from app.models.syncplay import SyncplayGroup


class MemoryDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE syncplay_groups (
                id TEXT PRIMARY KEY, host_user_id TEXT, host_name TEXT,
                allow_controls INTEGER, item_id TEXT, position REAL, playing INTEGER,
                resume INTEGER, revision INTEGER, ended INTEGER, updated REAL
            );
            CREATE TABLE syncplay_members (
                group_id TEXT, user_id TEXT, username TEXT, viewing INTEGER,
                loading INTEGER, PRIMARY KEY (group_id, user_id)
            );
        """)

    def execute(self, query, params):
        cursor = self.connection.execute(query, params)
        self.connection.commit()
        return cursor.fetchall() if query.lstrip().upper().startswith("SELECT") else []


class SyncplayReadinessTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()
        self.database.execute(
            "INSERT INTO syncplay_groups VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("group", "host", "Alex", 0, "old-item", 3, 1, 0, 4, 0, 0),
        )
        for user in ("host", "viewer"):
            self.database.execute(
                "INSERT INTO syncplay_members VALUES (?,?,?,?,?)",
                ("group", user, user, 1, 0),
            )
        self.group = SyncplayGroup("group")
        self.group.db = self.database

    def test_new_media_waits_until_every_member_is_ready(self):
        state = self.group.begin_media("new-item", 0)

        self.assertEqual(state["itemId"], "new-item")
        self.assertFalse(state["playing"])
        self.assertTrue(state["resumeWhenReady"])
        self.assertTrue(self.group.waiting_for_members())

        self.database.execute(
            "UPDATE syncplay_members SET viewing=1, loading=0 WHERE group_id=? AND user_id=?",
            ("group", "host"),
        )
        self.assertTrue(self.group.waiting_for_members())
        self.database.execute(
            "UPDATE syncplay_members SET viewing=1, loading=0 WHERE group_id=? AND user_id=?",
            ("group", "viewer"),
        )
        self.assertFalse(self.group.waiting_for_members())


if __name__ == "__main__":
    unittest.main()
