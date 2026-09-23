import unittest

from app.catalog import Catalog
from app.database import DatabaseHandler
from app.playlists import PlaylistService
from fastapi import HTTPException


class PlaylistServiceTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE users(id TEXT PRIMARY KEY)",
            "CREATE TABLE libraries(id TEXT PRIMARY KEY)",
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT,PRIMARY KEY(user_id,library_id))",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER,episode_end_number INTEGER,disc_number INTEGER,track_number INTEGER,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE media_files(entity_id TEXT,role TEXT)",
            "CREATE TABLE user_follow_targets(user_id TEXT,entity_id TEXT,created_at TEXT)",
            "CREATE TABLE user_item_state(user_id TEXT,entity_id TEXT,position_seconds REAL,duration_seconds REAL,played INTEGER,last_played_at TEXT)",
            "CREATE TABLE user_playlists(id TEXT PRIMARY KEY,user_id TEXT,name TEXT,description TEXT,is_private INTEGER,share_token TEXT,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE user_playlist_items(id TEXT PRIMARY KEY,playlist_id TEXT,entity_id TEXT,position INTEGER,added_at TEXT,UNIQUE(playlist_id,entity_id))",
        ):
            self.db.execute(statement)
        self.db.execute("INSERT INTO users VALUES('owner')")
        self.db.execute("INSERT INTO users VALUES('viewer')")
        self.db.execute("INSERT INTO libraries VALUES('music')")
        self.db.execute("INSERT INTO libraries VALUES('hidden')")
        self.db.execute("INSERT INTO user_library_access VALUES('owner','music')")
        self.db.execute("INSERT INTO user_library_access VALUES('viewer','music')")
        self._entity("artist", "artist", None)
        self._entity("release", "release", "artist")
        self._entity("track-a", "track", "release", track_number=2)
        self._entity("track-b", "track", "release", track_number=1)
        self._entity("movie", "movie", None)
        self._entity("series", "series", None)
        self._entity("episode-1", "episode", "series", season_number=1, episode_number=1)
        self._entity("episode-2", "episode", "series", season_number=1, episode_number=2)
        self._entity("season", "season", "series")
        self._entity("empty-release", "release", "artist")
        for item_id in ("track-a", "track-b"):
            self.db.execute("INSERT INTO media_files VALUES(?, 'media')", (item_id,))

        self.catalog = Catalog.__new__(Catalog)
        self.catalog.db = self.db

        def items_by_ids(user_id, entity_ids, _language):
            allowed = self.catalog.allowed_libraries(user_id)
            result = []
            for item_id in entity_ids:
                rows = self.db.execute(
                    "SELECT library_id FROM library_entities WHERE id=?", (item_id,)
                )
                if rows and rows[0][0] in allowed:
                    type_rows = self.db.execute(
                        "SELECT entity_type FROM library_entities WHERE id=?",
                        (item_id,),
                    )
                    entity_type = str(type_rows[0][0]) if type_rows else "track"
                    item = {"id": item_id, "type": entity_type, "name": item_id}
                    state_rows = self.db.execute(
                        "SELECT position_seconds,duration_seconds,played,last_played_at "
                        "FROM user_item_state WHERE user_id=? AND entity_id=?",
                        (user_id, item_id),
                    )
                    if state_rows:
                        state = state_rows[0]
                        item["userState"] = {
                            "positionSeconds": state[0],
                            "durationSeconds": state[1],
                            "played": state[2],
                            "lastPlayedAt": state[3],
                        }
                    result.append(item)
            return result

        self.catalog.items_by_ids = items_by_ids
        self.catalog.music_artist_tracks = lambda *_args: {
            "tracks": [{"id": "track-b"}, {"id": "track-a"}]
        }
        self.service = PlaylistService(self.catalog)

    def tearDown(self):
        self.db.close()

    def _entity(
        self,
        entity_id,
        entity_type,
        parent_id,
        *,
        library_id="music",
        season_number=None,
        episode_number=None,
        track_number=None,
    ):
        self.db.execute(
            "INSERT INTO library_entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entity_id,
                library_id,
                parent_id,
                entity_type,
                entity_id,
                season_number,
                episode_number,
                None,
                1 if entity_type == "track" else None,
                track_number,
                "2026-01-01",
                "2026-01-01",
            ),
        )

    def test_album_expands_to_ordered_track_snapshot_and_prevents_duplicates(self):
        playlist = self.service.create_playlist(
            "owner", "en", name="Road trip", entity_id="release"
        )

        updated = self.service.add_entities(
            "owner", playlist["id"], "en", ["track-a", "release"]
        )

        self.assertTrue(playlist["isPrivate"])
        self.assertEqual(
            [entry["item"]["id"] for entry in updated["items"]],
            ["track-b", "track-a"],
        )
        self.assertEqual(updated["itemCount"], 2)

    def test_artist_expands_to_tracks_in_catalog_order(self):
        playlist = self.service.create_playlist(
            "owner", "en", name="Artist mix", entity_id="artist"
        )

        self.assertEqual(
            [entry["item"]["id"] for entry in playlist["items"]],
            ["track-b", "track-a"],
        )

    def test_video_and_non_audio_entities_are_rejected(self):
        playlist = self.service.create_playlist("owner", "en", name="Audio only")

        for entity_id in ("movie", "series", "season", "empty-release"):
            with self.subTest(entity_id=entity_id):
                with self.assertRaises(HTTPException) as caught:
                    self.service.add_entities(
                        "owner", playlist["id"], "en", [entity_id]
                    )
                self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException) as caught:
            self.service.create_playlist(
                "owner", "en", name="Empty", entity_id="empty-release"
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_only_owner_can_change_playlist_entries_or_delete(self):
        playlist = self.service.create_playlist(
            "owner", "en", name="Owner only", entity_id="release"
        )
        entry_id = playlist["items"][0]["entryId"]

        mutations = (
            lambda: self.service.add_entities(
                "viewer", playlist["id"], "en", ["track-a"]
            ),
            lambda: self.service.remove_entry(
                "viewer", playlist["id"], entry_id, "en"
            ),
            lambda: self.service.reorder_entries(
                "viewer",
                playlist["id"],
                "en",
                [entry["entryId"] for entry in playlist["items"]],
            ),
            lambda: self.service.delete_playlist("viewer", playlist["id"]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with self.assertRaises(HTTPException) as caught:
                    mutate()
                self.assertEqual(caught.exception.status_code, 404)

        self.service.delete_playlist("owner", playlist["id"])
        with self.assertRaises(HTTPException):
            self.service.get_playlist("owner", playlist["id"], "en")

    def test_shared_playlist_is_permission_filtered_and_link_revoked_when_private(self):
        playlist = self.service.create_playlist(
            "owner",
            "en",
            name="Shared tracks",
            is_private=False,
            entity_id="release",
        )
        share_token = playlist["shareToken"]

        viewer_payload = self.service.get_shared_playlist("viewer", share_token, "en")

        self.assertFalse(viewer_payload["isOwner"])
        self.assertEqual(viewer_payload["itemCount"], 2)
        with self.assertRaises(HTTPException) as caught:
            self.service.update_playlist(
                "viewer", playlist["id"], "en", {"name": "Changed"}
            )
        self.assertEqual(caught.exception.status_code, 404)

        self.service.update_playlist(
            "owner", playlist["id"], "en", {"isPrivate": True}
        )
        with self.assertRaises(HTTPException) as caught:
            self.service.get_shared_playlist("viewer", share_token, "en")
        self.assertEqual(caught.exception.status_code, 404)

    def test_shared_playlist_hides_tracks_outside_viewers_library_access(self):
        self._entity(
            "hidden-track", "track", "release", library_id="hidden", track_number=3
        )
        self.db.execute("INSERT INTO media_files VALUES('hidden-track','media')")
        playlist = self.service.create_playlist(
            "owner", "en", name="Filtered", is_private=False, entity_id="release"
        )
        self.db.execute(
            "INSERT INTO user_playlist_items VALUES('hidden-entry',?,?,2,'2026-01-01')",
            (playlist["id"], "hidden-track"),
        )

        viewer_payload = self.service.get_shared_playlist(
            "viewer", playlist["shareToken"], "en"
        )

        self.assertEqual(viewer_payload["itemCount"], 2)
        self.assertNotIn(
            "hidden-track", [entry["item"]["id"] for entry in viewer_payload["items"]]
        )

    def test_reorder_requires_each_entry_exactly_once(self):
        playlist = self.service.create_playlist(
            "owner", "en", name="Ordered", entity_id="release"
        )
        entry_ids = [entry["entryId"] for entry in playlist["items"]]

        reordered = self.service.reorder_entries(
            "owner", playlist["id"], "en", list(reversed(entry_ids))
        )
        self.assertEqual(
            [entry["entryId"] for entry in reordered["items"]],
            list(reversed(entry_ids)),
        )
        with self.assertRaises(HTTPException):
            self.service.reorder_entries(
                "owner", playlist["id"], "en", [entry_ids[0], entry_ids[0]]
            )

    def test_watchlist_returns_followed_media_with_progress_and_next_episode(self):
        for entity_id, created_at in (
            ("artist", "2026-09-01T10:00:00Z"),
            ("series", "2026-09-02T10:00:00Z"),
            ("movie", "2026-09-03T10:00:00Z"),
        ):
            self.db.execute(
                "INSERT INTO user_follow_targets VALUES('owner',?,?)",
                (entity_id, created_at),
            )
        self.db.execute(
            "INSERT INTO user_item_state VALUES('owner','movie',20,100,0,'2026-09-03T09:00:00Z')"
        )
        self.db.execute(
            "INSERT INTO user_item_state VALUES('owner','episode-1',100,100,1,'2026-09-01T09:00:00Z')"
        )
        self.db.execute(
            "INSERT INTO user_item_state VALUES('owner','episode-2',0,100,0,NULL)"
        )

        result = self.service.watchlist("owner", "en")
        by_id = {item["id"]: item for item in result["items"]}

        self.assertEqual([item["id"] for item in result["items"]], ["movie", "series", "artist"])
        self.assertEqual(by_id["movie"]["watchlistStatus"], {"kind": "continue"})
        self.assertEqual(
            by_id["series"]["watchlistStatus"],
            {"kind": "upNext", "seasonNumber": 1, "episodeNumber": 2},
        )
        self.assertIsNone(by_id["artist"]["watchlistStatus"])


if __name__ == "__main__":
    unittest.main()
