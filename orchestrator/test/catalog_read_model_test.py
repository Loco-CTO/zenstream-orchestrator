import unittest
from unittest.mock import patch

from app.catalog_read_model import CatalogReadModel
from app.catalog import Catalog
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
            "CREATE TABLE catalog_item_genres(entity_id TEXT,locale TEXT,genre_key TEXT,genre_name TEXT,PRIMARY KEY(entity_id,locale,genre_key))",
            "CREATE TABLE catalog_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,PRIMARY KEY(gram,entity_id,locale))",
            "CREATE TABLE catalog_read_model_status(id INTEGER PRIMARY KEY,state TEXT,generation INTEGER,updated_at TEXT,error TEXT)",
        ):
            self.db.execute(statement)
        self.db.execute("INSERT INTO libraries VALUES('library','Library','tv_series','scanning',NULL,'2026')")
        self.db.execute("INSERT INTO libraries VALUES('collection','Collection','collection','ready',NULL,'2026')")
        self.db.execute("INSERT INTO users VALUES('user')")
        self.db.execute("CREATE TABLE user_library_access(user_id TEXT,library_id TEXT,created_at TEXT,PRIMARY KEY(user_id,library_id))")
        self.db.execute("INSERT INTO user_library_access VALUES('user','library','2026')")
        self.db.execute("INSERT INTO library_entities VALUES('series','library',NULL,'series','Series',NULL,NULL,NULL,NULL,NULL,'2026','2026')")
        self.db.execute("INSERT INTO library_entities VALUES('season','library','series','season','Series/Season 1',1,NULL,NULL,NULL,NULL,'2026','2026')")
        self.db.execute("INSERT INTO library_entities VALUES('episode-1','library','season','episode','Series/Season 1/Episode 1.mkv',1,1,NULL,NULL,NULL,'2026','2026')")
        self.db.execute("INSERT INTO library_entities VALUES('episode-2','library','season','episode','Series/Season 1/Episode 2.mkv',1,2,NULL,NULL,NULL,'2026','2026')")
        self.db.execute("INSERT INTO library_entities VALUES('collection-item','collection',NULL,'collection','Collection',NULL,NULL,NULL,NULL,NULL,'2026','2026')")
        self.db.execute("INSERT INTO collection_members VALUES('collection-item','series',1)")
        self.db.execute("INSERT INTO media_files VALUES('file-1','episode-1','Episode 1.mkv','media',10)")
        self.db.execute("INSERT INTO media_files VALUES('file-2','episode-2','Episode 2.mkv','media',20)")

    def tearDown(self):
        self.db.close()

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_rebuild_propagates_file_extrema_and_collection_contributions(self, _languages):
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

    @patch("app.catalog_read_model.MetadataLanguageSettings.get", return_value=["en"])
    def test_refresh_root_handles_timestamp_decrease_without_library_scan(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        self.db.execute("UPDATE media_files SET modified_ns=5 WHERE id='file-2'")
        model.refresh_roots(["series"])
        values = self.db.read_execute(
            "SELECT added_sort_ns,last_added_sort_ns FROM catalog_entity_summary WHERE entity_id='series'"
        )[0]
        self.assertEqual(values, (5, 10))

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
        response = catalog.list_items("user", "library", "en", parent_id="season", page_size=1, sort_by="lastAdded", sort_order="descending")
        self.assertEqual(response["total"], 2)
        self.assertEqual(response["items"][0]["id"], "episode-2")
        self.assertEqual(response["items"][0]["lastAddedAt"], "1970-01-01T00:00:00+00:00")

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en"])
    def test_short_search_uses_read_model_grams_before_hydration(self, _languages):
        model = CatalogReadModel(self.db)
        model.rebuild(["en"])
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        response = catalog.search("user", "Se", "en", 1, 10)
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["items"][0]["id"], "series")


if __name__ == "__main__":
    unittest.main()
