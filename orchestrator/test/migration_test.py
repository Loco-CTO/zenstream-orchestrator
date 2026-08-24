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
                        "idx_user_item_state_continue",
                        "idx_metadata_images_url_path_ready",
                        "idx_metadata_images_type_url_fetched",
                    }
                    <= indexes
                )
                playback_columns = {
                    row[1]: row
                    for row in connection.execute(
                        "PRAGMA table_info(playback_settings)"
                    )
                }
                preference_columns = {
                    row[1]: row
                    for row in connection.execute(
                        "PRAGMA table_info(account_preferences)"
                    )
                }
                self.assertEqual(
                    str(preference_columns["watch_history_enabled"][4]).strip("'\""),
                    "1",
                )
                intro_outro_columns = {
                    row[1]: row
                    for row in connection.execute(
                        "PRAGMA table_info(intro_outro_settings)"
                    )
                }
                self.assertIn(playback_columns["trickplay_workers"][4], {"1", "'1'"})
                self.assertIn(
                    playback_columns["trickplay_ffmpeg_threads"][4], {"4", "'4'"}
                )
                self.assertIn(
                    intro_outro_columns["intro_outro_workers"][4], {"1", "'1'"}
                )
                self.assertIn(
                    intro_outro_columns["intro_outro_ffmpeg_threads"][4],
                    {"4", "'4'"},
                )
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
                        "intro_outro_comparison_state",
                        "user_follow_targets",
                        "catalog_admissions",
                        "notifications",
                        "bazarr_series_mappings",
                        "bazarr_episode_mappings",
                        "bazarr_movie_mappings",
                    }
                    <= tables
                )
                series_mapping_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(bazarr_series_mappings)"
                    )
                }
                episode_mapping_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(bazarr_episode_mappings)"
                    )
                }
                movie_mapping_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(bazarr_movie_mappings)"
                    )
                }
                self.assertTrue(
                    {"series_entity_id", "bazarr_series_id", "state"}
                    <= series_mapping_columns
                )
                self.assertTrue(
                    {
                        "media_file_id",
                        "target_path",
                        "size",
                        "modified_ns",
                        "quick_fingerprint",
                        "bazarr_episode_id",
                        "subtitles_json",
                    }
                    <= episode_mapping_columns
                )
                self.assertTrue(
                    {
                        "media_file_id",
                        "entity_id",
                        "library_id",
                        "target_path",
                        "size",
                        "modified_ns",
                        "quick_fingerprint",
                        "bazarr_movie_id",
                        "state",
                        "title",
                        "subtitles_json",
                    }
                    <= movie_mapping_columns
                )
                self.assertNotIn("notification_push_subscriptions", tables)
                self.assertNotIn("notification_push_outbox", tables)
                follow_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(user_follow_targets)"
                    )
                }
                self.assertTrue(
                    {
                        "user_id",
                        "library_id",
                        "target_type",
                        "provider",
                        "provider_id",
                        "entity_id",
                    }
                    <= follow_columns
                )
                notification_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(notifications)")
                }
                self.assertTrue(
                    {
                        "user_id",
                        "kind",
                        "entity_id",
                        "series_id",
                        "dedupe_key",
                        "read_at",
                    }
                    <= notification_columns
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
                admin_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(admins)")
                }
                session_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(admin_sessions)")
                }
                self.assertIn("password_scheme", admin_columns)
                self.assertEqual(
                    session_columns, {"username", "token_hash", "expires_at"}
                )
                invite_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(invites)")
                }
                self.assertTrue(
                    {"id", "url", "max_uses", "used_uses", "expires_at", "created_at"}
                    <= invite_columns
                )
                self.assertIn(
                    "invite_library_access",
                    tables,
                )
            finally:
                connection.close()

    def test_bazarr_movie_mapping_migration_has_a_reversible_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "head")
            command.downgrade(config, "0043_bazarr_mapping_cache")

            connection = sqlite3.connect(database_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertNotIn("bazarr_movie_mappings", tables)
            finally:
                connection.close()

            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                        ("bazarr_movie_mappings",),
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_avatar_migration_preserves_existing_accounts_without_avatar_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "0035_subtitle_outline_default")
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "INSERT INTO users(id,username,password) VALUES(?,?,?)",
                    ("user-1", "Alex", "password"),
                )
                connection.commit()
            finally:
                connection.close()

            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("user_avatars", tables)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM user_avatars WHERE user_id=?",
                        ("user-1",),
                    ).fetchone()[0],
                    0,
                )
                columns = {
                    row[1]: row
                    for row in connection.execute("PRAGMA table_info(user_avatars)")
                }
                self.assertEqual(
                    set(columns),
                    {"user_id", "version", "file_format", "created_at", "updated_at"},
                )
            finally:
                connection.close()

    def test_legacy_invites_are_migrated_to_single_use_records(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "0013_invite_hardening")
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "INSERT INTO invites(url,expires_at,consumed_at) VALUES(?,?,?)",
                    ("legacy-hash", "2030-01-01T00:00:00+00:00", None),
                )
                connection.commit()
            finally:
                connection.close()
            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT max_uses,used_uses,expires_at FROM invites WHERE url='legacy-hash'"
                    ).fetchone(),
                    (1, 0, "2030-01-01T00:00:00+00:00"),
                )
                self.assertTrue(
                    connection.execute(
                        "SELECT id FROM invites WHERE url='legacy-hash'"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

    def test_existing_sessions_receive_legacy_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "0032_playback_language_preferences")
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "INSERT INTO users(id,username,password) VALUES(?,?,?)",
                    ("user-1", "legacy", "hash"),
                )
                connection.execute(
                    "INSERT INTO user_sessions(id,user_id,token_hash,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                    (
                        "session-1",
                        "user-1",
                        "token-hash",
                        "2099-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-02T00:00:00+00:00",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("""
                        SELECT s.device_id,d.device_key,d.device_type
                          FROM user_sessions s JOIN user_devices d ON d.id=s.device_id
                         WHERE s.id='session-1'
                        """).fetchone()[1:],
                    ("legacy", "unknown"),
                )
            finally:
                connection.close()

    def test_subtitle_outline_default_preserves_existing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "0034_intro_outro_comparison_state")
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "INSERT INTO users(id,username,password) VALUES(?,?,?)",
                    ("user-1", "viewer", "hash"),
                )
                connection.execute(
                    "INSERT INTO account_preferences(user_id,subtitle_border_size) VALUES(?,?)",
                    ("user-1", 0),
                )
                connection.commit()
            finally:
                connection.close()

            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                column = next(
                    row
                    for row in connection.execute(
                        "PRAGMA table_info(account_preferences)"
                    )
                    if row[1] == "subtitle_border_size"
                )
                self.assertEqual(str(column[4]).strip("'\""), "2")
                self.assertEqual(
                    connection.execute(
                        "SELECT subtitle_border_size FROM account_preferences WHERE user_id=?",
                        ("user-1",),
                    ).fetchone()[0],
                    0.0,
                )
                connection.execute(
                    "INSERT INTO users(id,username,password) VALUES(?,?,?)",
                    ("user-2", "new-viewer", "hash"),
                )
                connection.execute(
                    "INSERT INTO account_preferences(user_id) VALUES(?)",
                    ("user-2",),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT subtitle_border_size FROM account_preferences WHERE user_id=?",
                        ("user-2",),
                    ).fetchone()[0],
                    2.0,
                )
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

    def test_artwork_selection_rebuild_migration_marks_ready_models_dirty(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "0009_artwork_selection_provider")
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "UPDATE catalog_read_model_status SET state='ready' WHERE id=1"
                )
                connection.commit()
            finally:
                connection.close()
            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT state FROM catalog_read_model_status WHERE id=1"
                    ).fetchone()[0],
                    "building",
                )
            finally:
                connection.close()

    def test_upgrade_deduplicates_neutral_images_before_enforcing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            config = self._config(database_path)
            command.upgrade(config, "0004_catalog_read_model_progress")
            connection = sqlite3.connect(database_path)
            try:
                connection.executescript("""
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
                    """)
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
