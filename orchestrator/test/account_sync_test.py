import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class AccountSyncSchemaTest(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        self.connection = self.engine.connect()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.connection.close)
        for sql in (
            "CREATE TABLE users(id TEXT PRIMARY KEY)",
            "CREATE TABLE user_item_state(user_id TEXT,entity_id TEXT,favorite INTEGER,played INTEGER,position_seconds REAL,duration_seconds REAL)",
            "CREATE TABLE account_preferences(user_id TEXT,watch_history_enabled INTEGER)",
            "CREATE TABLE user_follow_targets(user_id TEXT,entity_id TEXT)",
            "CREATE TABLE user_playlists(id TEXT,user_id TEXT,name TEXT,description TEXT)",
            "CREATE TABLE user_playlist_items(playlist_id TEXT,entity_id TEXT,position INTEGER)",
            "INSERT INTO users VALUES('viewer')",
        ):
            self.connection.exec_driver_sql(sql)
        source = (
            Path(__file__).resolve().parents[2]
            / "migrations/versions/0065_account_sync.py"
        )
        spec = importlib.util.spec_from_file_location("account_sync_migration", source)
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        with patch.object(
            self.migration,
            "op",
            Operations(MigrationContext.configure(self.connection)),
        ):
            self.migration.upgrade()

    def test_legacy_writes_advance_only_changed_state_fields(self):
        self.connection.exec_driver_sql(
            "INSERT INTO user_item_state VALUES('viewer','movie',0,0,0,100)"
        )
        self.connection.exec_driver_sql(
            "UPDATE user_item_state SET position_seconds=10"
        )
        rows = dict(
            self.connection.exec_driver_sql(
                "SELECT field,revision FROM account_sync_fields"
            ).fetchall()
        )
        self.assertEqual(rows, {"favorite": 1, "playback": 2})
        self.connection.exec_driver_sql("UPDATE user_item_state SET favorite=1")
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT revision FROM account_sync_fields WHERE field='favorite'"
            ).scalar_one(),
            2,
        )

    def test_privacy_barrier_is_monotonic(self):
        self.connection.exec_driver_sql(
            "INSERT INTO account_preferences VALUES('viewer',1)"
        )
        for enabled in (0, 1, 0):
            self.connection.exec_driver_sql(
                "UPDATE account_preferences SET watch_history_enabled=?", (enabled,)
            )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT privacy_epoch FROM account_sync_epochs"
            ).scalar_one(),
            3,
        )

    def test_playlist_deletion_retains_a_tombstone(self):
        self.connection.exec_driver_sql(
            "INSERT INTO user_playlists VALUES('playlist','viewer','name',NULL)"
        )
        self.connection.exec_driver_sql(
            "INSERT INTO user_playlist_items VALUES('playlist','track',0)"
        )
        self.connection.exec_driver_sql("DELETE FROM user_playlists")
        fields = {
            row[0]
            for row in self.connection.exec_driver_sql(
                "SELECT field FROM account_sync_fields"
            )
        }
        self.assertTrue(
            {"name", "description", "order", "member:track", "deleted"}.issubset(fields)
        )

    def test_downgrade_removes_tables_and_triggers(self):
        with patch.object(
            self.migration,
            "op",
            Operations(MigrationContext.configure(self.connection)),
        ):
            self.migration.downgrade()
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'sync_%'"
            ).scalar_one(),
            0,
        )
