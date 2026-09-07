import json
import unittest

from app.database import DatabaseHandler
from app.library import LibraryScanner, LibraryStore


class _SuccessfulIngest:
    def locales(self):
        return ["en"]

    def provider_locales(self, _provider, _entity_type):
        return [""]

    def ingest_locales(self, _provider, _entity_type, provider_id, _locales, **_kwargs):
        return {"": {"title": f"Provider {provider_id}"}}


class MusicArtistCreditsTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, parent_id TEXT, entity_type TEXT NOT NULL, relative_path TEXT, season_number INTEGER, episode_number INTEGER, episode_end_number INTEGER, disc_number INTEGER, track_number INTEGER, created_at TEXT, updated_at TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, UNIQUE(library_id, entity_type, relative_path))",
            "CREATE TABLE entity_provider_ids(entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER, PRIMARY KEY(entity_id, provider, identifier_type))",
            "CREATE TABLE music_artist_credits(track_id TEXT NOT NULL, artist_id TEXT NOT NULL, credit_order INTEGER NOT NULL, credited_name TEXT NOT NULL, PRIMARY KEY(track_id, artist_id))",
            "CREATE TABLE metadata_cache(provider TEXT, entity_type TEXT, provider_id TEXT, locale TEXT, payload TEXT, fetched_at TEXT, expires_at TEXT)",
        ):
            self.db.execute(statement)
        store = LibraryStore.__new__(LibraryStore)
        store.db = self.db
        self.scanner = LibraryScanner(store)
        self.scanner._publish_root = lambda _root_id: None
        self.scanner._flush_publications = lambda: None
        self.scanner._music_artist_entities = {}

    def tearDown(self):
        self.db.close()

    def _entities(self):
        return self.db.execute(
            "SELECT id,entity_type,parent_id,relative_path FROM library_entities ORDER BY entity_type,relative_path"
        )

    def _seed_track(self):
        album_artist = self.scanner._entity("library-1", None, "artist", "Album Artist")
        release = self.scanner._entity("library-1", album_artist, "release", "Album")
        track = self.scanner._entity(
            "library-1",
            release,
            "track",
            "Album/01 - Track.mp3",
            disc_number=1,
            track_number=1,
        )
        self.scanner._music_local_metadata[album_artist] = {
            "title": "Album Artist",
            "albumArtist": "Album Artist",
        }
        self.scanner._music_local_metadata[release] = {
            "albumArtist": "Album Artist",
            "artists": [{"name": "Album Artist"}],
        }
        return album_artist, release, track

    def _materialize(self, album_artist, release, track, *, provider_id=None):
        resolved = [{"name": "Guest Artist"}]
        if provider_id:
            resolved = [{"id": provider_id, "name": "Guest Artist"}]
        self.scanner._materialize_music_artist_credits(
            "library-1",
            album_artist,
            release,
            [
                {
                    "entity_id": track,
                    "local": {
                        "artists": [
                            {"name": "Album Artist"},
                            {"name": "Guest Artist"},
                        ],
                        "contributingArtists": [
                            {"name": "Guest Artist"},
                            {"name": "Album Artist"},
                        ],
                    },
                    "resolved_artists": resolved,
                }
            ],
            {
                "": {
                    "artists": [
                        {"id": "mb-album", "name": "Album Artist"},
                        {"id": "mb-guest", "name": "Guest Artist"},
                    ]
                }
            },
            _SuccessfulIngest(),
            "job-1",
            lambda: False,
        )

    def test_provider_credits_reuse_album_artist_and_dedupe(self):
        album_artist, release, track = self._seed_track()

        self._materialize(album_artist, release, track)

        artists = self.db.execute(
            "SELECT id,relative_path FROM library_entities WHERE entity_type='artist' ORDER BY relative_path"
        )
        self.assertEqual(
            artists,
            [
                (album_artist, "Album Artist"),
                (self.scanner._music_artist_entities["guest artist"], "Guest Artist"),
            ],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT artist_id,credit_order,credited_name FROM music_artist_credits WHERE track_id=? ORDER BY credit_order",
                (track,),
            ),
            [
                (album_artist, 0, "Album Artist"),
                (
                    self.scanner._music_artist_entities["guest artist"],
                    1,
                    "Guest Artist",
                ),
            ],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY provider",
                (album_artist,),
            ),
            [("local", album_artist), ("musicbrainz", "mb-album")],
        )

        self._materialize(album_artist, release, track)
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM library_entities WHERE entity_type='artist'"
            )[0][0],
            2,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM music_artist_credits WHERE track_id=?",
                (track,),
            )[0][0],
            2,
        )

    def test_provider_id_later_attaches_to_existing_local_artist(self):
        album_artist, release, track = self._seed_track()
        self.scanner._music_artist_entities = {}
        self.scanner._materialize_music_artist_credits(
            "library-1",
            album_artist,
            release,
            [
                {
                    "entity_id": track,
                    "local": {
                        "artists": [
                            {"name": "Album Artist"},
                            {"name": "Guest Artist"},
                        ]
                    },
                }
            ],
            None,
            _SuccessfulIngest(),
            "job-1",
            lambda: False,
        )
        guest_id = self.scanner._music_artist_entities["guest artist"]

        self.scanner._music_artist_entities = {}
        self._materialize(album_artist, release, track, provider_id="mb-guest")

        self.assertEqual(
            self.db.execute(
                "SELECT id FROM library_entities WHERE relative_path='Guest Artist'"
            )[0][0],
            guest_id,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider='musicbrainz'",
                (guest_id,),
            ),
            [("mb-guest",)],
        )

    def test_orphan_credited_artist_is_removed_after_final_credit(self):
        album_artist, release, track = self._seed_track()
        self._materialize(album_artist, release, track)
        guest_id = self.scanner._music_artist_entities["guest artist"]
        self.db.execute(
            "DELETE FROM music_artist_credits WHERE track_id=? AND artist_id=?",
            (track, guest_id),
        )

        self.scanner._remove_orphan_music_artists("library-1")

        self.assertNotIn((guest_id, "artist", None, "Guest Artist"), self._entities())
        self.assertIn((album_artist, "artist", None, "Album Artist"), self._entities())

    def test_repair_materializes_release_track_credits_without_provider_requests(self):
        album_artist, release, track = self._seed_track()
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT, locale TEXT, payload TEXT)"
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            (track, "musicbrainz", "recording", "mb-recording", 1),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                release,
                "en",
                json.dumps(
                    {
                        "albumArtist": "Album Artist",
                        "artists": [{"id": "mb-album", "name": "Album Artist"}],
                        "tracks": [
                            {
                                "id": "mb-recording",
                                "position": 1,
                                "disc": 1,
                                "artists": [
                                    {"id": "mb-album", "name": "Album Artist"},
                                    {"id": "mb-guest", "name": "Guest Artist"},
                                ],
                                "contributingArtists": [
                                    {"id": "mb-guest", "name": "Guest Artist"},
                                    {"id": "mb-album", "name": "Album Artist"},
                                ],
                            }
                        ],
                    }
                ),
            ),
        )

        repaired = self.scanner.repair_music_artist_credits(
            _SuccessfulIngest(), "job-1", lambda: False
        )

        self.assertEqual(repaired, 1)
        guest_rows = self.db.execute(
            "SELECT id FROM library_entities WHERE entity_type='artist' AND relative_path=?",
            ("Guest Artist",),
        )
        self.assertEqual(len(guest_rows), 1)
        guest_id = guest_rows[0][0]
        self.assertEqual(
            self.db.execute(
                "SELECT provider_id FROM entity_provider_ids WHERE entity_id=? AND provider='musicbrainz'",
                (guest_id,),
            ),
            [("mb-guest",)],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT artist_id,credit_order,credited_name FROM music_artist_credits WHERE track_id=? ORDER BY credit_order",
                (track,),
            ),
            [
                (album_artist, 0, "Album Artist"),
                (guest_id, 1, "Guest Artist"),
            ],
        )

    def test_credit_normalization_removes_joined_synthetic_artist(self):
        document = {
            "artists": [
                {"name": "Aiobahn feat. ヰ世界情緒"},
                {
                    "id": "mb-aiobahn",
                    "name": "Aiobahn",
                    "joinPhrase": " feat. ",
                },
                {"id": "mb-uisekai", "name": "ヰ世界情緒", "joinPhrase": ""},
            ],
            "contributingArtists": [
                {"id": "mb-aiobahn", "name": "Aiobahn", "joinPhrase": " feat. "},
                {"id": "mb-uisekai", "name": "ヰ世界情緒", "joinPhrase": ""},
            ],
        }

        self.assertEqual(
            self.scanner._music_document_credits(document),
            [
                {"name": "Aiobahn", "id": "mb-aiobahn", "joinPhrase": " feat. "},
                {"name": "ヰ世界情緒", "id": "mb-uisekai", "joinPhrase": ""},
            ],
        )

    def test_credit_normalization_keeps_atomic_credits_with_same_name_and_ids(self):
        document = {
            "artists": [
                {"id": "mb-one", "name": "Shared Name", "joinPhrase": " & "},
                {"id": "mb-two", "name": "Shared Name", "joinPhrase": ""},
            ]
        }

        self.assertEqual(
            self.scanner._music_document_credits(document),
            [
                {"id": "mb-one", "name": "Shared Name", "joinPhrase": " & "},
                {"id": "mb-two", "name": "Shared Name", "joinPhrase": ""},
            ],
        )

    def test_repair_reparents_release_to_primary_and_removes_joined_parent(self):
        old_artist = self.scanner._entity("library-1", None, "artist", "ヰ世界情緒")
        primary_artist = self.scanner._entity("library-1", None, "artist", "Aiobahn")
        release = self.scanner._entity(
            "library-1", old_artist, "release", "Aiobahn feat. ヰ世界情緒/new world"
        )
        track = self.scanner._entity(
            "library-1",
            release,
            "track",
            "Aiobahn feat. ヰ世界情緒/new world/01. new world.flac",
            disc_number=1,
            track_number=1,
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            (primary_artist, "musicbrainz", "artist", "mb-aiobahn", 1),
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES(?,?,?,?,?)",
            (track, "musicbrainz", "recording", "mb-recording", 1),
        )
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT, locale TEXT, payload TEXT)"
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                release,
                "en",
                json.dumps(
                    {
                        "albumArtist": "Aiobahn",
                        "artists": [
                            {"id": "mb-aiobahn", "name": "Aiobahn"},
                            {
                                "id": "mb-uisekai",
                                "name": "ヰ世界情緒",
                                "joinPhrase": " feat. ",
                            },
                        ],
                        "contributingArtists": [
                            {"id": "mb-aiobahn", "name": "Aiobahn"},
                            {
                                "id": "mb-uisekai",
                                "name": "ヰ世界情緒",
                                "joinPhrase": " feat. ",
                            },
                        ],
                        "tracks": [
                            {
                                "id": "mb-recording",
                                "position": 1,
                                "disc": 1,
                                "artists": [
                                    {"id": "mb-aiobahn", "name": "Aiobahn"},
                                    {
                                        "id": "mb-uisekai",
                                        "name": "ヰ世界情緒",
                                        "joinPhrase": " feat. ",
                                    },
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                track,
                "en",
                json.dumps(
                    {
                        "artists": [
                            {"name": "Aiobahn feat. ヰ世界情緒"}
                        ],
                        "contributingArtists": [
                            {"name": "Aiobahn feat. ヰ世界情緒"}
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )

        repaired = self.scanner.repair_music_artist_credits(
            _SuccessfulIngest(), "job-1", lambda: False
        )

        self.assertEqual(repaired, 1)
        self.assertEqual(
            self.db.execute(
                "SELECT parent_id FROM library_entities WHERE id=?", (release,)
            ),
            [(primary_artist,)],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT artist_id,credit_order,credited_name FROM music_artist_credits WHERE track_id=? ORDER BY credit_order",
                (track,),
            ),
            [
                (primary_artist, 0, "Aiobahn"),
                (
                    self.scanner._music_artist_entities["ヰ世界情緒"],
                    1,
                    "ヰ世界情緒",
                ),
            ],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT id FROM library_entities WHERE entity_type='artist' AND relative_path=?",
                ("Aiobahn feat. ヰ世界情緒",),
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
