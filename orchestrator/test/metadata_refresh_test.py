import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.database import DatabaseHandler
from app.metadata_refresh import MetadataRefreshJob, _patterns, _utc
from app.models.metadata import (
    DEFAULT_METADATA_REFRESH_SETTINGS,
)
from app.providers import ProviderError


class MetadataRefreshSelectionTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,created_at TEXT,relative_path TEXT)"
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
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
        modified_at=None,
        media_file=True,
        title="Example",
        overview=None,
        provider_id=None,
    ):
        created_value = created_at or datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?)",
            (
                entity_id,
                "library-1",
                None,
                entity_type,
                created_value,
                f"{entity_id}.mkv",
            ),
        )
        if media_file:
            modified_value = _utc(modified_at or created_value)
            self.db.execute(
                "INSERT INTO media_files VALUES(?,?,?,?,?)",
                (
                    f"{entity_id}-media",
                    entity_id,
                    f"{entity_id}.mkv",
                    "media",
                    int(modified_value.timestamp() * 1_000_000_000),
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
            if entity_type == "episode":
                settings["itemTypes"][entity_type]["cutoffDays"] = -1
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
        self.assertEqual(
            [candidate["entity"]["id"] for candidate in candidates], ["movie-1"]
        )

    def test_unlimited_cache_age_refreshes_missing_metadata(self):
        settings = self.settings()
        settings["itemTypes"]["movie"]["documentMaxAgeDays"] = -1
        for image_type in settings["itemTypes"]["movie"]["artwork"]:
            settings["itemTypes"]["movie"]["artwork"][image_type]["enabled"] = False
        self.add_entity("movie-1", "movie", overview=None, provider_id="42")

        candidates, _skipped = self.job._select(settings, ["en"])

        self.assertEqual(
            [candidate["entity"]["id"] for candidate in candidates], ["movie-1"]
        )

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

    def test_episode_cutoff_uses_media_file_last_modified(self):
        settings = self.settings()
        settings["itemTypes"]["episode"]["cutoffDays"] = 90
        settings["itemTypes"]["episode"]["artwork"] = {
            image_type: {"enabled": False, "maxAgeDays": 1}
            for image_type in ("Primary", "Backdrop", "Logo", "Banner")
        }
        now = datetime.now(timezone.utc)
        self.add_entity(
            "episode-recent-modified",
            "episode",
            created_at=(now - timedelta(days=365)).isoformat(),
            modified_at=(now - timedelta(days=30)).isoformat(),
            overview="Recent episode",
        )
        self.db.execute(
            "UPDATE catalog_item_projection SET payload=? WHERE entity_id=?",
            (
                json.dumps(
                    {
                        "title": "Recent episode",
                        "overview": "Recent episode",
                        "date": (now - timedelta(days=365)).date().isoformat(),
                    }
                ),
                "episode-recent-modified",
            ),
        )
        self.add_entity(
            "episode-old-modified",
            "episode",
            created_at=(now - timedelta(days=1)).isoformat(),
            modified_at=(now - timedelta(days=365)).isoformat(),
            overview="Old episode",
        )
        self.db.execute(
            "UPDATE catalog_item_projection SET payload=? WHERE entity_id=?",
            (
                json.dumps(
                    {
                        "title": "Old episode",
                        "overview": "Old episode",
                        "date": (now - timedelta(days=30)).date().isoformat(),
                    }
                ),
                "episode-old-modified",
            ),
        )
        self.add_entity(
            "episode-no-media-file",
            "episode",
            created_at=now.isoformat(),
            media_file=False,
            overview="Unknown air date",
        )

        candidates, skipped = self.job._select(settings, ["en"])

        self.assertEqual(
            [candidate["entity"]["id"] for candidate in candidates],
            ["episode-recent-modified"],
        )
        self.assertEqual(skipped["cutoff"], 2)

    def test_document_age_selects_complete_episode(self):
        settings = self.settings()
        settings["itemTypes"]["episode"]["cutoffDays"] = -1
        settings["itemTypes"]["episode"]["documentMaxAgeDays"] = 1
        settings["itemTypes"]["episode"]["artwork"] = {
            image_type: {"enabled": False, "maxAgeDays": 1}
            for image_type in ("Primary", "Backdrop", "Logo", "Banner")
        }
        self.add_entity("episode-1", "episode", overview="Complete overview")
        fresh = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO metadata_cache VALUES(?,?,?,?,?,?)",
            (
                "tmdb",
                "episode",
                "episode-1",
                "en",
                json.dumps({"title": "Example", "overview": "Complete overview"}),
                fresh,
            ),
        )

        candidates, _skipped = self.job._select(settings, ["en"])
        self.assertEqual(candidates, [])

        self.db.execute(
            "UPDATE metadata_cache SET fetched_at=? WHERE provider_id='episode-1'",
            ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),),
        )
        candidates, _skipped = self.job._select(settings, ["en"])

        self.assertEqual(
            [candidate["entity"]["id"] for candidate in candidates], ["episode-1"]
        )
        self.assertIn("document refresh age", candidates[0]["reasons"])

    def test_missing_provider_artwork_category_waits_for_document_recheck(self):
        settings = self.settings()
        settings["itemTypes"]["episode"]["cutoffDays"] = -1
        settings["itemTypes"]["episode"]["artwork"] = {
            "Primary": {"enabled": False, "maxAgeDays": 1},
            "Backdrop": {"enabled": True, "maxAgeDays": 1},
            "Logo": {"enabled": True, "maxAgeDays": 1},
            "Banner": {"enabled": True, "maxAgeDays": 1},
        }
        self.add_entity("episode-1", "episode", overview="Complete overview")
        fresh = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO metadata_cache VALUES(?,?,?,?,?,?)",
            (
                "tmdb",
                "episode",
                "episode-1",
                "en",
                json.dumps(
                    {
                        "title": "Example",
                        "overview": "Complete overview",
                        "images": [{"type": "Primary", "url": "https://example.test/1"}],
                    }
                ),
                fresh,
            ),
        )

        candidates, _skipped = self.job._select(settings, ["en"])

        self.assertEqual(candidates, [])

    def test_missing_provider_artwork_is_rechecked_after_document_age(self):
        settings = self.settings()
        settings["itemTypes"]["movie"]["artwork"] = {
            "Primary": {"enabled": False, "maxAgeDays": 7},
            "Backdrop": {"enabled": True, "maxAgeDays": 7},
            "Logo": {"enabled": False, "maxAgeDays": 7},
            "Banner": {"enabled": False, "maxAgeDays": 7},
        }
        self.add_entity("movie-1", "movie", overview="Complete overview")
        self.db.execute(
            "INSERT INTO metadata_cache VALUES(?,?,?,?,?,?)",
            (
                "tmdb",
                "movie",
                "movie-1",
                "en",
                json.dumps(
                    {
                        "title": "Example",
                        "overview": "Complete overview",
                        "images": [{"type": "Primary", "url": "https://example.test/1"}],
                    }
                ),
                (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
            ),
        )

        candidates, _skipped = self.job._select(settings, ["en"])

        self.assertEqual(
            [candidate["entity"]["id"] for candidate in candidates], ["movie-1"]
        )
        self.assertIn("artwork discovery age (Backdrop)", candidates[0]["reasons"])

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

    def test_selection_stats_capture_work_shape_without_changing_selection(self):
        settings = self.settings()
        self.add_entity("movie-1", "movie", overview=None, provider_id="42")
        candidates, skipped = self.job._select(settings, ["en", "ja", "zh-TW"])
        groups = self.job._groups(candidates)

        stats = MetadataRefreshJob._selection_stats(
            self.job._entities(),
            candidates,
            groups,
            skipped,
            ["en", "ja", "zh-TW"],
            SimpleNamespace(image_ingest=object()),
        )

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["providerIdentities"], 1)
        self.assertEqual(stats["candidateTypes"], {"movie": 1})
        self.assertEqual(stats["providerGroups"], {"tmdb:movie": 1})
        self.assertEqual(stats["providerRequestEstimate"], {"tmdb": 1})
        self.assertEqual(stats["projectionPassesEstimated"], 2)
        self.assertEqual(stats["projectionInvocationsEstimated"], 6)

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
