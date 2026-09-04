import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.database import DatabaseHandler
from app.metadata_refresh import MetadataRefreshJob, _patterns
from app.models.metadata import (
    DEFAULT_METADATA_REFRESH_SETTINGS,
    MetadataRefreshSettings,
)
from app.providers import ProviderError


class MetadataRefreshSelectionTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,created_at TEXT,relative_path TEXT)"
        )
        self.db.execute(
            "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,is_primary INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE metadata_cache(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,payload TEXT,fetched_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,payload TEXT,PRIMARY KEY(entity_id,locale))"
        )
        self.db.execute(
            "CREATE TABLE metadata_refresh_state(entity_id TEXT PRIMARY KEY,last_attempted_at TEXT,last_completed_at TEXT,last_error TEXT)"
        )
        self.db.execute(
            "CREATE TABLE metadata_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)"
        )
        self.job = MetadataRefreshJob(SimpleNamespace(db=self.db))

    def tearDown(self):
        self.db.close()

    @staticmethod
    def settings():
        return copy.deepcopy(DEFAULT_METADATA_REFRESH_SETTINGS)

    def add_entity(
        self,
        entity_id,
        entity_type,
        *,
        created_at=None,
        title="Example",
        overview=None,
        provider_id=None,
    ):
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?)",
            (
                entity_id,
                "library-1",
                None,
                entity_type,
                created_at or datetime.now(timezone.utc).isoformat(),
                f"{entity_id}.mkv",
            ),
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            (entity_id, "tmdb", entity_type, provider_id or entity_id, 1),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (entity_id, "en", json.dumps({"title": title, "overview": overview})),
        )

    def test_selects_movies_series_seasons_and_episodes(self):
        settings = self.settings()
        for entity_type in ("movie", "series", "season", "episode"):
            settings["itemTypes"][entity_type]["artwork"] = {
                image_type: {"enabled": False, "maxAgeDays": 7}
                for image_type in ("Primary", "Backdrop", "Logo", "Banner")
            }
            self.add_entity(entity_type, entity_type, overview=None)

        candidates, _skipped = self.job._select(settings, ["en"])

        self.assertEqual(
            {candidate["entity"]["type"] for candidate in candidates},
            {"movie", "series", "season", "episode"},
        )

    def test_requires_relevant_cache_bucket_to_be_old(self):
        settings = self.settings()
        for image_type in settings["itemTypes"]["movie"]["artwork"]:
            settings["itemTypes"]["movie"]["artwork"][image_type]["enabled"] = False
        self.add_entity("movie-1", "movie", overview=None, provider_id="42")
        fresh = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO metadata_cache VALUES(?,?,?,?,?,?)",
            (
                "tmdb",
                "movie",
                "42",
                "en",
                json.dumps({"title": "Example", "overview": None}),
                fresh,
            ),
        )

        candidates, _skipped = self.job._select(settings, ["en"])
        self.assertEqual(candidates, [])

        self.db.execute(
            "UPDATE metadata_cache SET fetched_at=? WHERE provider_id='42'",
            ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),),
        )
        candidates, _skipped = self.job._select(settings, ["en"])
        self.assertEqual([candidate["entity"]["id"] for candidate in candidates], ["movie-1"])

    def test_unlimited_cache_age_refreshes_missing_metadata(self):
        settings = self.settings()
        settings["itemTypes"]["movie"]["documentMaxAgeDays"] = -1
        for image_type in settings["itemTypes"]["movie"]["artwork"]:
            settings["itemTypes"]["movie"]["artwork"][image_type]["enabled"] = False
        self.add_entity("movie-1", "movie", overview=None, provider_id="42")

        candidates, _skipped = self.job._select(settings, ["en"])

        self.assertEqual([candidate["entity"]["id"] for candidate in candidates], ["movie-1"])

    def test_filter_values_trim_delimited_patterns(self):
        self.assertEqual(_patterns("  TBA | TBD\nExample  "), ["tba", "tbd", "example"])

    def test_cutoff_and_attempt_cooldown_are_applied(self):
        settings = self.settings()
        settings["itemTypes"]["movie"]["cutoffDays"] = 14
        settings["itemTypes"]["movie"]["cooldownMinutes"] = 60
        self.add_entity(
            "movie-old",
            "movie",
            created_at=(datetime.now(timezone.utc) - timedelta(days=15)).isoformat(),
            overview=None,
        )
        self.add_entity("movie-recent", "movie", overview=None)
        self.db.execute(
            "INSERT INTO metadata_refresh_state VALUES(?,?,?,?)",
            (
                "movie-recent",
                datetime.now(timezone.utc).isoformat(),
                None,
                None,
            ),
        )

        candidates, skipped = self.job._select(settings, ["en"])

        self.assertEqual(candidates, [])
        self.assertEqual(skipped["cutoff"], 1)
        self.assertEqual(skipped["cooldown"], 1)

    def test_shared_provider_identity_is_grouped_once(self):
        settings = self.settings()
        self.add_entity("movie-1", "movie", overview=None, provider_id="42")
        self.add_entity("movie-2", "movie", overview=None, provider_id="42")

        candidates, _skipped = self.job._select(settings, ["en"])
        groups = self.job._groups(candidates)

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            {candidate["entity"]["id"] for candidate in groups[0]["candidates"]},
            {"movie-1", "movie-2"},
        )

    def test_attempt_state_is_recorded_for_success_and_failure(self):
        self.add_entity("movie-1", "movie", overview=None, provider_id="42")
        settings = self.settings()
        candidates, _skipped = self.job._select(settings, ["en"])
        group = self.job._groups(candidates)[0]

        class Ingest:
            metadata_service = object()

            def ingest_locales(self, *args, **kwargs):
                return {"en": {"title": "Example"}}

        self.job._process_group(
            group,
            "run-1",
            Ingest(),
            ["en"],
            lambda: False,
            False,
        )
        self.assertIsNotNone(
            self.db.execute(
                "SELECT last_attempted_at,last_completed_at,last_error FROM metadata_refresh_state WHERE entity_id='movie-1'"
            )[0][0]
        )
        self.assertIsNotNone(
            self.db.execute(
                "SELECT last_attempted_at,last_completed_at,last_error FROM metadata_refresh_state WHERE entity_id='movie-1'"
            )[0][1]
        )

        class BrokenIngest(Ingest):
            def ingest_locales(self, *args, **kwargs):
                raise ProviderError("unavailable")

        with self.assertRaises(ProviderError):
            self.job._process_group(
                group,
                "run-2",
                BrokenIngest(),
                ["en"],
                lambda: False,
                False,
            )
        self.assertEqual(
            self.db.execute(
                "SELECT last_error FROM metadata_refresh_state WHERE entity_id='movie-1'"
            )[0][0],
            "ProviderError: unavailable",
        )


if __name__ == "__main__":
    unittest.main()
