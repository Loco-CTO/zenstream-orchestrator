from app.offline_selections import OfflineSelectionRequest, OfflineSelections
from fastapi import HTTPException
from test.playlists_test import PlaylistServiceTest


class OfflineSelectionTest(PlaylistServiceTest):
    def setUp(self):
        super().setUp()
        for sql in (
            "CREATE TABLE offline_selections(id TEXT PRIMARY KEY,user_id TEXT,root_id TEXT,root_type TEXT,language TEXT,state TEXT,created_at TEXT,error_code TEXT)",
            "CREATE TABLE offline_selection_frontier(id INTEGER PRIMARY KEY AUTOINCREMENT,selection_id TEXT,entity_id TEXT,kind TEXT,page_offset INTEGER DEFAULT 0,done INTEGER DEFAULT 0,UNIQUE(selection_id,entity_id))",
            "CREATE TABLE offline_selection_items(selection_id TEXT,entity_id TEXT,payload TEXT,ordinal INTEGER,PRIMARY KEY(selection_id,entity_id))",
            "CREATE TABLE offline_selection_metadata(selection_id TEXT,entity_id TEXT,payload TEXT,PRIMARY KEY(selection_id,entity_id))",
        ):
            self.db.execute(sql)
        self.selections = OfflineSelections(self.catalog)

    def prepare(self, root, kind="catalog"):
        result = self.selections.create(
            "owner", OfflineSelectionRequest(itemId=root, kind=kind)
        )
        for _ in range(100):
            self.selections.process_next()
            result = self.selections.get("owner", result["id"])
            if result["state"] != "enumerating":
                return result
        self.fail("Selection did not finish")

    def test_album_is_an_ordered_frozen_snapshot(self):
        result = self.prepare("release")
        self.assertEqual(
            [item["id"] for item in result["items"]], ["track-b", "track-a"]
        )
        self.assertTrue(
            {"artist", "release"}.issubset({item["id"] for item in result["metadata"]})
        )
        self._entity("track-c", "track", "release", track_number=3)
        self.db.execute("INSERT INTO media_files VALUES('track-c','media')")
        self.assertEqual(self.selections.get("owner", result["id"])["itemCount"], 2)

    def test_series_selects_only_playable_episodes(self):
        self.db.execute("INSERT INTO media_files VALUES('episode-1','media')")
        result = self.prepare("series")
        self.assertEqual([item["id"] for item in result["items"]], ["episode-1"])

    def test_playlist_expansion_preserves_its_order(self):
        playlist = self.service.create_playlist(
            "owner", "en", name="Offline", entity_id="release"
        )
        result = self.prepare(playlist["id"], "playlist")
        self.assertEqual(
            [item["id"] for item in result["items"]], ["track-b", "track-a"]
        )

    def test_cancellation_is_terminal(self):
        result = self.selections.create(
            "owner", OfflineSelectionRequest(itemId="release")
        )
        self.selections.cancel("owner", result["id"])
        self.assertFalse(self.selections.process_next())
        self.assertEqual(
            self.selections.get("owner", result["id"])["state"], "cancelled"
        )

    def test_selection_cannot_be_read_by_another_account(self):
        result = self.prepare("release")
        with self.assertRaises(HTTPException) as error:
            self.selections.get("viewer", result["id"])
        self.assertEqual(error.exception.status_code, 404)

    def test_large_selection_is_paged_and_restarts_mid_enumeration(self):
        for index in range(205):
            item = f"extra-{index:04}"
            self._entity(item, "track", "release", track_number=index + 3)
            self.db.execute("INSERT INTO media_files VALUES(?,'media')", (item,))
        started = self.selections.create(
            "owner", OfflineSelectionRequest(itemId="release")
        )
        self.selections.process_next()
        self.selections = OfflineSelections(self.catalog)
        for _ in range(20):
            self.selections.process_next()
        page = self.selections.get("owner", started["id"])
        self.assertEqual(page["state"], "ready")
        self.assertEqual(page["itemCount"], 207)
        self.assertEqual(len(page["items"]), 100)
        self.assertEqual(page["nextOffset"], 100)
        last = self.selections.get("owner", started["id"], 200)
        self.assertEqual(len(last["items"]), 7)
        self.assertIsNone(last["nextOffset"])
