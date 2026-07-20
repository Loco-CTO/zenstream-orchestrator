import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from api.zenstream import library_routes
from app.database import DatabaseHandler
from app.library import LibraryRuntime, LibraryStore, guess_media, provider_ids
from app.providers import MetadataService, ProviderError, TMDBClient, _select_match, choose_image


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
            {"type": "poster", "language": "fr", "url": "fr"},
            {"type": "poster", "language": "en", "url": "en"},
            {"type": "poster", "language": None, "url": "neutral"},
            {"type": "poster", "language": "ja", "url": "ja"},
        ]
        self.assertEqual(choose_image(images, "ja-JP", "poster")["url"], "ja")
        self.assertEqual(choose_image(images, "de-DE", "poster")["url"], "neutral")

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
                    imageType="poster",
                    locale="en",
                    Username="admin",
                    TOKEN="token",
                )
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["retry-after"], "10")
        metadata.assert_called_once_with(item, "en", False, False)
        hydration.assert_called_once_with(["entity-1"], "en")

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
        self.assertEqual({item["provider"] for item in value["ids"]}, {"tvdb", "imdb"})

    def test_primary_provider_is_required_but_secondary_provider_is_optional(self):
        service = MetadataService.__new__(MetadataService)
        service.fetch = lambda provider, entity_type, provider_id, locale, force=False: {"provider": provider, "providerId": provider_id, "title": "Example"}

        class PrimaryOnlyClient:
            def __init__(self, provider):
                self.provider = provider

            def search(self, entity_type, query, *args):
                if self.provider != "tvdb":
                    raise ProviderError("optional provider unavailable")
                return [{"providerId": "primary-1", "title": "Example", "year": "2020"}]

        service.client = lambda provider: PrimaryOnlyClient(provider)
        series = service.resolve_inventory_entity("series", "Example", "2020")
        self.assertEqual(series["providerIds"], [{"provider": "tvdb", "id": "primary-1"}])

        with self.assertRaises(ProviderError):
            service.resolve_inventory_entity("movie", "Example", "2020")


class LibraryJobControlTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute("CREATE TABLE library_jobs (id TEXT PRIMARY KEY, library_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)")
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
