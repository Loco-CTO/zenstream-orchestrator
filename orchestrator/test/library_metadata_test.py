import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from api.zenstream import library_routes
from app.database import DatabaseHandler
from app.library import LibraryRuntime, LibraryScanner, LibraryStore, guess_media, provider_ids
from app.providers import BANNER, PRIMARY, MetadataService, ProviderError, TMDBClient, TVDBClient, _select_match, choose_image, _tvdb_children, _tvdb_images


class LibraryMetadataTest(unittest.TestCase):
    def test_jellyfin_style_provider_ids_are_extracted(self):
        self.assertEqual(
            provider_ids("The Matrix (1999) [tmdbid-603] [tvdbid-Movie-123]"),
            [("tmdb", "movie", "603"), ("tvdb", "series", "Movie-123")],
        )

    def test_guessit_fallback_reads_season_and_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Show - S02E03 - Episode.mkv"
            path.touch()
            parsed = guess_media(path)
            self.assertEqual(int(parsed["season"]), 2)
            self.assertEqual(int(parsed["episode"]), 3)

    def test_image_fallback_order_is_requested_no_language_english_any(self):
        images = [
            {"type": PRIMARY, "language": "fr", "url": "fr"},
            {"type": PRIMARY, "language": "en", "url": "en"},
            {"type": PRIMARY, "language": None, "url": "neutral"},
            {"type": PRIMARY, "language": "ja", "url": "ja"},
        ]
        self.assertEqual(choose_image(images, "ja-JP", PRIMARY)["url"], "ja")
        self.assertEqual(choose_image(images, "de-DE", PRIMARY)["url"], "neutral")
        with self.assertRaises(ValueError):
            choose_image(images, "en", "Thumb")

    def test_preview_image_cache_miss_does_not_hydrate_provider_metadata(self):
        item = {
            "id": "entity-1",
            "libraryId": "library-1",
            "type": "movie",
            "providerIds": [{"provider": "tmdb", "id": "603"}],
        }
        with (
            patch.object(library_routes, "require_admin"),
            patch.object(library_routes, "_entity", return_value=item),
            patch.object(
                library_routes.store,
                "get",
                return_value={"id": "library-1", "directory": None},
            ),
            patch.object(library_routes.store.db, "execute", return_value=[]),
            patch.object(library_routes, "_metadata_for", return_value=None) as metadata,
            patch.object(library_routes.scheduler, "enqueue_metadata_hydration", return_value={"jobId": "job-1"}) as hydration,
        ):
            response = asyncio.run(
                library_routes.get_image(
                    "entity-1",
                    imageType=PRIMARY,
                    locale="en",
                    Username="admin",
                    TOKEN="token",
                )
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["retry-after"], "2")
        metadata.assert_called_once_with(item, "en", False, False)
        hydration.assert_called_once_with(["entity-1"], "en")

    def test_local_image_names_are_matched_to_canonical_artwork_types(self):
        self.assertTrue(library_routes._local_image_for_type("Series/poster.jpg", "Primary"))
        self.assertTrue(library_routes._local_image_for_type("Series/fanart.jpg", "Backdrop"))
        self.assertFalse(library_routes._local_image_for_type("Series/fanart.jpg", "Primary"))
        self.assertFalse(library_routes._local_image_for_type("Series/poster.jpg", "Backdrop"))

    def test_provider_match_rejects_ambiguous_candidates(self):
        with self.assertRaises(ProviderError):
            _select_match(
                [
                    {"providerId": "1", "title": "Example Show", "year": "2020"},
                    {"providerId": "2", "title": "Example Show", "year": "2020"},
                ],
                "Example Show",
                "2020",
            )

    def test_tmdb_normalization_keeps_common_fields_and_external_ids(self):
        value = TMDBClient({}, "api_key").normalize(
            "series",
            "10",
            {
                "name": "Example",
                "first_air_date": "2020-01-02",
                "overview": "Overview",
                "genres": [{"name": "Drama"}],
                "original_language": "ja",
                "external_ids": {"tvdb_id": 42, "imdb_id": "tt1"},
            },
        )
        self.assertEqual(value["year"], "2020")
        self.assertEqual(value["tags"], ["Drama"])
        self.assertEqual(value["images"], [])
        self.assertEqual({item["provider"] for item in value["ids"]}, {"tvdb", "imdb"})

    def test_provider_artwork_uses_canonical_categories(self):
        tmdb = TMDBClient({}, "api_key").normalize("episode", "10:1:2", {"name": "Episode", "images": {"stills": [{"file_path": "/still.jpg"}], "backdrops": [{"file_path": "/backdrop.jpg"}], "logos": [{"file_path": "/logo.png"}]}})
        self.assertEqual({value["type"] for value in tmdb["images"]}, {PRIMARY, "Backdrop", "Logo"})
        tvdb, extras = _tvdb_images({"artworks": [{"type": "banner", "image": "banner"}, {"type": "episode still", "image": "still"}, {"type": "unknown", "image": "other", "width": 100, "height": 100}]})
        self.assertEqual({value["type"] for value in tvdb}, {BANNER, PRIMARY})
        self.assertEqual(extras[0]["sourceType"], "unknown")

    def test_primary_provider_flags_follow_entity_type(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute("CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)")
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        for entity_id, entity_type in (("series-1", "series"), ("movie-1", "movie"), ("season-1", "season")):
            db.execute("INSERT INTO library_entities(id,entity_type) VALUES(?,?)", (entity_id, entity_type))
        scanner._ids("series-1", [("tmdb", "series", "tmdb-series"), ("tvdb", "series", "tvdb-series")])
        scanner._ids("movie-1", [("tvdb", "movie", "tvdb-movie"), ("tmdb", "movie", "tmdb-movie")])
        scanner._ids("season-1", [("tmdb", "season", "tmdb-season"), ("tvdb", "season", "tvdb-season")])
        self.assertEqual(db.execute("SELECT provider,is_primary FROM entity_provider_ids WHERE entity_id='series-1' ORDER BY provider"), [("tmdb", 0), ("tvdb", 1)])
        self.assertEqual(db.execute("SELECT provider,is_primary FROM entity_provider_ids WHERE entity_id='movie-1' ORDER BY provider"), [("tmdb", 1), ("tvdb", 0)])
        self.assertEqual(db.execute("SELECT provider,is_primary FROM entity_provider_ids WHERE entity_id='season-1' ORDER BY provider"), [("tmdb", 0), ("tvdb", 1)])
        db.close()

    def test_tvdb_season_details_attach_exact_episode_ids(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute("CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, parent_id TEXT, season_number INTEGER, episode_number INTEGER, relative_path TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)")
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        db.execute("INSERT INTO library_entities(id,entity_type,relative_path) VALUES('series-1','series','Example')")
        db.execute("INSERT INTO library_entities(id,entity_type,parent_id,season_number,relative_path) VALUES('season-1','season','series-1',1,'Season 1')")
        db.execute("INSERT INTO library_entities(id,entity_type,parent_id,season_number,episode_number,relative_path) VALUES('episode-1','episode','season-1',1,2,'Episode 2')")
        scanner._ids("season-1", [("tvdb", "season", "season-tvdb-1"), ("tmdb", "season", "series-tmdb:1")])

        class FakeService:
            def fetch(self, provider, entity_type, provider_id, locale, force=False):
                self.called = (provider, entity_type, provider_id, locale, force)
                return {"provider": "tvdb", "providerId": provider_id, "children": [{"type": "episode", "season": 1, "episode": 2, "id": "episode-tvdb-2"}], "ids": [], "images": []}

        scanner._derive_tvdb_episode_ids("series-1", FakeService())
        self.assertEqual(db.execute("SELECT provider,provider_id,is_primary FROM entity_provider_ids WHERE entity_id='episode-1'"), [("tvdb", "episode-tvdb-2", 1)])
        db.close()

    def test_tvdb_children_preserve_specials_season_zero(self):
        self.assertEqual(
            _tvdb_children({
                "seasons": [{"seasonNumber": 0, "number": 99, "id": 123}],
                "episodes": [{"seasonNumber": 0, "number": 1, "id": 456}],
            }),
            [
                {"type": "season", "season": 0, "id": "123"},
                {"type": "episode", "season": 0, "episode": 1, "id": "456"},
            ],
        )

    def test_provider_child_ids_attach_specials_season_zero(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute("CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, parent_id TEXT, season_number INTEGER, episode_number INTEGER, relative_path TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)")
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        db.execute("INSERT INTO library_entities(id,entity_type) VALUES('series-1','series')")
        db.execute("INSERT INTO library_entities(id,entity_type,parent_id,season_number,relative_path) VALUES('season-0','season','series-1',0,'Specials')")
        scanner._derive_provider_child_ids("series-1", {"provider": "tvdb", "children": [{"type": "season", "season": 0, "id": "season-tvdb-0"}]})
        self.assertEqual(db.execute("SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id='season-0'"), [("tvdb", "season-tvdb-0")])
        db.close()

    def test_tvdb_episode_resolution_failure_is_actionable(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute("CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, parent_id TEXT, season_number INTEGER, episode_number INTEGER, relative_path TEXT, match_status TEXT DEFAULT 'unresolved', match_confidence REAL, match_method TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)")
        store = LibraryStore.__new__(LibraryStore)
        store.db = db
        scanner = LibraryScanner(store)
        db.execute("INSERT INTO library_entities(id,entity_type,relative_path) VALUES('series-1','series','Example')")
        db.execute("INSERT INTO library_entities(id,entity_type,parent_id,season_number,relative_path) VALUES('season-1','season','series-1',1,'Season 1')")
        db.execute("INSERT INTO library_entities(id,entity_type,parent_id,season_number,episode_number,relative_path) VALUES('episode-1','episode','season-1',1,2,'Episode 2')")
        scanner._ids("season-1", [("tvdb", "season", "season-tvdb-1")])
        with self.assertRaisesRegex(ValueError, "TVDB episode ID could not be resolved.*S01E02"):
            scanner._derive_tvdb_episode_ids("series-1", type("FakeService", (), {"fetch": lambda *_args, **_kwargs: {"children": [], "ids": [], "images": []}})())
        db.close()

    def test_detail_payload_orders_and_labels_tvdb_as_series_primary(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute("CREATE TABLE library_entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL)")
        db.execute("CREATE TABLE entity_provider_ids (entity_id TEXT, provider TEXT, identifier_type TEXT, provider_id TEXT, is_primary INTEGER)")
        db.execute("INSERT INTO library_entities(id,entity_type) VALUES('series-1','series')")
        db.execute("INSERT INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES('series-1','tmdb','series','217512',1)")
        db.execute("INSERT INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES('series-1','tvdb','series','429055',1)")
        original = library_routes.store.db
        library_routes.store.db = db
        try:
            value = library_routes._entity_ids("series-1")
        finally:
            library_routes.store.db = original
            db.close()
        self.assertEqual(value[0], {"provider": "tvdb", "type": "series", "id": "429055", "primary": True, "role": "primary"})
        self.assertEqual(value[1]["role"], "secondary")
    def test_primary_provider_is_required_but_secondary_provider_is_optional(self):
        service = MetadataService.__new__(MetadataService)
        service.fetch = lambda provider, entity_type, provider_id, locale, force=False: {"provider": provider, "providerId": provider_id, "title": "Example", "ids": [{"provider": "tmdb", "id": "secondary-from-tvdb"}]}

        class PrimaryOnlyClient:
            def __init__(self, provider):
                self.provider = provider
                self.searches = []

            def search(self, entity_type, query, *args):
                self.searches.append((entity_type, query, args))
                if self.provider != "tvdb":
                    raise ProviderError("primary provider unavailable")
                return [{"providerId": "primary-1", "title": "Example", "year": "2020"}]

        clients = {}
        def client(provider):
            clients[provider] = PrimaryOnlyClient(provider)
            return clients[provider]
        service.client = client
        series = service.resolve_inventory_entity("series", "Example", "2020")
        self.assertEqual(series["providerIds"], [{"provider": "tvdb", "id": "primary-1"}, {"provider": "tmdb", "id": "secondary-from-tvdb"}])

        with self.assertRaises(ProviderError):
            service.resolve_inventory_entity("movie", "Example", "2020")

    def test_movie_secondary_ids_come_from_tmdb_external_ids_without_tvdb_search(self):
        service = MetadataService.__new__(MetadataService)

        class FakeClient:
            def __init__(self, provider):
                self.provider = provider

            def search(self, entity_type, query, *args):
                if self.provider != "tmdb":
                    raise AssertionError("secondary provider must not be searched")
                return [{"providerId": "movie-1", "title": "Example", "year": "2020"}]

        service.client = FakeClient
        service.fetch = lambda provider, entity_type, provider_id, locale, force=False: {
            "provider": "tmdb",
            "providerId": provider_id,
            "title": "Example",
            "ids": [
                {"provider": "tvdb", "id": "tvdb-from-tmdb"},
                {"provider": "imdb", "id": "tt-from-tmdb"},
            ],
        }

        result = service.resolve_inventory_entity("movie", "Example", "2020")

        self.assertEqual(
            result["providerIds"],
            [
                {"provider": "tmdb", "id": "movie-1"},
                {"provider": "tvdb", "id": "tvdb-from-tmdb"},
                {"provider": "imdb", "id": "tt-from-tmdb"},
            ],
        )


class LibraryJobControlTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute("CREATE TABLE library_jobs (id TEXT PRIMARY KEY, library_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, error_details TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)")
        store = LibraryStore.__new__(LibraryStore)
        store.db = self.db
        self.runtime = LibraryRuntime.__new__(LibraryRuntime)
        self.runtime.store = store
        self.runtime.condition = threading.Condition()
        self.runtime._active_lock = threading.RLock()
        self.runtime._cancel_events = {}

    def tearDown(self):
        self.db.close()

    def test_scan_and_reconcile_share_one_active_library_task(self):
        scan = self.runtime.enqueue("library-1", "scan")
        reconcile = self.runtime.enqueue("library-1", "reconcile")

        self.assertEqual(scan["id"], reconcile["id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM library_jobs")[0][0], 1)

    def test_queued_library_task_can_be_terminated(self):
        job = self.runtime.enqueue("library-1", "scan")

        terminated = self.runtime.terminate(job["id"])

        self.assertEqual(terminated["state"], "terminated")
        self.assertIsNotNone(terminated["finishedAt"])


if __name__ == "__main__":
    unittest.main()
