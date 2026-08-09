import sqlite3
import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PersistenceMigrationTest(unittest.TestCase):
    @staticmethod
    def _config(database_path: Path) -> Config:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option(
            "script_location", (PROJECT_ROOT / "migrations").as_posix()
        )
        config.set_main_option(
            "sqlalchemy.url", f"sqlite:///{database_path.as_posix()}"
        )
        return config

    def test_clean_upgrade_builds_normalized_image_schema_and_hot_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "head")

            connection = sqlite3.connect(database_path)
            try:
                columns = {
                    row[1]: row
                    for row in connection.execute("PRAGMA table_info(metadata_images)")
                }
                self.assertEqual(columns["locale"][3], 1)
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertTrue(
                    {
                        "idx_entity_provider_ids_provider_id",
                        "idx_library_jobs_global_queue",
                        "idx_catalog_search_grams_entity_locale",
                        "idx_catalog_root_search_grams_lookup",
                        "idx_catalog_item_genres_covering",
                        "idx_catalog_artwork_selection_lookup",
                        "idx_catalog_collection_member_projection_page",
                        "idx_metadata_images_url_path_ready",
                        "idx_metadata_images_type_url_fetched",
                    }
                    <= indexes
                )
                playback_columns = {
                    row[1]: row
                    for row in connection.execute("PRAGMA table_info(playback_settings)")
                }
                intro_outro_columns = {
                    row[1]: row
                    for row in connection.execute("PRAGMA table_info(intro_outro_settings)")
                }
                self.assertIn(playback_columns["trickplay_workers"][4], {"1", "'1'"})
                self.assertIn(intro_outro_columns["intro_outro_workers"][4], {"1", "'1'"})
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "catalog_library_summary",
                        "catalog_root_search_grams",
                        "catalog_artwork_selection",
                        "catalog_collection_member_projection",
                    }
                    <= tables
                )
                genre_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(catalog_item_genres)"
                    )
                }
                self.assertTrue({"library_id", "entity_type"} <= genre_columns)
                artwork_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(catalog_artwork_selection)"
                    )
                }
                self.assertIn("provider", artwork_columns)
            finally:
                connection.close()

    def test_catalog_performance_migration_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "head")
            command.downgrade(config, "0006_analysis_worker_limits")
            connection = sqlite3.connect(database_path)
            try:
                genre_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(catalog_item_genres)"
                    )
                }
                self.assertNotIn("library_id", genre_columns)
                self.assertNotIn("entity_type", genre_columns)
            finally:
                connection.close()
            command.upgrade(config, "head")

    def test_upgrade_deduplicates_neutral_images_before_enforcing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "0004_catalog_read_model_progress")
            connection = sqlite3.connect(database_path)
            try:
                connection.executescript(
                    """
                    DROP TABLE metadata_images;
                    CREATE TABLE metadata_images (
                        provider TEXT NOT NULL,entity_type TEXT NOT NULL,
                        provider_id TEXT NOT NULL,locale TEXT,image_type TEXT NOT NULL,
                        image_url TEXT NOT NULL,local_path TEXT,fetched_at TEXT,
                        expires_at TEXT,blur_hash TEXT,
                        PRIMARY KEY(provider,entity_type,provider_id,locale,image_type,image_url)
                    );
                    INSERT INTO metadata_images VALUES(
                        'tmdb','movie','10',NULL,'Primary','poster','old','2026-01-01',NULL,NULL
                    );
                    INSERT INTO metadata_images VALUES(
                        'tmdb','movie','10',NULL,'Primary','poster','new','2026-01-02',NULL,NULL
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()

            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT locale,local_path,COUNT(*) FROM metadata_images"
                    ).fetchall(),
                    [("", "new", 1)],
                )
            finally:
                connection.close()
