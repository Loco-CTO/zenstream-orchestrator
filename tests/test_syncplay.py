import os
import sqlite3
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from app.models.syncplay import StaleSyncplayState, SyncplayGroup
from app.config import Config


class MemoryDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE syncplay_groups (
                id TEXT PRIMARY KEY, host_user_id TEXT, host_name TEXT,
                allow_controls INTEGER, item_id TEXT, position REAL, playing INTEGER,
                resume INTEGER, revision INTEGER, timeline_revision INTEGER, media_generation INTEGER,
                anchor_position REAL, anchor_time REAL, effective_at REAL, playback_state TEXT, pause_reason TEXT,
                host_disconnected_at REAL, ended INTEGER, updated REAL
            );
            CREATE TABLE syncplay_members (
                group_id TEXT, user_id TEXT, participant_id TEXT, username TEXT,
                watching_together INTEGER DEFAULT 1, viewing INTEGER,
                loading INTEGER, ready_generation INTEGER DEFAULT -1, presence_sequence INTEGER DEFAULT 0, PRIMARY KEY (group_id, participant_id)
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
            "INSERT INTO syncplay_groups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "group",
                "host",
                "Alex",
                0,
                "old-item",
                3,
                1,
                0,
                4,
                0,
                0,
                3,
                0,
                0,
                "playing",
                None,
                None,
                0,
                0,
            ),
        )
        for user in ("host", "viewer"):
            self.database.execute(
                "INSERT INTO syncplay_members VALUES (?,?,?,?,?,?,?,?,?)",
                ("group", user, user + "-tab", user, 1, 1, 0, 0, 0),
            )
        self.group = SyncplayGroup("group")
        self.group.db = self.database

    def test_new_media_waits_until_every_member_is_ready(self):
        def apply(cursor, old):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=0,loading=1,ready_generation=-1 WHERE group_id=?",
                ("group",),
            )
            self.group.transition(
                cursor,
                old,
                item_id="new-item",
                position=0,
                playing=0,
                resume=1,
                media_generation=1,
            )

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
        self.database.execute(
            "UPDATE syncplay_members SET ready_generation=1 WHERE group_id=?",
            ("group",),
        )
        with self.database.transaction() as cursor:
            self.assertFalse(self.group.waiting_for_members(cursor, 1))

    def test_single_host_presence_releases_pending_playback(self):
        self.database.execute(
            "DELETE FROM syncplay_members WHERE user_id=?", ("viewer",)
        )

        def apply(cursor, old):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=0,loading=1,ready_generation=-1 WHERE group_id=?",
                ("group",),
            )
            self.group.transition(
                cursor,
                old,
                item_id="solo-item",
                playing=0,
                resume=1,
                media_generation=1,
                playback_state="paused",
                pause_reason="readiness",
            )

        state = self.group.mutate("host", 4, "solo-media", apply)
        self.assertTrue(state["resumeWhenReady"])

        def report_ready(cursor, current):
            cursor.execute(
                "UPDATE syncplay_members SET viewing=1,loading=0,ready_generation=1 WHERE group_id=?",
                ("group",),
            )
            self.group.reconcile_readiness(cursor, current)

        released = self.group.mutate(
            "host", state["revision"], "solo-ready", report_ready
        )
        self.assertTrue(released["playing"])
        self.assertFalse(released["resumeWhenReady"])
        self.assertEqual(released["playbackState"], "playing")

    def test_duplicate_operation_returns_the_original_snapshot(self):
        def apply(cursor, old):
            self.group.transition(cursor, old, playing=0)

        first = self.group.mutate("host", 4, "pause-once", apply)
        repeated = self.group.mutate("host", 5, "pause-once", apply)
        self.assertEqual(first["revision"], 5)
        self.assertEqual(repeated["revision"], 5)

    def test_stale_revision_cannot_change_group_state(self):
        with self.assertRaises(StaleSyncplayState):
            self.group.mutate(
                "host",
                3,
                "stale",
                lambda cursor, old: self.group.transition(cursor, old, playing=0),
            )
        self.assertEqual(self.group.state()["revision"], 4)

    def test_presence_from_an_older_timeline_cannot_pause_current_playback(self):
        before = self.group.state()

        with self.database.transaction() as cursor:
            accepted = self.group.apply_presence(
                cursor,
                before,
                "host",
                "host-tab",
                generation=0,
                timeline_revision=-1,
                sequence=1,
                viewing=True,
                loading=True,
            )

        after = self.group.state()
        self.assertFalse(accepted)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["timelineRevision"], before["timelineRevision"])
        self.assertTrue(after["playing"])
        self.assertFalse(after["members"][0]["loading"])

    def test_current_timeline_presence_still_pauses_and_resumes_playback(self):
        before = self.group.state()

        with self.database.transaction() as cursor:
            accepted = self.group.apply_presence(
                cursor,
                before,
                "host",
                "host-tab",
                generation=0,
                timeline_revision=0,
                sequence=1,
                viewing=True,
                loading=True,
            )

        paused = self.group.state()
        self.assertTrue(accepted)
        self.assertFalse(paused["playing"])
        self.assertEqual(paused["pauseReason"], "buffering")

        with self.database.transaction() as cursor:
            accepted = self.group.apply_presence(
                cursor,
                paused,
                "host",
                "host-tab",
                generation=0,
                timeline_revision=paused["timelineRevision"],
                sequence=2,
                viewing=True,
                loading=False,
            )

        resumed = self.group.state()
        self.assertTrue(accepted)
        self.assertTrue(resumed["playing"])
        self.assertEqual(resumed["playbackState"], "playing")

    def test_disconnecting_a_viewer_releases_a_group_waiting_to_resume(self):
        self.database.execute(
            "UPDATE syncplay_groups SET playing=0,resume=1,playback_state='paused',pause_reason='buffering' WHERE id=?",
            ("group",),
        )

        state = self.group.remove_disconnected_member("viewer", "viewer-tab")

        self.assertTrue(state["playing"])
        self.assertFalse(state["resumeWhenReady"])
        self.assertEqual([member["userId"] for member in state["members"]], ["host"])
        self.assertGreater(state["effectiveAt"], 0)

    def test_disconnecting_the_host_ends_the_group(self):
        state = self.group.mark_host_disconnected()

        self.assertFalse(state["ended"])
        self.assertFalse(state["playing"])
        self.assertEqual(state["pauseReason"], "host-disconnected")


class SyncplayMigrationTests(unittest.TestCase):
    def migrate(self, ddl, participant_column=False, unique_user=False):
        db = sqlite3.connect(":memory:")
        db.execute(ddl)
        if participant_column:
            db.execute(
                "ALTER TABLE syncplay_members ADD COLUMN participant_id TEXT NOT NULL DEFAULT ''"
            )
        if unique_user:
            db.execute(
                "CREATE UNIQUE INDEX old_member_key ON syncplay_members(group_id, user_id)"
            )
        db.execute(
            "INSERT INTO syncplay_members (group_id,user_id,username) VALUES ('g','u','User')"
        )
        db.commit()

        class Database:
            db_type = "sqlite"

            def execute(self, query, params):
                return db.execute(query, params).fetchall()

            @contextmanager
            def transaction(self):
                try:
                    yield db
                    db.commit()
                except Exception:
                    db.rollback()
                    raise

        owner = Config.__new__(Config)
        owner._database = Database()
        owner._migrate_syncplay_members_participant_key()
        return db, owner

    def test_old_primary_key_is_rebuilt(self):
        db, _ = self.migrate(
            "CREATE TABLE syncplay_members (group_id TEXT, user_id TEXT, username TEXT, viewing INTEGER DEFAULT 0, loading INTEGER DEFAULT 0, ready_generation INTEGER DEFAULT -1, presence_sequence INTEGER DEFAULT 0, PRIMARY KEY(group_id,user_id))",
            participant_column=True,
        )
        self.assertTrue(
            db.execute("SELECT participant_id FROM syncplay_members", ())
            .fetchone()[0]
            .startswith("__legacy__:")
        )
        self.assertEqual(
            db.execute(
                "PRAGMA index_info(sqlite_autoindex_syncplay_members_1)"
            ).fetchall(),
            [(0, 0, "group_id"), (1, 2, "participant_id")],
        )

    def test_partial_schema_and_old_unique_index_are_rebuilt(self):
        db, _ = self.migrate(
            "CREATE TABLE syncplay_members (group_id TEXT, user_id TEXT, username TEXT, viewing INTEGER DEFAULT 0, loading INTEGER DEFAULT 0, ready_generation INTEGER DEFAULT -1, presence_sequence INTEGER DEFAULT 0, PRIMARY KEY(group_id,user_id))",
            participant_column=True,
            unique_user=True,
        )
        db.execute(
            "INSERT INTO syncplay_members (group_id,user_id,participant_id,username) VALUES ('g','u','tab-2','User')"
        )
        db.commit()
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM syncplay_members").fetchone()[0], 2
        )

    def test_correct_schema_is_idempotent(self):
        db, owner = self.migrate(
            "CREATE TABLE syncplay_members (group_id TEXT NOT NULL, user_id TEXT NOT NULL, participant_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL, PRIMARY KEY(group_id,participant_id))"
        )
        before = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='syncplay_members'"
        ).fetchone()[0]
        owner._migrate_syncplay_members_participant_key()
        after = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='syncplay_members'"
        ).fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
