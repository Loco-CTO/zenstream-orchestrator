import hashlib
import json
import time
import unittest
from unittest.mock import patch

from app.catalog import Catalog, _CatalogReadContext
from app.database import DatabaseHandler
from app.models.account import Account
from app.models.metadata import IMAGE_LANGUAGE_SCHEMA
from fastapi import HTTPException


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        statements = [
            "CREATE TABLE users(id TEXT UNIQUE,username TEXT UNIQUE,password TEXT,password_scheme TEXT,disabled INTEGER DEFAULT 0)",
            "CREATE TABLE user_sessions(id TEXT PRIMARY KEY,user_id TEXT,token_hash TEXT UNIQUE,expires_at TEXT,created_at TEXT,last_seen_at TEXT)",
            "CREATE TABLE libraries(id TEXT PRIMARY KEY,name TEXT,type TEXT,scan_state TEXT,last_scan_finished_at TEXT,directory TEXT)",
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT,created_at TEXT,PRIMARY KEY(user_id,library_id))",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER,episode_end_number INTEGER,track_number INTEGER,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,is_primary INTEGER)",
            "CREATE TABLE metadata_cache(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,payload TEXT,fetched_at TEXT,expires_at TEXT)",
            "CREATE TABLE account_preferences(user_id TEXT PRIMARY KEY,watch_history_enabled INTEGER NOT NULL DEFAULT 1)",
            "CREATE TABLE user_item_state(user_id TEXT,entity_id TEXT,favorite INTEGER,played INTEGER,play_count INTEGER,position_seconds REAL,duration_seconds REAL,last_played_at TEXT,updated_at TEXT,PRIMARY KEY(user_id,entity_id))",
        ]
        for statement in statements:
            self.db.execute(statement)
        self.db.execute(
            "INSERT INTO libraries VALUES('allowed','Allowed','movies','ready',NULL,NULL)"
        )
        self.db.execute(
            "INSERT INTO libraries VALUES('hidden','Hidden','movies','ready',NULL,NULL)"
        )

    def tearDown(self):
        self.db.close()

    def account(self):
        value = Account.__new__(Account)
        value.db = self.db
        return value

    def catalog(self):
        value = Catalog.__new__(Catalog)
        value.db = self.db
        return value

    def seed_item(self):
        self.db.execute(
            "INSERT INTO library_entities VALUES('movie','allowed',NULL,'movie','Movie',NULL,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES('movie','tmdb','movie','10',1)"
        )
        for locale, payload in {
            "en": {
                "title": "English",
                "overview": "English overview",
                "originalLanguage": "ja",
                "trailers": [{"url": "https://youtube.com/en", "language": "en"}],
                "images": [
                    {
                        "type": "Primary",
                        "url": "en.jpg",
                        "language": "en",
                        "provider": "tmdb",
                    }
                ],
            },
            "ja": {
                "title": "Japanese",
                "originalLanguage": "ja",
                "trailers": [{"url": "https://youtube.com/ja", "language": "ja"}],
                "images": [
                    {
                        "type": "Primary",
                        "url": "neutral.jpg",
                        "language": None,
                        "provider": "tmdb",
                    }
                ],
            },
        }.items():
            payload["_imageLanguageSchema"] = IMAGE_LANGUAGE_SCHEMA
            self.db.execute(
                "INSERT INTO metadata_cache VALUES('tmdb','movie','10',?,?,?,?)",
                (locale, json.dumps(payload), "now", "later"),
            )

    def test_credits_supports_legacy_localization_value_column(self):
        """Deployed databases may use ``value`` instead of ``name``."""
        self.db.execute(
            "INSERT INTO library_entities VALUES('credit-movie','allowed',NULL,'movie','Credit',NULL,NULL,NULL,NULL,'2026','2026')"
        )
        account = self.account().create("credits-user", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.db.execute(
            "CREATE TABLE people(id TEXT PRIMARY KEY, local_path TEXT, image_blur_hash TEXT, updated_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE entity_person_credits(id TEXT PRIMARY KEY, entity_id TEXT, person_id TEXT, locale TEXT, credit_type TEXT, role TEXT, department TEXT, credit_order INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE person_localizations(person_id TEXT, locale TEXT, value TEXT, updated_at TEXT)"
        )
        self.db.execute("INSERT INTO people VALUES('person-1',NULL,NULL,'now')")
        self.db.execute(
            "INSERT INTO entity_person_credits VALUES('credit-1','credit-movie','person-1','en','cast','Hero',NULL,0)"
        )
        self.db.execute(
            "INSERT INTO person_localizations VALUES('person-1','en','Legacy Actor','now')"
        )

        result = self.catalog().credits(account["id"], "credit-movie", "en")

        self.assertEqual(result["cast"][0]["name"], "Legacy Actor")

    def test_hydrated_episode_rows_include_the_parent_series_name(self):
        catalog = self.catalog()
        account_id = "user-1"
        episode_row = (
            "episode-1",
            "allowed",
            "season-1",
            "episode",
            "Episode.mkv",
            1,
            1,
            None,
            "2026",
            "2026",
        )
        with (
            patch.object(
                catalog,
                "_entity_row",
                return_value=(
                    "season-1",
                    "allowed",
                    "series-1",
                    "season",
                    "Season",
                    1,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            ),
            patch.object(
                catalog, "_series_metadata", return_value={"title": "Example Series"}
            ),
            patch.object(
                catalog, "metadata", return_value={"metadata": {"title": "Pilot"}}
            ),
        ):
            value = catalog._hydrate_rows(account_id, [episode_row], "en")[0]

        self.assertEqual("Example Series", value["seriesName"])
        self.assertEqual("series-1", value["seriesId"])

    def seed_series_hierarchy(self):
        account = self.account().create("series", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "series-1",
                "allowed",
                None,
                "series",
                "Example",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "season-1",
                "allowed",
                "series-1",
                "season",
                "Example/Season 1",
                1,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        for entity_id, episode, title in (
            ("episode-10", 10, "Episode 10"),
            ("episode-2", 2, "Episode 2"),
            ("episode-1", 1, "Episode 1"),
        ):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "allowed",
                    "season-1",
                    "episode",
                    f"Example/Season 1/{title}",
                    1,
                    episode,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "episode-unset",
                "allowed",
                "season-1",
                "episode",
                "Example/Season 1/Unaired",
                1,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        for entity_id, season in (("season-10", 10), ("season-2", 2), ("season-3", 3)):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "allowed",
                    "series-1",
                    "season",
                    f"Example/Season {season}",
                    season,
                    None,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            )
        return account["id"]

    @staticmethod
    def patch_catalog_metadata(catalog):
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {"title": entity_id.replace("-", " ")}
        }

    def create_date_projection_tables(self):
        self.db.execute(
            "CREATE TABLE catalog_entity_rollups("
            "entity_id TEXT PRIMARY KEY,library_id TEXT,added_ns INTEGER,last_added_ns INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE catalog_projection_status("
            "library_id TEXT PRIMARY KEY,state TEXT)"
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_hierarchy_defaults_to_numeric_episode_order_and_paginates(
        self, _languages
    ):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        self.patch_catalog_metadata(catalog)

        first_page = catalog.list_items(
            user_id, "allowed", "en", parent_id="season-1", page_size=2
        )
        second_page = catalog.list_items(
            user_id, "allowed", "en", parent_id="season-1", page=2, page_size=2
        )

        self.assertEqual(
            [item["id"] for item in first_page["items"] + second_page["items"]],
            ["episode-1", "episode-2", "episode-10", "episode-unset"],
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_hierarchy_pagination_avoids_metadata_for_unselected_rows(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        calls = []

        def metadata(_user_id, entity_id, _language):
            calls.append(entity_id)
            return {"metadata": {"title": entity_id}}

        catalog.metadata = metadata

        result = catalog.list_items(
            user_id, "allowed", "en", parent_id="season-1", page_size=2
        )

        self.assertEqual(
            [item["id"] for item in result["items"]], ["episode-1", "episode-2"]
        )
        self.assertNotIn("episode-10", calls)
        self.assertNotIn("episode-unset", calls)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_episode_serialization_includes_the_resolved_series_poster(
        self, _languages
    ):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {
                "title": entity_id,
                "images": (
                    {
                        "Primary": {
                            "url": "/api/catalog/items/series-1/images/Primary?language=en"
                        }
                    }
                    if entity_id == "series-1"
                    else {}
                ),
                "date": "2026-04-01" if entity_id == "series-1" else None,
            }
        }

        result = catalog.list_items(user_id, "allowed", "en", parent_id="season-1")

        self.assertEqual(
            "/api/catalog/items/series-1/images/Primary?language=en",
            result["items"][0]["seriesPrimaryImage"]["url"],
        )
        self.assertEqual(2026, result["items"][0]["seriesProductionYear"])

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_hierarchy_defaults_to_numeric_season_order(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        self.patch_catalog_metadata(catalog)

        result = catalog.list_items(user_id, "allowed", "en", parent_id="series-1")

        self.assertEqual(
            [item["id"] for item in result["items"]],
            ["season-1", "season-2", "season-3", "season-10"],
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_explicit_title_sort_overrides_hierarchy_order(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {
                "title": {
                    "series-1": "Series",
                    "episode-1": "Zulu",
                    "episode-2": "Alpha",
                    "episode-10": "Middle",
                    "episode-unset": "Omega",
                }[entity_id]
            }
        }

        result = catalog.list_items(
            user_id, "allowed", "en", parent_id="season-1", sort_by="title"
        )

        self.assertEqual(
            [item["id"] for item in result["items"]],
            ["episode-2", "episode-10", "episode-unset", "episode-1"],
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_added_and_last_added_aggregate_playable_file_mtimes(self, _languages):
        user_id = self.seed_series_hierarchy()
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "series-2",
                "allowed",
                None,
                "series",
                "Other",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "episode-20",
                "allowed",
                "series-2",
                "episode",
                "Other/Episode",
                1,
                1,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "file-1",
                "episode-1",
                "episode-1.mkv",
                "media",
                1_700_000_000_000_000_000,
            ),
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "file-2",
                "episode-2",
                "episode-2.mkv",
                "media",
                1_800_000_000_000_000_000,
            ),
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "file-3",
                "episode-20",
                "episode-20.mkv",
                "media",
                1_750_000_000_000_000_000,
            ),
        )
        catalog = self.catalog()
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {"title": entity_id}
        }

        added = catalog.list_items(
            user_id, "allowed", "en", sort_by="added", sort_order="ascending"
        )
        latest = catalog.list_items(
            user_id, "allowed", "en", sort_by="lastAdded", sort_order="descending"
        )

        self.assertEqual(
            [item["id"] for item in added["items"]], ["series-1", "series-2"]
        )
        self.assertEqual(
            [item["id"] for item in latest["items"]], ["series-1", "series-2"]
        )
        self.assertEqual(added["items"][0]["addedAt"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(added["items"][0]["lastAddedAt"], "2027-01-15T08:00:00+00:00")

    def test_collection_date_values_follow_authorized_members(self):
        user_id = self.account().create("collection-dates", "password-123")["id"]
        self.db.execute(
            "INSERT INTO libraries VALUES('collections','Collections','collection','ready',NULL,NULL)"
        )
        for library_id in ("allowed", "collections"):
            self.db.execute(
                "INSERT INTO user_library_access VALUES(?,?,?)",
                (user_id, library_id, "now"),
            )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "source-movie",
                "allowed",
                None,
                "movie",
                "Source",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "collection-1",
                "collections",
                None,
                "collection",
                "Collection",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "CREATE TABLE collection_members(collection_entity_id TEXT,source_entity_id TEXT,position INTEGER)"
        )
        self.db.execute(
            "INSERT INTO collection_members VALUES(?,?,?)",
            ("collection-1", "source-movie", 0),
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "source-file",
                "source-movie",
                "source.mkv",
                "media",
                1_800_000_000_000_000_000,
            ),
        )

        values = self.catalog()._date_values("collections", {"allowed", "collections"})

        self.assertEqual(
            values["collection-1"]["lastAddedAt"], "2027-01-15T08:00:00+00:00"
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_library_capability_probe_uses_parent_index(self, _languages):
        user_id = self.seed_series_hierarchy()
        self.db.execute(
            "CREATE INDEX idx_library_entities_parent_id "
            "ON library_entities(parent_id) WHERE parent_id IS NOT NULL"
        )
        plan = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT parent.library_id "
            "FROM library_entities parent "
            "JOIN library_entities child ON child.parent_id=parent.id "
            "WHERE parent.library_id IN (?)",
            ("allowed",),
        )
        plan_text = " ".join(row[3] for row in plan)
        self.assertIn("SEARCH child", plan_text)
        self.assertNotIn("SCAN child", plan_text)
        libraries = self.catalog().libraries(user_id)
        self.assertTrue(libraries[0]["supportsLastAdded"])

    def test_libraries_sort_by_highest_order_first(self):
        self.db.execute(
            "ALTER TABLE libraries ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
        )
        self.db.execute(
            "INSERT INTO user_library_access VALUES('user','allowed','now'),('user','hidden','now')"
        )
        self.db.execute("UPDATE libraries SET sort_order=7 WHERE id='allowed'")
        self.db.execute("UPDATE libraries SET sort_order=-2 WHERE id='hidden'")

        values = self.catalog().libraries("user")

        self.assertEqual([value["id"] for value in values], ["allowed", "hidden"])
        self.assertEqual([value["sortOrder"] for value in values], [7, -2])

    def test_date_values_cache_overlapping_roots(self):
        self.seed_series_hierarchy()
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "file-1",
                "episode-1",
                "episode-1.mkv",
                "media",
                1_800_000_000_000_000_000,
            ),
        )
        catalog = self.catalog()
        token = catalog._read_context.set(_CatalogReadContext(catalog, "series"))
        execute = self.db.execute
        date_queries = []

        def counted_execute(query, params=None):
            if "FROM media_files WHERE role='media'" in query:
                date_queries.append(query)
            return execute(query, params)

        try:
            self.db.execute = counted_execute
            first = catalog._date_values(
                "allowed", {"allowed"}, {"episode-1", "episode-2"}
            )
            second = catalog._date_values("allowed", {"allowed"}, {"episode-1"})
        finally:
            self.db.execute = execute
            catalog._read_context.reset(token)

        self.assertEqual(first["episode-1"], second["episode-1"])
        self.assertEqual(len(date_queries), 1)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_next_up_returns_without_episode_scan_when_user_has_no_state(
        self, _languages
    ):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        queries = []
        execute = self.db.execute

        def counted_execute(query, params=None):
            queries.append(query)
            return execute(query, params)

        self.db.execute = counted_execute
        try:
            result = catalog.home_next_up(user_id, "en")
        finally:
            self.db.execute = execute

        self.assertEqual(result, [])
        self.assertFalse(any("ROW_NUMBER" in query for query in queries))

    def test_partial_rollups_compute_only_missing_requested_roots(self):
        self.seed_series_hierarchy()
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "series-2",
                "allowed",
                None,
                "series",
                "Other",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "episode-20",
                "allowed",
                "series-2",
                "episode",
                "Other/Episode",
                1,
                1,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "file-20",
                "episode-20",
                "episode-20.mkv",
                "media",
                1_900_000_000_000_000_000,
            ),
        )
        self.create_date_projection_tables()
        self.db.execute(
            "INSERT INTO catalog_projection_status VALUES(?,?)", ("allowed", "ready")
        )
        self.db.execute(
            "INSERT INTO catalog_entity_rollups VALUES(?,?,?,?)",
            (
                "series-1",
                "allowed",
                1_700_000_000_000_000_000,
                1_800_000_000_000_000_000,
            ),
        )

        values = self.catalog()._date_values(
            "allowed", {"allowed"}, {"series-1", "series-2"}
        )

        self.assertEqual(values["series-1"]["addedAt"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(values["series-1"]["lastAddedAt"], "2027-01-15T08:00:00+00:00")
        self.assertEqual(values["series-2"]["addedAt"], "2030-03-17T17:46:40+00:00")
        self.assertEqual(values["series-2"]["lastAddedAt"], "2030-03-17T17:46:40+00:00")

    def test_scanning_library_ignores_stale_rollups(self):
        self.seed_series_hierarchy()
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        for file_id, entity_id, modified_ns in (
            ("file-1", "episode-1", 2_000_000_000_000_000_000),
            ("file-2", "episode-2", 2_100_000_000_000_000_000),
        ):
            self.db.execute(
                "INSERT INTO media_files VALUES(?,?,?,?,?)",
                (file_id, entity_id, f"{entity_id}.mkv", "media", modified_ns),
            )
        self.create_date_projection_tables()
        self.db.execute("UPDATE libraries SET scan_state='scanning' WHERE id='allowed'")
        self.db.execute(
            "INSERT INTO catalog_projection_status VALUES(?,?)", ("allowed", "ready")
        )
        self.db.execute(
            "INSERT INTO catalog_entity_rollups VALUES(?,?,?,?)",
            (
                "series-1",
                "allowed",
                1_000_000_000_000_000_000,
                1_100_000_000_000_000_000,
            ),
        )

        values = self.catalog()._date_values("allowed", {"allowed"}, {"series-1"})

        self.assertEqual(values["series-1"]["addedAt"], "2033-05-18T03:33:20+00:00")
        self.assertEqual(values["series-1"]["lastAddedAt"], "2036-07-18T13:20:00+00:00")

    def test_date_fallback_is_root_scoped_and_indexed(self):
        self.seed_series_hierarchy()
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "series-2",
                "allowed",
                None,
                "series",
                "Other",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "episode-20",
                "allowed",
                "series-2",
                "episode",
                "Other/Episode",
                1,
                1,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "file-1",
                "episode-1",
                "episode-1.mkv",
                "media",
                1_800_000_000_000_000_000,
            ),
        )
        self.db.execute(
            "INSERT INTO media_files VALUES(?,?,?,?,?)",
            (
                "file-20",
                "episode-20",
                "episode-20.mkv",
                "media",
                1_900_000_000_000_000_000,
            ),
        )
        catalog = self.catalog()
        execute = self.db.execute
        recursive_queries = []

        def counted_execute(query, params=None):
            if "WITH RECURSIVE entity_tree" in query:
                recursive_queries.append((query, params))
            return execute(query, params)

        self.db.execute = counted_execute
        try:
            values = catalog._date_values("allowed", {"allowed"}, {"series-1"})
        finally:
            self.db.execute = execute

        self.assertEqual(set(values), {"series-1"})
        self.assertEqual(len(recursive_queries), 1)
        self.assertNotIn("edges(parent_id", recursive_queries[0][0])
        self.assertIn(
            "child.parent_id = entity_tree.entity_id", recursive_queries[0][0]
        )

    def test_date_recursive_plan_probes_children_by_parent_index(self):
        self.seed_series_hierarchy()
        self.db.execute(
            "CREATE INDEX idx_library_entities_parent_id "
            "ON library_entities(parent_id) WHERE parent_id IS NOT NULL"
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        plan = self.db.execute(
            """
            EXPLAIN QUERY PLAN
            WITH RECURSIVE entity_tree(root_id, entity_id) AS (
                SELECT id, id FROM library_entities
                WHERE library_id IN (?) AND id IN (?)
                UNION
                SELECT entity_tree.root_id, child.id
                FROM entity_tree
                CROSS JOIN library_entities child
                WHERE child.parent_id = entity_tree.entity_id
                  AND child.library_id IN (?)
            )
            SELECT entity_tree.root_id, MIN(media_files.modified_ns)
            FROM entity_tree
            LEFT JOIN media_files
              ON media_files.entity_id = entity_tree.entity_id
             AND media_files.role = 'media'
            GROUP BY entity_tree.root_id
            """,
            ("allowed", "series-1", "allowed"),
        )
        plan_text = " ".join(row[3] for row in plan)
        self.assertIn("SEARCH child", plan_text)
        self.assertNotIn("SCAN child", plan_text)

    def test_newest_media_plan_uses_timestamp_index(self):
        self.seed_series_hierarchy()
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        self.db.execute(
            "CREATE INDEX idx_media_files_role_modified_entity "
            "ON media_files(role, modified_ns DESC, entity_id, id)"
        )
        plan = self.db.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT f.entity_id, f.modified_ns, f.id
            FROM media_files f
            JOIN library_entities e ON e.id=f.entity_id
            WHERE f.role='media' AND e.library_id=? AND e.entity_type=?
            ORDER BY f.modified_ns DESC, f.entity_id, f.id
            LIMIT 256
            """,
            ("allowed", "episode"),
        )
        plan_text = " ".join(row[3] for row in plan)
        self.assertIn(
            "USING COVERING INDEX idx_media_files_role_modified_entity", plan_text
        )
        self.assertNotIn("USE TEMP B-TREE FOR ORDER BY", plan_text)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_continue_watching_plan_streams_from_partial_state_index(self, _languages):
        user_id = self.account().create("continue-plan", "password-123")["id"]
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (user_id, "allowed", "now"),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES('continue-movie','allowed',NULL,'movie','Continue.mkv',NULL,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, "continue-movie", 0, 0, 0, 20, 100, None, "2026"),
        )
        self.db.execute(
            "CREATE INDEX idx_user_item_state_continue "
            "ON user_item_state(user_id,COALESCE(last_played_at,updated_at) DESC,entity_id) "
            "WHERE played=0 AND duration_seconds>0 AND position_seconds>0"
        )
        catalog = self.catalog()
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {"title": entity_id}
        }
        selected_query = None
        selected_params = None
        execute = self.db.execute

        def capture_query(query, params=None):
            nonlocal selected_query, selected_params
            if "ORDER BY COALESCE(s.last_played_at,s.updated_at)" in query:
                selected_query = query
                selected_params = params
            return execute(query, params)

        self.db.execute = capture_query
        try:
            response = catalog.home_continue_watching(user_id, "en")
        finally:
            self.db.execute = execute

        self.assertEqual([item["id"] for item in response], ["continue-movie"])
        self.assertIsNotNone(selected_query)
        plan = execute("EXPLAIN QUERY PLAN " + selected_query, selected_params)
        plan_text = " ".join(str(row[3]) for row in plan)
        self.assertIn("idx_user_item_state_continue", plan_text)
        self.assertNotIn("TEMP B-TREE", plan_text)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_large_list_preloads_graph_and_state_once(self, _languages):
        user_id = self.account().create("large", "password-123")["id"]
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)", (user_id, "allowed", "now")
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        for series_index in range(157):
            series_id = f"series-{series_index}"
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    series_id,
                    "allowed",
                    None,
                    "series",
                    series_id,
                    None,
                    None,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            )
            for episode_index in range(26 if series_index < 82 else 25):
                episode_id = f"{series_id}-episode-{episode_index}"
                self.db.execute(
                    "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        episode_id,
                        "allowed",
                        series_id,
                        "episode",
                        episode_id,
                        1,
                        episode_index + 1,
                        None,
                        None,
                        "2026",
                        "2026",
                    ),
                )
                if episode_index == 0:
                    self.db.execute(
                        "INSERT INTO media_files VALUES(?,?,?,?,?)",
                        (
                            f"{episode_id}-file",
                            episode_id,
                            f"{episode_id}.mkv",
                            "media",
                            1_700_000_000_000_000_000 + series_index,
                        ),
                    )
                    self.db.execute(
                        "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
                        (user_id, episode_id, 0, 1, 1, 0, 0, None, "2026"),
                    )
        catalog = self.catalog()
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {"title": entity_id}
        }
        graph_calls = 0
        state_queries = 0
        graph = catalog._relationship_graph_uncached
        execute = self.db.execute

        def counted_graph(value):
            nonlocal graph_calls
            graph_calls += 1
            return graph(value)

        def counted_execute(query, params=None):
            nonlocal state_queries
            if query.startswith("SELECT s.entity_id,s.favorite"):
                state_queries += 1
            return execute(query, params)

        catalog._relationship_graph_uncached = counted_graph
        self.db.execute = counted_execute
        try:
            started = time.monotonic()
            result = catalog.list_items(user_id, "allowed", "en", page_size=40)
            elapsed = time.monotonic() - started
        finally:
            self.db.execute = execute

        self.assertEqual(len(result["items"]), 40)
        self.assertEqual(graph_calls, 1)
        self.assertEqual(state_queries, 1)
        self.assertLess(elapsed, 2.0)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_newly_added_uses_playable_file_times_and_leaf_items(self, _languages):
        user_id = self.seed_series_hierarchy()
        self.db.execute("UPDATE libraries SET type='tv_series' WHERE id='allowed'")
        self.db.execute(
            "INSERT INTO libraries VALUES('movies','Movies','movies','ready',NULL,NULL)"
        )
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)", (user_id, "movies", "now")
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "movie-new",
                "movies",
                None,
                "movie",
                "New",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "movie-old",
                "movies",
                None,
                "movie",
                "Old",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "hidden-movie",
                "hidden",
                None,
                "movie",
                "Hidden",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        for row in (
            (
                "episode-1-file",
                "episode-1",
                "episode-1.mkv",
                "media",
                1_700_000_000_000_000_000,
            ),
            (
                "episode-2-file",
                "episode-2",
                "episode-2.mkv",
                "media",
                1_800_000_000_000_000_000,
            ),
            (
                "movie-new-file",
                "movie-new",
                "new.mkv",
                "media",
                1_750_000_000_000_000_000,
            ),
            (
                "movie-old-file",
                "movie-old",
                "old.mkv",
                "media",
                1_600_000_000_000_000_000,
            ),
            (
                "hidden-file",
                "hidden-movie",
                "hidden.mkv",
                "media",
                1_900_000_000_000_000_000,
            ),
        ):
            self.db.execute("INSERT INTO media_files VALUES(?,?,?,?,?)", row)
        catalog = self.catalog()
        self.patch_catalog_metadata(catalog)

        home = catalog.home(user_id, "en")
        self.assertTrue(
            all(row["titleKey"] == "newlyAddedOn" for row in home["libraryRows"])
        )
        rows = {
            row["libraryId"]: row
            for row in home["libraryRows"]
            if row["titleKey"] == "newlyAddedOn"
        }

        self.assertEqual(
            [item["id"] for item in rows["allowed"]["items"]],
            ["episode-2", "episode-1"],
        )
        self.assertTrue(rows["allowed"]["stackEpisodes"])
        self.assertEqual(rows["allowed"]["items"][0]["seriesName"], "series 1")
        self.assertEqual(
            [item["id"] for item in rows["movies"]["items"]], ["movie-new", "movie-old"]
        )
        self.assertFalse(rows["movies"]["stackEpisodes"])
        self.assertNotIn("hidden", rows)

        allowed_row = catalog.home_library(user_id, "en", "allowed")
        self.assertEqual(
            [item["id"] for item in allowed_row["items"]], ["episode-2", "episode-1"]
        )
        self.assertTrue(allowed_row["stackEpisodes"])
        self.assertIsNone(catalog.home_library(user_id, "en", "hidden"))

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_featured_uses_series_added_date(self, _languages):
        user_id = self.account().create("featured-order", "password-123")["id"]
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (user_id, "allowed", "now"),
        )
        for row in (
            (
                "series",
                "allowed",
                None,
                "series",
                "Series",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
            (
                "season",
                "allowed",
                "series",
                "season",
                "Series/Season 1",
                1,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
            (
                "episode-1",
                "allowed",
                "season",
                "episode",
                "Series/Season 1/Episode 1",
                1,
                1,
                None,
                None,
                "2026",
                "2026",
            ),
            (
                "episode-2",
                "allowed",
                "season",
                "episode",
                "Series/Season 1/Episode 2",
                1,
                2,
                None,
                None,
                "2026",
                "2026",
            ),
            (
                "movie",
                "allowed",
                None,
                "movie",
                "Movie",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
            (
                "collection",
                "allowed",
                None,
                "collection",
                "Collection",
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        ):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)", row
            )
        self.db.execute(
            "CREATE TABLE catalog_entity_summary(entity_id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,playable_leaf_count INTEGER,media_file_count INTEGER,media_added_ns INTEGER,media_last_added_ns INTEGER,added_sort_ns INTEGER,last_added_sort_ns INTEGER,generation INTEGER,updated_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,payload TEXT,PRIMARY KEY(entity_id,locale))"
        )
        self.db.execute(
            "CREATE TABLE catalog_read_model_status(id INTEGER PRIMARY KEY,state TEXT)"
        )
        self.db.execute("INSERT INTO catalog_read_model_status VALUES(1,'ready')")
        for entity_id, entity_type, added_ns, last_added_ns in (
            ("series", "series", 100, 300),
            ("movie", "movie", 250, 250),
            ("collection", "collection", 275, 275),
        ):
            self.db.execute(
                "INSERT INTO catalog_entity_summary VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "allowed",
                    None,
                    entity_type,
                    1,
                    1,
                    added_ns,
                    last_added_ns,
                    added_ns,
                    last_added_ns,
                    1,
                    "2026",
                ),
            )
            self.db.execute(
                "INSERT INTO catalog_item_projection VALUES(?,?,?)",
                (entity_id, "en", json.dumps({"title": entity_id})),
            )
        catalog = self.catalog()
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {"title": entity_id}
        }

        featured = catalog.home_featured(user_id, "en")
        aggregate = catalog.home(user_id, "en")

        self.assertEqual([item["id"] for item in featured], ["movie", "series"])
        self.assertEqual(
            [item["id"] for item in aggregate["latestItems"][:2]],
            ["movie", "series"],
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_newly_added_serializes_only_selected_items(self, _languages):
        user_id = self.account().create("home-limit", "password-123")["id"]
        self.db.execute("UPDATE libraries SET type='movies' WHERE id='allowed'")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)", (user_id, "allowed", "now")
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)"
        )
        for index in range(40):
            entity_id = f"movie-{index}"
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "allowed",
                    None,
                    "movie",
                    entity_id,
                    None,
                    None,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            )
            self.db.execute(
                "INSERT INTO media_files VALUES(?,?,?,?,?)",
                (
                    f"file-{index}",
                    entity_id,
                    f"{entity_id}.mkv",
                    "media",
                    1_700_000_000_000_000_000 + index,
                ),
            )
        catalog = self.catalog()
        metadata_calls = 0

        def metadata(_user_id, entity_id, _language):
            nonlocal metadata_calls
            metadata_calls += 1
            return {"metadata": {"title": entity_id}}

        catalog.metadata = metadata
        catalog._home_discovery_items = lambda *_args: []
        catalog._home_top_rated_row = lambda *_args: None

        result = catalog.home(user_id, "en")

        self.assertEqual(len(result["libraryRows"][0]["items"]), 18)
        self.assertEqual(metadata_calls, 18)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_top_rated_rows_are_scoped_sorted_and_exclude_unrated(
        self, _languages
    ):
        user_id = self.account().create("top-rated", "password-123")["id"]
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)", (user_id, "allowed", "now")
        )
        for entity_id in ("movie-z", "movie-a", "movie-unrated"):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "allowed",
                    None,
                    "movie",
                    entity_id,
                    None,
                    None,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            )
        catalog = self.catalog()
        ratings = {
            "movie-z": (9.0, "Zulu"),
            "movie-a": (9.0, "Alpha"),
            "movie-unrated": (0, "Unrated"),
        }
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {
                "title": ratings[entity_id][1],
                "communityRating": ratings[entity_id][0],
            }
        }

        row = catalog._home_top_rated_row(
            user_id,
            "en",
            {"id": "allowed", "name": "Allowed", "type": "movies"},
        )

        self.assertIsNotNone(row)
        self.assertEqual(row["titleKey"], "topRated")
        self.assertEqual([item["id"] for item in row["items"]], ["movie-a", "movie-z"])

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_derived_rows_use_existing_catalog_and_user_state(self, _languages):
        user_id = self.account().create("derived", "password-123")["id"]
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)", (user_id, "allowed", "now")
        )
        for entity_id, path, created_at in (
            ("movie-drama", "Drama", "2026-01-01"),
            ("movie-action", "Action", "2026-02-01"),
            ("movie-drama-2", "Drama 2", "2026-03-01"),
            ("hidden-movie", "Hidden", "2026-04-01"),
        ):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "hidden" if entity_id == "hidden-movie" else "allowed",
                    None,
                    "movie",
                    path,
                    None,
                    None,
                    None,
                    None,
                    created_at,
                    created_at,
                ),
            )
        for entity_id, favorite, position, duration, last_played_at in (
            ("movie-action", 1, 0, 0, None),
            ("movie-drama", 0, 95, 100, "2026-04-02"),
            ("movie-drama-2", 0, 50, 100, "2026-04-03"),
            ("hidden-movie", 1, 100, 100, "2026-04-04"),
        ):
            self.db.execute(
                "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    entity_id,
                    favorite,
                    int(position >= duration > 0),
                    0,
                    position,
                    duration,
                    last_played_at,
                    "2026-04-04",
                ),
            )
        catalog = self.catalog()
        catalog.metadata = lambda _user_id, entity_id, _language: {
            "metadata": {
                "title": entity_id.replace("movie-", "").title(),
                "tags": {
                    "movie-drama": ["Drama", "drama"],
                    "movie-action": ["Action"],
                    "movie-drama-2": ["Drama", "Action"],
                    "hidden-movie": ["Hidden"],
                }[entity_id],
            }
        }

        derived = catalog.home_derived(user_id, "en")

        self.assertEqual([item["id"] for item in derived["myList"]], ["movie-action"])
        self.assertEqual(
            [item["id"] for item in derived["recentlyPlayed"]], ["movie-drama"]
        )
        self.assertEqual(
            [row["genre"] for row in derived["genreRows"]], ["Action", "Drama"]
        )
        self.assertEqual(
            [item["id"] for item in derived["genreRows"][0]["items"]],
            ["movie-drama-2", "movie-action"],
        )
        self.assertNotIn("hidden-movie", str(derived))

    def test_argon_session_revocation_and_legacy_password_upgrade(self):
        account = self.account()
        created = account.create("local", "password-123")
        session = account.create_session(created["id"])
        self.assertEqual(
            account.authenticate_token(session["token"])["id"], created["id"]
        )
        account.revoke(session["token"])
        self.assertIsNone(account.authenticate_token(session["token"]))

        legacy_id = "legacy-id"
        self.db.execute(
            "INSERT INTO users VALUES(?,?,?,?,0)",
            (legacy_id, "legacy", hashlib.sha256(b"legacy-pass").hexdigest(), "sha256"),
        )
        self.assertEqual(
            account.authenticate_password("legacy", "legacy-pass")["id"], legacy_id
        )
        self.assertEqual(
            self.db.execute(
                "SELECT password_scheme FROM users WHERE id=?", (legacy_id,)
            )[0][0],
            "argon2id",
        )

    def test_change_password_validates_atomically_rehashes_and_revokes_sessions(self):
        account = self.account()
        created = account.create("change-password", "password-123")
        first_session = account.create_session(created["id"])
        second_session = account.create_session(created["id"])
        original_hash = self.db.execute(
            "SELECT password FROM users WHERE id=?", (created["id"],)
        )[0][0]

        with self.assertRaises(ValueError):
            account.change_password(
                created["id"], "wrong-password", "new-password", "new-password"
            )
        with self.assertRaises(ValueError):
            account.change_password(created["id"], "password-123", "short", "short")
        with self.assertRaises(ValueError):
            account.change_password(
                created["id"], "password-123", "new-password", "different-password"
            )

        self.assertEqual(
            self.db.execute("SELECT password FROM users WHERE id=?", (created["id"],))[
                0
            ][0],
            original_hash,
        )
        self.assertIsNotNone(account.authenticate_token(first_session["token"]))
        self.assertIsNotNone(account.authenticate_token(second_session["token"]))

        account.change_password(
            created["id"], "password-123", "new-password", "new-password"
        )

        self.assertIsNone(account.authenticate_token(first_session["token"]))
        self.assertIsNone(account.authenticate_token(second_session["token"]))
        self.assertEqual(
            account.authenticate_password("change-password", "new-password")["id"],
            created["id"],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT password_scheme FROM users WHERE id=?", (created["id"],)
            )[0][0],
            "argon2id",
        )

    def test_change_password_accepts_legacy_sha256_current_password(self):
        account = self.account()
        legacy_id = "legacy-change-password"
        self.db.execute(
            "INSERT INTO users VALUES(?,?,?,?,0)",
            (
                legacy_id,
                "legacy-change-password",
                hashlib.sha256(b"legacy-pass").hexdigest(),
                "sha256",
            ),
        )

        account.change_password(
            legacy_id, "legacy-pass", "legacy-new-pass", "legacy-new-pass"
        )

        self.assertEqual(
            account.authenticate_password("legacy-change-password", "legacy-new-pass")[
                "id"
            ],
            legacy_id,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT password_scheme FROM users WHERE id=?", (legacy_id,)
            )[0][0],
            "argon2id",
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en", "ja"])
    def test_permissions_and_field_level_language_fallback(self, _languages):
        account = self.account().create("viewer", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.seed_item()
        metadata = self.catalog().metadata(account["id"], "movie", "ja")["metadata"]
        self.assertEqual(metadata["title"], "Japanese")
        self.assertEqual(metadata["overview"], "English overview")
        self.assertEqual(metadata["trailers"][0]["url"], "https://youtube.com/ja")
        # Provider metadata alone is not publishable: image endpoints expose
        # only artwork with a verified local cache file.
        self.assertNotIn("Primary", metadata["images"])
        with self.assertRaises(HTTPException) as hidden:
            self.catalog().require_library(account["id"], "hidden")
        self.assertEqual(hidden.exception.status_code, 404)
        with self.assertRaises(HTTPException) as unsupported:
            self.catalog().metadata(account["id"], "movie", "fr")
        self.assertEqual(unsupported.exception.status_code, 400)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en", "ja"])
    def test_legacy_projection_without_text_fields_falls_back_to_metadata_cache(
        self, _languages
    ):
        account = self.account().create("legacy-projection", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.seed_item()
        self.db.execute("CREATE TABLE catalog_entity_summary(entity_id TEXT)")
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,payload TEXT)"
        )
        self.db.execute(
            "CREATE TABLE catalog_read_model_status(id INTEGER PRIMARY KEY,state TEXT)"
        )
        self.db.execute("INSERT INTO catalog_read_model_status VALUES(1,'ready')")
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                "movie",
                "ja",
                json.dumps(
                    {
                        "title": "Legacy Japanese",
                        "images": {"Primary": {"url": "legacy.jpg"}},
                    }
                ),
            ),
        )

        metadata = self.catalog().metadata(account["id"], "movie", "ja")["metadata"]

        self.assertEqual(metadata["title"], "Japanese")
        self.assertEqual(metadata["overview"], "English overview")
        self.assertEqual(metadata["trailers"][0]["url"], "https://youtube.com/ja")

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en", "ja"])
    def test_projection_with_language_code_overview_falls_back_to_metadata_cache(
        self, _languages
    ):
        account = self.account().create("stale-projection", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.seed_item()
        self.db.execute("CREATE TABLE catalog_entity_summary(entity_id TEXT)")
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,payload TEXT)"
        )
        self.db.execute(
            "CREATE TABLE catalog_read_model_status(id INTEGER PRIMARY KEY,state TEXT)"
        )
        self.db.execute("INSERT INTO catalog_read_model_status VALUES(1,'ready')")
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                "movie",
                "ja",
                json.dumps(
                    {
                        "title": "Japanese",
                        "overview": "eng",
                        "images": {"Primary": {"url": "poster.jpg"}},
                        "_catalogItemProjectionSchema": 1,
                    }
                ),
            ),
        )

        metadata = self.catalog().metadata(account["id"], "movie", "ja")["metadata"]

        self.assertEqual(metadata["overview"], "English overview")

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en", "ja"])
    def test_progress_marks_played_at_ninety_percent(self, _languages):
        account = self.account().create("progress", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.seed_item()
        state = self.catalog().update_progress(
            account["id"], "movie", {"positionSeconds": 90, "durationSeconds": 100}
        )
        self.assertTrue(state["played"])
        self.assertEqual(state["playCount"], 1)
        self.assertEqual(state["positionSeconds"], 0)
        self.assertIsNone(state["playedPercentage"])

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_progress_is_ignored_when_watch_history_is_disabled(self, _languages):
        account = self.account().create("progress-disabled", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.seed_item()
        self.db.execute(
            "INSERT INTO account_preferences(user_id,watch_history_enabled) VALUES(?,0)",
            (account["id"],),
        )

        state = self.catalog().update_progress(
            account["id"], "movie", {"positionSeconds": 90, "durationSeconds": 100}
        )

        self.assertFalse(state["played"])
        self.assertEqual(state["positionSeconds"], 0)
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM user_item_state WHERE user_id=?",
                (account["id"],),
            )[0][0],
            0,
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_explicit_watched_state_remains_available_when_history_is_disabled(
        self, _languages
    ):
        account = self.account().create("manual-disabled", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.seed_item()
        self.db.execute(
            "INSERT INTO account_preferences(user_id,watch_history_enabled) VALUES(?,0)",
            (account["id"],),
        )

        state = self.catalog().update_state(account["id"], "movie", {"played": True})

        self.assertTrue(state["played"])
        self.assertEqual(state["playCount"], 1)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_clear_watch_history_preserves_favorites_and_follow_targets(
        self, _languages
    ):
        account = self.account().create("clear-history", "password-123")
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)",
            (account["id"], "allowed", "now"),
        )
        self.seed_item()
        self.db.execute("CREATE TABLE user_follow_targets(user_id TEXT,entity_id TEXT)")
        self.db.execute(
            "INSERT INTO user_follow_targets VALUES(?,?)", (account["id"], "movie")
        )
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (account["id"], "movie", 1, 1, 3, 40, 100, "played", "updated"),
        )
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (account["id"], "other", 0, 1, 2, 20, 100, "played", "updated"),
        )
        self.db.execute(
            "CREATE TABLE catalog_user_summary(user_id TEXT,entity_id TEXT,played_leaf_count INTEGER)"
        )
        self.db.execute(
            "INSERT INTO catalog_user_summary VALUES(?,?,?)",
            (account["id"], "movie", 1),
        )
        self.db.execute(
            "CREATE TABLE catalog_user_rollups(user_id TEXT,entity_id TEXT,favorite INTEGER,played INTEGER,play_count INTEGER,played_leaf_count INTEGER,unplayed_leaf_count INTEGER,position_seconds REAL,duration_seconds REAL,last_played_at TEXT,updated_at TEXT)"
        )
        self.db.execute(
            "INSERT INTO catalog_user_rollups VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (account["id"], "movie", 1, 1, 3, 1, 0, 40, 100, "played", "updated"),
        )
        self.db.execute(
            "INSERT INTO catalog_user_rollups VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (account["id"], "other", 0, 1, 2, 1, 1, 20, 100, "played", "updated"),
        )

        self.catalog().clear_watch_history(account["id"])

        state = self.db.read_execute(
            "SELECT favorite,played,play_count,position_seconds,duration_seconds,last_played_at FROM user_item_state WHERE user_id=? AND entity_id=?",
            (account["id"], "movie"),
        )[0]
        self.assertEqual(state, (1, 0, 0, 0.0, 0.0, None))
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM user_follow_targets WHERE user_id=?",
                (account["id"],),
            )[0][0],
            1,
        )
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM catalog_user_summary WHERE user_id=?",
                (account["id"],),
            )[0][0],
            0,
        )
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM user_item_state WHERE user_id=? AND favorite=0",
                (account["id"],),
            )[0][0],
            0,
        )
        legacy = self.db.read_execute(
            "SELECT favorite,played,play_count,played_leaf_count,unplayed_leaf_count,position_seconds,duration_seconds,last_played_at FROM catalog_user_rollups WHERE user_id=? AND entity_id=?",
            (account["id"], "movie"),
        )[0]
        self.assertEqual(legacy, (1, 0, 0, 0, 1, 0.0, 0.0, None))
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM catalog_user_rollups WHERE user_id=? AND favorite=0",
                (account["id"],),
            )[0][0],
            0,
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_parent_state_cascades_and_unwatch_clears_descendant_progress(
        self, _languages
    ):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()

        catalog.update_state(
            user_id,
            "episode-1",
            {"positionSeconds": 25, "durationSeconds": 100},
        )
        catalog.update_state(user_id, "series-1", {"played": True})

        for entity_id in (
            "series-1",
            "season-1",
            "episode-1",
            "episode-2",
            "episode-10",
            "episode-unset",
        ):
            state = catalog._state(user_id, entity_id)
            self.assertTrue(state["played"], entity_id)
            self.assertEqual(state["positionSeconds"], 0, entity_id)
        self.assertEqual(catalog._state(user_id, "series-1")["unplayedItemCount"], 0)

        catalog.update_state(user_id, "series-1", {"played": False})
        for entity_id in (
            "series-1",
            "season-1",
            "episode-1",
            "episode-2",
            "episode-10",
            "episode-unset",
        ):
            state = catalog._state(user_id, entity_id)
            self.assertFalse(state["played"], entity_id)
            self.assertEqual(state["positionSeconds"], 0, entity_id)
        self.assertEqual(catalog._state(user_id, "series-1")["unplayedItemCount"], 4)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_all_playable_children_mark_each_ancestor_watched(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()

        for entity_id in ("episode-1", "episode-2", "episode-10", "episode-unset"):
            catalog.update_state(user_id, entity_id, {"played": True})

        self.assertTrue(catalog._state(user_id, "season-1")["played"])
        self.assertTrue(catalog._state(user_id, "series-1")["played"])
        self.assertEqual(catalog._state(user_id, "series-1")["unplayedItemCount"], 0)
        self.assertEqual(catalog._state(user_id, "series-1")["playCount"], 1)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_parent_state_reports_permission_filtered_unplayed_count(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        self.assertEqual(catalog._state(user_id, "series-1")["unplayedItemCount"], 4)
        self.assertIsNone(catalog._state(user_id, "episode-1")["playedPercentage"])

        with self.assertRaises(HTTPException) as invalid:
            catalog.update_state(user_id, "episode-1", {"positionSeconds": "nan"})
        self.assertEqual(invalid.exception.status_code, 400)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_episode_serialization_tolerates_missing_series_metadata(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        original_metadata = catalog.metadata

        def metadata(user, entity_id, language):
            if entity_id == "series-1":
                raise HTTPException(404, "Metadata not found.")
            return original_metadata(user, entity_id, language)

        catalog.metadata = metadata
        result = catalog.list_items(user_id, "allowed", "en", parent_id="season-1")
        self.assertTrue(result["items"])
        self.assertIsNone(result["items"][0]["seriesPrimaryImage"])

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_episode_rows_include_their_series_title(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        self.patch_catalog_metadata(catalog)
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, "episode-1", 0, 0, 0, 25, 100, "2026-01-01", "2026-01-01"),
        )

        home = catalog.home(user_id, "en")

        self.assertEqual(home["continueWatching"][0]["seriesName"], "series 1")
        self.assertEqual(home["nextUp"], [])
        self.assertEqual(
            catalog.home_continue_watching(user_id, "en"),
            home["continueWatching"],
        )
        self.assertEqual(catalog.home_next_up(user_id, "en"), home["nextUp"])

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_rows_use_completion_frontier_and_exclude_partial_next(
        self, _languages
    ):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        self.patch_catalog_metadata(catalog)
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, "episode-1", 0, 1, 1, 0, 100, "2026-01-01", "2026-01-01"),
        )
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, "episode-2", 0, 0, 0, 1, 100, "2026-01-02", "2026-01-02"),
        )

        self.assertEqual(
            [item["id"] for item in catalog.home_continue_watching(user_id, "en")],
            ["episode-2"],
        )
        self.assertEqual(catalog.home_next_up(user_id, "en"), [])

        self.db.execute(
            "UPDATE user_item_state SET played=1,position_seconds=0,last_played_at=?,updated_at=? WHERE user_id=? AND entity_id=?",
            ("2026-01-03", "2026-01-03", user_id, "episode-2"),
        )
        next_up = catalog.home_next_up(user_id, "en")
        self.assertEqual([item["id"] for item in next_up], ["episode-10"])

        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "episode-11",
                "allowed",
                "season-1",
                "episode",
                "Example/Season 1/Episode 11",
                1,
                11,
                None,
                None,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, "episode-10", 0, 1, 1, 0, 100, "2026-01-04", "2026-01-04"),
        )
        self.assertEqual(
            [item["id"] for item in catalog.home_next_up(user_id, "en")],
            ["episode-11"],
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_next_up_requires_published_candidate(self, _languages):
        user_id = self.seed_series_hierarchy()
        catalog = self.catalog()
        self.patch_catalog_metadata(catalog)
        self.db.execute(
            "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, "episode-1", 0, 1, 1, 0, 100, "2026-01-01", "2026-01-01"),
        )
        self.db.execute(
            "CREATE TABLE catalog_entity_summary(entity_id TEXT PRIMARY KEY)"
        )
        catalog._read_model_ready = lambda: True

        self.assertEqual(catalog.home_next_up(user_id, "en"), [])
        self.db.execute("INSERT INTO catalog_entity_summary VALUES(?)", ("episode-2",))
        self.assertEqual(
            [item["id"] for item in catalog.home_next_up(user_id, "en")],
            ["episode-2"],
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_home_continue_includes_sub_two_percent_progress_and_orders_latest(
        self, _languages
    ):
        user_id = self.account().create("home-progress", "password-123")["id"]
        self.db.execute(
            "INSERT INTO user_library_access VALUES(?,?,?)", (user_id, "allowed", "now")
        )
        for entity_id, path in (("movie-old", "Old"), ("movie-new", "New")):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "allowed",
                    None,
                    "movie",
                    path,
                    None,
                    None,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            )
        for state in (
            (user_id, "movie-old", 0, 0, 0, 1, 100, "2026-01-01", "2026-01-01"),
            (user_id, "movie-new", 0, 0, 0, 0.1, 100, "2026-01-02", "2026-01-02"),
        ):
            self.db.execute(
                "INSERT INTO user_item_state VALUES(?,?,?,?,?,?,?,?,?)", state
            )
        catalog = self.catalog()
        self.patch_catalog_metadata(catalog)
        self.assertEqual(
            [item["id"] for item in catalog.home_continue_watching(user_id, "en")],
            ["movie-new", "movie-old"],
        )


if __name__ == "__main__":
    unittest.main()
