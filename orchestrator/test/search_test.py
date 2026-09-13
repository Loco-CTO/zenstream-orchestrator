import unittest
from unittest.mock import patch

from app.catalog import Catalog
from app.database import DatabaseHandler
from app.models.metadata import MetadataLanguageSettings
from app.search_scoring import match_score, trigram_set
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

    def _seed_legacy_search_rows(self, rows):
        self.db.execute(
            "CREATE TABLE catalog_search(entity_id TEXT,library_id TEXT,locale TEXT,title TEXT)"
        )
        for row in rows:
            self.db.execute("INSERT INTO catalog_search VALUES(?,?,?,?)", row)

    def _seed_read_index(self, indexed_rows=None):
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
        indexed_rows = indexed_rows or (
            ("movie-1", "allowed", "movie", "livid movie"),
            ("series-1", "allowed", "series", "livid series"),
            ("artist-1", "allowed", "artist", "livid artist"),
            ("hidden-1", "hidden", "movie", "livid movie"),
        )
        for entity_id, library_id, _entity_type, title in indexed_rows:
            for gram in trigram_set(title):
                self.db.execute(
                    "INSERT INTO catalog_root_search_grams VALUES(?,?,?,?,?)",
                    (gram, entity_id, "en", library_id, title),
                )

    def _search(self, catalog, query="livid", **options):
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
            return catalog.search("user", query, "en", **options)

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

    def test_search_uses_server_relevance_order_across_pages_and_deduplicates(self):
        self._insert_entity("release-1", "allowed", "release")
        self._seed_legacy_search_rows(
            [
                ("movie-1", "allowed", "en", "Dune Story"),
                ("series-1", "allowed", "en", "Dune Storybook"),
                ("artist-1", "allowed", "en", "Dune in the Story"),
                ("release-1", "allowed", "en", "Undune Story"),
                ("movie-1", "allowed", "original", "Dune Story"),
            ]
        )
        catalog = self._catalog()

        first_page = self._search(catalog, query="dune story", page=1, page_size=2)
        second_page = self._search(catalog, query="dune story", page=2, page_size=2)
        filtered = self._search(
            catalog, query="dune story", page=1, page_size=1, entity_type="series"
        )

        self.assertEqual(
            [item["id"] for item in first_page["items"]], ["movie-1", "series-1"]
        )
        self.assertEqual(
            [item["id"] for item in second_page["items"]], ["release-1", "artist-1"]
        )
        self.assertEqual(first_page["total"], 4)
        self.assertEqual(second_page["total"], 4)
        self.assertEqual(
            first_page["facets"],
            {
                "all": 4,
                "movie": 1,
                "series": 1,
                "collection": 0,
                "release": 1,
                "artist": 1,
                "track": 0,
            },
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual([item["id"] for item in filtered["items"]], ["series-1"])

    def test_search_score_distinguishes_exact_prefix_partial_and_word_prefix(self):
        query = "dune story"
        exact = match_score(query, "Dune Story")
        prefix = match_score(query, "Dune Storybook")
        partial = match_score(query, "Undune Story")
        word_prefix = match_score(query, "Dune in the Story")

        self.assertGreater(exact, prefix)
        self.assertGreater(prefix, partial)
        self.assertGreater(partial, word_prefix)

    def test_read_model_search_preserves_relevance_order_before_pagination(self):
        self._insert_entity("release-1", "allowed", "release")
        self._seed_read_index(
            [
                ("movie-1", "allowed", "movie", "dune story"),
                ("series-1", "allowed", "series", "dune storybook"),
                ("artist-1", "allowed", "artist", "dune in the story"),
                ("release-1", "allowed", "release", "undune story"),
                ("hidden-1", "hidden", "movie", "dune story"),
            ]
        )
        catalog = self._catalog()

        first_page = self._search(catalog, query="dune story", page=1, page_size=2)
        second_page = self._search(catalog, query="dune story", page=2, page_size=2)

        self.assertEqual(
            [item["id"] for item in first_page["items"]], ["movie-1", "series-1"]
        )
        self.assertEqual(
            [item["id"] for item in second_page["items"]], ["release-1", "artist-1"]
        )
        self.assertEqual(first_page["total"], 4)
        self.assertEqual(first_page["facets"]["all"], 4)

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
