import importlib.util
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.database import DatabaseHandler
from app.offline import OfflineService


class OfflineIdentityTest(unittest.TestCase):
    def test_capabilities_do_not_advertise_incomplete_features(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        self.addCleanup(db.close)
        db.execute(
            "CREATE TABLE offline_server(id INTEGER PRIMARY KEY,instance_id TEXT)"
        )
        db.execute("INSERT INTO offline_server VALUES(1,'instance-0001')")
        service = OfflineService(db)
        first = service.capabilities()
        self.assertEqual(
            first["serverInstanceId"], service.capabilities()["serverInstanceId"]
        )
        self.assertFalse(first["enabled"])
        self.assertEqual(first["profiles"], [])
        self.assertEqual(first["features"], [])
        self.assertNotIn("secret", str(first).lower())

    def test_migration_creates_distinct_valid_identities_and_is_reversible(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "migrations/versions/0064_offline_identity.py"
        )
        spec = importlib.util.spec_from_file_location(
            "offline_identity_migration", source
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        identities = []
        for _ in range(2):
            engine = sa.create_engine("sqlite://")
            with (
                engine.begin() as connection,
                patch.object(
                    migration, "op", Operations(MigrationContext.configure(connection))
                ),
            ):
                migration.upgrade()
                identities.append(
                    connection.execute(
                        sa.text("SELECT instance_id FROM offline_server")
                    ).scalar_one()
                )
                uuid.UUID(identities[-1])
                migration.downgrade()
                self.assertNotIn(
                    "offline_server", sa.inspect(connection).get_table_names()
                )
            engine.dispose()
        self.assertNotEqual(*identities)
