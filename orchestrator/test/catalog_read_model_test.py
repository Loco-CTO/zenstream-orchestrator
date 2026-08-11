import unittest
from unittest.mock import patch

from app.catalog import Catalog
from app.catalog_read_model import CatalogReadModel
from app.database import DatabaseHandler


class CatalogReadModelTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE libraries(id TEXT PRIMARY KEY,name TEXT,type TEXT,scan_state TEXT,last_scan_finished_at TEXT,created_at TEXT)",
            "CREATE TABLE users(id TEXT PRIMARY KEY)",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER,episode_end_number INTEGER,disc_number INTEGER,track_number INTEGER,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)",
            "CREATE TABLE user_item_state(user_id TEXT,entity_id TEXT,played INTEGER,favorite INTEGER,play_count INTEGER,position_seconds REAL,duration_seconds REAL,last_played_at TEXT,PRIMARY KEY(user_id,entity_id))",
            "CREATE TABLE collection_members(collection_entity_id TEXT,source_entity_id TEXT,position INTEGER)",
            "CREATE TABLE catalog_entity_summary(entity_id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,playable_leaf_count INTEGER,media_file_count INTEGER,media_added_ns INTEGER,media_last_added_ns INTEGER,added_sort_ns INTEGER,last_added_sort_ns INTEGER,generation INTEGER,updated_at TEXT)",
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,entity_type TEXT,payload TEXT,title_sort TEXT,rating_sort REAL,release_sort TEXT,runtime_sort REAL,updated_at TEXT,generation INTEGER,PRIMARY KEY(entity_id,locale))",
            "CREATE TABLE catalog_user_summary(user_id TEXT,entity_id TEXT,played_leaf_count INTEGER,updated_at TEXT,PRIMARY KEY(user_id,entity_id))",
            "CREATE TABLE catalog_collection_summary(collection_entity_id TEXT,collection_library_id TEXT,source_library_id TEXT,playable_leaf_count INTEGER,media_file_count INTEGER,added_sort_ns INTEGER,last_added_sort_ns INTEGER,updated_at TEXT,PRIMARY KEY(collection_entity_id,source_library_id))",
            "CREATE TABLE catalog_collection_member_projection(collection_entity_id TEXT,source_entity_id TEXT,source_library_id TEXT,position INTEGER,updated_at TEXT,PRIMARY KEY(collection_entity_id,source_entity_id))",
            "CREATE TABLE catalog_item_genres(entity_id TEXT,locale TEXT,library_id TEXT,entity_type TEXT,genre_key TEXT,genre_name TEXT,PRIMARY KEY(entity_id,locale,genre_key))",
            "CREATE TABLE catalog_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,PRIMARY KEY(gram,entity_id,locale))",
            "CREATE TABLE catalog_root_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,title_sort TEXT,PRIMARY KEY(gram,entity_id,locale))",
            "CREATE TABLE catalog_library_summary(library_id TEXT PRIMARY KEY,generation INTEGER,supports_last_added INTEGER,last_root_entity_id TEXT,updated_at TEXT)",
            "CREATE TABLE catalog_artwork_selection(entity_id TEXT,locale TEXT,image_type TEXT,provider TEXT,local_path TEXT,blur_hash TEXT,version TEXT,updated_at TEXT,PRIMARY KEY(entity_id,locale,image_type))",
            "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,is_primary INTEGER)",
            "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,local_path TEXT,fetched_at TEXT,blur_hash TEXT)",
            "CREATE TABLE catalog_read_model_status(id INTEGER PRIMARY KEY,state TEXT,generation INTEGER,updated_at TEXT,error TEXT)",
            "CREATE INDEX idx_catalog_item_projection_title ON catalog_item_projection(library_id,parent_id,locale,title_sort,entity_id)",
            "CREATE INDEX idx_catalog_entity_summary_parent_last ON catalog_entity_summary(library_id,parent_id,last_added_sort_ns DESC,entity_id)",
            "CREATE INDEX idx_catalog_root_search_grams_lookup ON catalog_root_search_grams(gram,locale,library_id,entity_id)",
            "CREATE INDEX idx_catalog_item_genres_covering ON catalog_item_genres(locale,library_id,entity_type,genre_key,entity_id)",
        ):
            self.db.execute(statement)
        self.db.execute(
            "INSERT INTO libraries VALUES('library','Library','tv_series','scanning',NULL,'2026')"
        )
        self.db.execute(
            "INSERT INTO libraries VALUES('collection','Collection','collection','ready',NULL,'2026')"
        )
        self.db.execute("INSERT INTO users VALUES('user')")
        self.db.execute(
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT,created_at TEXT,PRIMARY KEY(user_id,library_id))"
        )
        self.db.execute(
            "INSERT INTO user_library_access VALUES('user','library','2026')"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES('series','library',NULL,'series','Series',NULL,NULL,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES('season','library','series','season','Series/Season 1',1,NULL,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES('episode-1','library','season','episode','Series/Season 1/Episode 1.mkv',1,1,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES('episode-2','library','season','episode','Series/Season 1/Episode 2.mkv',1,2,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES('collection-item','collection',NULL,'collection','Collection',NULL,NULL,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO collection_members VALUES('collection-item','series',1)"
        )
        self.db.execute(
            "INSERT INTO media_files VALUES('file-1','episode-1','Episode 1.mkv','media',10)"
        )
        self.db.execute(
            "INSERT INTO media_files VALUES('file-2','episode-2','Episode 2.mkv','media',20)"
        )

    def tearDown(self):
        self.db.close()

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_rebuild_propagates_file_extrema_and_collection_contributions(
        self, _languages
    ):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        series = self.db.read_execute(
            "SELECT playable_leaf_count,media_file_count,added_sort_ns,last_added_sort_ns FROM catalog_entity_summary WHERE entity_id='series'"
        )[0]
        self.assertEqual(series[:2], (2, 2))
        self.assertEqual(series[2:], (10, 20))
        collection = self.db.read_execute(
            "SELECT playable_leaf_count,added_sort_ns,last_added_sort_ns FROM catalog_collection_summary WHERE collection_entity_id='collection-item'"
        )[0]
        self.assertEqual(collection, (2, 10, 20))
        self.assertEqual(
            self.db.read_execute(
                "SELECT collection_entity_id,source_entity_id,source_library_id,position FROM catalog_collection_member_projection"
            ),
            [("collection-item", "series", "library", 1)],
        )

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_rebuild_backfills_cached_artwork_selection(self, _languages):
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES('series','tvdb','series','1',1)"
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES('series','en','library',NULL,'series','{\"title\":\"Series\"}','series',0,'',0,'2026',1)"
        )
        self.db.execute(
            "INSERT INTO metadata_images VALUES('tvdb','series','1','en','Primary','url','cached.webp','2026','blur')"
        )
        CatalogReadModel(self.db).rebuild(["en"])
        # A metadata row is not publishable unless its cache file exists and
        # is non-empty.
        self.assertEqual(
            self.db.read_execute(
                "SELECT provider,local_path,blur_hash FROM catalog_artwork_selection WHERE entity_id='series' AND locale='en' AND image_type='Primary'"
            ),
            [],
        )

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_short_search_grams_contain_only_catalog_roots(self, _languages):
        CatalogReadModel(self.db).rebuild(["en"])
        entities = {
            row[0]
            for row in self.db.read_execute(
                "SELECT DISTINCT entity_id FROM catalog_root_search_grams"
            )
        }
        self.assertEqual(entities, {"series", "collection-item"})

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_multi_root_and_deletion_publications_are_library_wide(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute(
            "INSERT INTO library_entities VALUES('series-2','library',NULL,'series','Series 2',NULL,NULL,NULL,NULL,NULL,'2026','2026')"
        )
        model.refresh_roots(["series", "series-2"])
        generation, last_root = self.db.read_execute(
            "SELECT generation,last_root_entity_id FROM catalog_library_summary WHERE library_id='library'"
        )[0]
        self.assertEqual((generation, last_root), (2, None))
        model.refresh_roots([], affected_library_ids=["library"])
        self.assertEqual(
            self.db.read_execute(
                "SELECT generation,last_root_entity_id FROM catalog_library_summary WHERE library_id='library'"
            )[0],
            (3, None),
        )

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_refresh_root_handles_timestamp_decrease_without_library_scan(
        self, _languages
    ):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        initial_generation = model.status()[1]
        self.db.execute("UPDATE media_files SET modified_ns=5 WHERE id='file-2'")
        model.refresh_roots(["series"])
        values = self.db.read_execute(
            "SELECT added_sort_ns,last_added_sort_ns FROM catalog_entity_summary WHERE entity_id='series'"
        )[0]
        self.assertEqual(values, (5, 10))
        self.assertEqual(model.status()[1], initial_generation + 1)
        library = self.db.read_execute(
            "SELECT generation,last_root_entity_id FROM catalog_library_summary WHERE library_id='library'"
        )[0]
        self.assertEqual(library, (2, "series"))

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_refresh_root_preserves_localized_and_original_title_grams(
        self, _languages
    ):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute(
            "UPDATE catalog_item_projection SET payload=?,title_sort=? WHERE entity_id='series' AND locale='en'",
            (
                '{"title":"Localized Name","originalTitle":"Original Name"}',
                "localized name",
            ),
        )
        model.refresh_roots(["series"])
        indexed = set(
            self.db.read_execute(
                "SELECT locale,title_sort FROM catalog_root_search_grams WHERE entity_id='series'"
            )
        )
        self.assertIn(("en", "localized name"), indexed)
        self.assertIn(("original", "original name"), indexed)
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        self.assertEqual(catalog.search("user", "localized", "en", 1, 10)["total"], 1)
        self.assertEqual(catalog.search("user", "original", "en", 1, 10)["total"], 1)

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_refresh_root_admits_new_collection_entity(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute(
            "INSERT INTO library_entities VALUES('collection-2','collection',NULL,'collection','Collection 2',NULL,NULL,NULL,NULL,NULL,'2026','2026')"
        )
        self.db.execute(
            "INSERT INTO collection_members VALUES('collection-2','series',1)"
        )

        model.refresh_roots(["collection-2"])

        self.assertEqual(
            self.db.read_execute(
                "SELECT entity_id FROM catalog_entity_summary WHERE entity_id='collection-2'"
            ),
            [("collection-2",)],
        )

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_bootstrap_repairs_small_summary_gap_without_full_rebuild(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute("DELETE FROM catalog_entity_summary WHERE entity_id='series'")
        self.db.execute("UPDATE catalog_read_model_status SET state='ready' WHERE id=1")

        with patch.object(
            model, "rebuild", side_effect=AssertionError("unexpected full rebuild")
        ):
            result = model.bootstrap(["en"])

        self.assertEqual(result, 5)
        self.assertEqual(
            self.db.read_execute("SELECT COUNT(*) FROM catalog_entity_summary")[0][0],
            5,
        )

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_building_status_forces_full_rebuild_even_with_small_gap(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute("DELETE FROM catalog_entity_summary WHERE entity_id='series'")
        self.db.execute(
            "UPDATE catalog_read_model_status SET state='building' WHERE id=1"
        )
        with patch.object(model, "rebuild", wraps=model.rebuild) as rebuild:
            result = model.bootstrap(["en"])
        self.assertEqual(result, 5)
        rebuild.assert_called_once_with(["en"])

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_bootstrap_repairs_small_projection_gap_without_full_rebuild(
        self, _languages
    ):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute(
            "DELETE FROM catalog_item_projection WHERE entity_id='episode-1' AND locale='en'"
        )

        with patch.object(
            model, "rebuild", side_effect=AssertionError("unexpected full rebuild")
        ):
            result = model.bootstrap(["en"])

        self.assertEqual(result, 5)
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM catalog_item_projection WHERE entity_id='episode-1' AND locale='en'"
            )[0][0],
            1,
        )

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_bootstrap_defers_unpublished_scan_inventory(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute(
            "CREATE TABLE library_jobs(id TEXT PRIMARY KEY,library_id TEXT,kind TEXT,state TEXT)"
        )
        self.db.execute(
            "INSERT INTO library_jobs VALUES('job','library','scan','running')"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES('unpublished','library','series','episode','Series/Season 1/Episode 3.mkv',1,3,NULL,NULL,NULL,'2026','2026')"
        )

        with patch.object(
            model, "rebuild", side_effect=AssertionError("unexpected full rebuild")
        ):
            result = model.bootstrap(["en"])

        self.assertEqual(result, 5)
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM catalog_entity_summary WHERE entity_id='unpublished'"
            )[0][0],
            0,
        )

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_missing_metadata_still_gets_deterministic_projection(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        projection = self.db.read_execute(
            "SELECT payload,title_sort FROM catalog_item_projection WHERE entity_id='series' AND locale='en'"
        )[0]
        self.assertIn("Series", projection[0])
        self.assertEqual(projection[1], "series")

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_catalog_list_selects_one_page_from_summary(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        response = catalog.list_items(
            "user",
            "library",
            "en",
            parent_id="season",
            page_size=1,
            sort_by="lastAdded",
            sort_order="descending",
        )
        self.assertEqual(response["total"], 2)
        self.assertEqual(response["items"][0]["id"], "episode-2")
        self.assertEqual(
            response["items"][0]["lastAddedAt"], "1970-01-01T00:00:00+00:00"
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_projection_first_title_plan_avoids_temp_ordering_tree(self, _languages):
        CatalogReadModel(self.db).rebuild(["en"])
        for direction in ("ASC", "DESC"):
            plan = self.db.read_execute(
                "EXPLAIN QUERY PLAN SELECT e.id FROM catalog_item_projection p "
                "JOIN library_entities e ON e.id=p.entity_id "
                "WHERE p.locale='en' AND p.library_id='library' AND p.parent_id IS 'season' "
                f"ORDER BY p.title_sort {direction},p.entity_id {direction} LIMIT 40"
            )
            detail = " ".join(str(row[3]) for row in plan)
            self.assertIn("idx_catalog_item_projection_title", detail)
            self.assertNotIn("TEMP B-TREE", detail)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_detail_state_does_not_build_global_relationship_graph(self, _languages):
        CatalogReadModel(self.db).rebuild(["en"])
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        query_count = 0
        original_read = self.db.read_execute

        def counted_read(query, params=None):
            nonlocal query_count
            query_count += 1
            return original_read(query, params)

        self.db.read_execute = counted_read
        with patch.object(
            catalog,
            "_relationship_graph",
            side_effect=AssertionError("detail loaded global graph"),
        ):
            response = catalog.item("user", "series", "en")
        self.assertEqual(response["id"], "series")

        query_count = 0
        with patch.object(
            catalog,
            "_relationship_graph",
            side_effect=AssertionError("composite detail loaded global graph"),
        ):
            detail = catalog.detail("user", "series", "en")
        self.assertEqual(detail["selectedSeasonId"], "season")
        self.assertLessEqual(query_count, 12)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_short_search_uses_read_model_grams_before_hydration(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        response = catalog.search("user", "Se", "en", 1, 10)
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["items"][0]["id"], "series")

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_fts_search_does_not_use_bm25_inside_grouped_read_model_query(
        self, _languages
    ):
        self.db.execute(
            "CREATE VIRTUAL TABLE catalog_search USING fts5(entity_id UNINDEXED, library_id UNINDEXED, locale UNINDEXED, title, tokenize='trigram')"
        )
        self.db.execute(
            "INSERT INTO catalog_search(entity_id,library_id,locale,title) VALUES('series','library','en','Series')"
        )
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        response = catalog.search("user", "Series", "en", 1, 10)
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["items"][0]["id"], "series")

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_catalog_search_ranks_typo_candidates_by_score_before_pagination(
        self, _languages
    ):
        for entity_id, title in (
            ("gintama", "Gintama"),
            ("gintama-chronicles", "Gintama Chronicles"),
            ("the-gintama-story", "The Gintama Story"),
            ("gintara", "Gintara"),
        ):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "library",
                    None,
                    "series",
                    title,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "2026",
                    "2026",
                ),
            )
            self.db.execute(
                "INSERT INTO catalog_item_projection(entity_id,locale,library_id,parent_id,entity_type,payload,title_sort,rating_sort,release_sort,runtime_sort,updated_at,generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "en",
                    "library",
                    None,
                    "series",
                    '{"title":"' + title + '"}',
                    title.casefold(),
                    0,
                    "",
                    0,
                    "2026",
                    1,
                ),
            )
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db

        response = catalog.search("user", "gintma", "en", 1, 2)
        self.assertEqual(response["total"], 4)
        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["gintama", "gintama-chronicles"],
        )
        self.assertEqual(
            [
                item["id"]
                for item in catalog.search("user", "gintma", "en", 2, 2)["items"]
            ],
            ["the-gintama-story", "gintara"],
        )

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_search_out_of_range_page_preserves_total(self, _languages):
        CatalogReadModel(self.db).rebuild(["en"])
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        response = catalog.search("user", "se", "en", 2, 10)
        self.assertEqual(response["items"], [])
        self.assertEqual(response["total"], 1)


if __name__ == "__main__":
    unittest.main()
