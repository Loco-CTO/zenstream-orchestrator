import unittest
from unittest.mock import patch

from app.catalog import Catalog
from app.database import DatabaseHandler
from app.models.metadata import MetadataLanguageSettings
from app.search_scoring import trigram_set
from fastapi import HTTPException


class CatalogSearchTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE libraries(id TEXT PRIMARY KEY,name TEXT,type TEXT,scan_state TEXT,last_scan_finished_at TEXT,directory TEXT)",
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT,created_at TEXT,PRIMARY KEY(user_id,library_id))",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER,episode_end_number INTEGER,track_number INTEGER,created_at TEXT,updated_at TEXT)",
        ):
            self.db.execute(statement)
        self.db.execute(
            "INSERT INTO libraries VALUES('allowed','Allowed','movies','ready',NULL,NULL)"
        )
        self.db.execute(
            "INSERT INTO libraries VALUES('hidden','Hidden','movies','ready',NULL,NULL)"
        )
        self.db.execute(
            "INSERT INTO user_library_access VALUES('user','allowed','now')"
        )
        self._insert_entity("movie-1", "allowed", "movie")
        self._insert_entity("series-1", "allowed", "series")
        self._insert_entity("artist-1", "allowed", "artist")
        self._insert_entity("hidden-1", "hidden", "movie")

    def tearDown(self):
        self.db.close()

    def _insert_entity(self, entity_id, library_id, entity_type):
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                entity_id,
                library_id,
                None,
                entity_type,
                entity_id,
                None,
                None,
                None,
                None,
                "2026",
                "2026",
            ),
        )

    def _catalog(self):
        value = Catalog.__new__(Catalog)
        value.db = self.db
        return value

    def _hydrate(self, _user_id, rows, _language):
        return [{"id": row[0], "type": row[3]} for row in rows]

    def _seed_legacy_index(self):
        self.db.execute(
            "CREATE TABLE catalog_search(entity_id TEXT,library_id TEXT,locale TEXT,title TEXT)"
        )
        for entity_id, library_id, entity_type in (
            ("movie-1", "allowed", "movie"),
            ("series-1", "allowed", "series"),
            ("artist-1", "allowed", "artist"),
            ("hidden-1", "hidden", "movie"),
        ):
            self.db.execute(
                "INSERT INTO catalog_search VALUES(?,?,?,?)",
                (entity_id, library_id, "en", f"Livid {entity_type}"),
            )

    def _seed_read_index(self):
        self.db.execute(
            "CREATE TABLE catalog_entity_summary(entity_id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)"
        )
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,entity_type TEXT,payload TEXT,title_sort TEXT)"
        )
        self.db.execute(
            "CREATE TABLE catalog_read_model_status(id INTEGER PRIMARY KEY,state TEXT)"
        )
        self.db.execute("INSERT INTO catalog_read_model_status VALUES(1,'ready')")
        self.db.execute(
            "CREATE TABLE catalog_root_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,title_sort TEXT)"
        )
        for entity_id, library_id, entity_type in (
            ("movie-1", "allowed", "movie"),
            ("series-1", "allowed", "series"),
            ("artist-1", "allowed", "artist"),
            ("hidden-1", "hidden", "movie"),
        ):
            title = f"livid {entity_type}"
            for gram in trigram_set(title):
                self.db.execute(
                    "INSERT INTO catalog_root_search_grams VALUES(?,?,?,?,?)",
                    (gram, entity_id, "en", library_id, title),
                )

    def _search(self, catalog, **options):
        with (
            patch.object(MetadataLanguageSettings, "get", return_value=["en"]),
            patch.object(catalog, "_hydrate_rows", side_effect=self._hydrate),
            patch.object(
                catalog,
                "_serialize",
                side_effect=lambda _user_id, row, _metadata, **_kwargs: {
                    "id": row[0],
                    "type": row[3],
                },
            ),
        ):
            return catalog.search("user", "livid", "en", **options)

    def test_legacy_search_returns_authorized_facets_and_type_pages(self):
        self._seed_legacy_index()
        result = self._search(self._catalog(), page=1, page_size=2)

        self.assertEqual(result["total"], 3)
        self.assertEqual(
            result["facets"],
            {
                "all": 3,
                "movie": 1,
                "series": 1,
                "collection": 0,
                "release": 0,
                "artist": 1,
                "track": 0,
            },
        )
        filtered = self._search(
            self._catalog(), page=1, page_size=1, entity_type="series"
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual([item["id"] for item in filtered["items"]], ["series-1"])
        self.assertNotIn("hidden-1", [item["id"] for item in result["items"]])

    def test_read_model_search_returns_facets_and_paginates_filtered_matches(self):
        self._seed_read_index()
        catalog = self._catalog()

        first_page = self._search(catalog, page=1, page_size=1, entity_type="movie")
        second_page = self._search(catalog, page=2, page_size=1, entity_type="movie")

        self.assertEqual(first_page["total"], 1)
        self.assertEqual(second_page["total"], 1)
        self.assertEqual(first_page["facets"]["all"], 3)
        self.assertEqual([item["id"] for item in first_page["items"]], ["movie-1"])
        self.assertEqual(second_page["items"], [])

    def test_search_rejects_unknown_entity_type(self):
        self._seed_legacy_index()
        with self.assertRaises(HTTPException) as error:
            self._search(self._catalog(), page=1, page_size=20, entity_type="episode")
        self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
