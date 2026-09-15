import json
import unittest
from unittest.mock import Mock

from app.catalog import Catalog
from app.database import DatabaseHandler


class MusicCatalogPageTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE libraries(id TEXT PRIMARY KEY,name TEXT,type TEXT,scan_state TEXT,last_scan_finished_at TEXT,created_at TEXT)",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER,episode_end_number INTEGER,disc_number INTEGER,track_number INTEGER,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,relative_path TEXT,role TEXT,modified_ns INTEGER)",
            "CREATE TABLE catalog_entity_summary(entity_id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,playable_leaf_count INTEGER,media_file_count INTEGER,media_added_ns INTEGER,media_last_added_ns INTEGER,added_sort_ns INTEGER,last_added_sort_ns INTEGER,generation INTEGER,updated_at TEXT)",
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,entity_type TEXT,payload TEXT,title_sort TEXT,rating_sort REAL,release_sort TEXT,runtime_sort REAL,updated_at TEXT,generation INTEGER,PRIMARY KEY(entity_id,locale))",
            "CREATE TABLE catalog_music_album_page(release_id TEXT,locale TEXT,library_id TEXT,artist_id TEXT,title_sort TEXT,release_sort TEXT,added_sort_ns INTEGER,last_added_sort_ns INTEGER,updated_at TEXT,PRIMARY KEY(release_id,locale))",
            "CREATE TABLE catalog_music_album_page_status(library_id TEXT PRIMARY KEY,state TEXT,generation INTEGER,updated_at TEXT,error TEXT)",
        ):
            self.db.execute(statement)
        self.db.execute(
            "INSERT INTO libraries VALUES('music','Music','music','ready',NULL,'2026')"
        )
        self.db.execute(
            "INSERT INTO catalog_music_album_page_status VALUES('music','ready',1,'2026',NULL)"
        )
        for release_id, title, added in (
            ("release-a", "Alpha", 10),
            ("release-b", "Beta", 20),
            ("release-c", "Charlie", 30),
        ):
            self.db.execute(
                "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    "music",
                    "artist",
                    "release",
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
                "INSERT INTO catalog_entity_summary VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    "music",
                    "artist",
                    "release",
                    1,
                    1,
                    added,
                    added,
                    added,
                    added,
                    1,
                    "2026",
                ),
            )
            self.db.execute(
                "INSERT INTO catalog_item_projection VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    "en",
                    "music",
                    "artist",
                    "release",
                    json.dumps({"title": title}),
                    title.casefold(),
                    0,
                    "2024",
                    0,
                    "2026",
                    1,
                ),
            )
            self.db.execute(
                "INSERT INTO catalog_music_album_page VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    "en",
                    "music",
                    "artist",
                    title.casefold(),
                    "2024",
                    added,
                    added,
                    "2026",
                ),
            )
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "track-b",
                "music",
                "release-b",
                "track",
                "Beta/01.mp3",
                None,
                None,
                None,
                1,
                1,
                "2026",
                "2026",
            ),
        )
        self.db.execute(
            "INSERT INTO media_files VALUES('media-b','track-b','Beta/01.mp3','media',20)"
        )

    def tearDown(self):
        self.db.close()

    def test_page_selection_hydrates_only_selected_releases_and_batches_child_ids(self):
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        catalog._seed_hydration_rows = Mock()
        catalog._music_album_value = Mock(
            side_effect=lambda _user, row, _language, dates, children: {
                "id": row[0],
                "name": row[4],
                "childIds": children,
                "addedAt": dates[row[0]]["addedAt"],
            }
        )

        result = catalog._music_album_page(
            "user",
            "en",
            {"music"},
            page=2,
            page_size=1,
            sort_by="title",
            sort_order="ascending",
        )

        self.assertEqual(result["total"], 3)
        self.assertEqual([item["id"] for item in result["items"]], ["release-b"])
        self.assertEqual(result["items"][0]["childIds"], ["track-b"])
        catalog._seed_hydration_rows.assert_called_once()
        self.assertEqual(
            [row[0] for row in catalog._seed_hydration_rows.call_args.args[1]],
            ["release-b"],
        )
        catalog._music_album_value.assert_called_once()

    def test_page_sort_keys_match_release_and_added_order(self):
        catalog = Catalog.__new__(Catalog)
        catalog.db = self.db
        catalog._seed_hydration_rows = Mock()
        catalog._music_album_value = Mock(
            side_effect=lambda _user, row, _language, _dates, _children: {
                "id": row[0],
                "name": row[4],
            }
        )

        release_result = catalog._music_album_page(
            "user",
            "en",
            {"music"},
            page=1,
            page_size=2,
            sort_by="release",
            sort_order="descending",
        )
        added_result = catalog._music_album_page(
            "user",
            "en",
            {"music"},
            page=1,
            page_size=2,
            sort_by="added",
            sort_order="descending",
        )

        self.assertEqual(
            [item["id"] for item in release_result["items"]],
            ["release-c", "release-b"],
        )
        self.assertEqual(
            [item["id"] for item in added_result["items"]],
            ["release-c", "release-b"],
        )


if __name__ == "__main__":
    unittest.main()
