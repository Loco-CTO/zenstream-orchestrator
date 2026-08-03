import json
import threading
import tempfile
import unittest
from pathlib import Path

from app.database import DatabaseHandler
from app.metadata_services import (
    MetadataAssetExecutor,
    MetadataImageIngestService,
    MetadataIngestService,
    MetadataReadService,
)


class _Settings:
    def __init__(self, locales):
        self._locales = locales

    def get(self):
        return list(self._locales)


class _Fetcher:
    def __init__(self):
        self.calls = []
        self.cache = object()

    def fetch(self, provider, entity_type, provider_id, locale, force=False):
        self.calls.append((provider, entity_type, provider_id, locale, force))
        return {
            "title": locale,
            "images": [
                {
                    "type": "Primary",
                    "url": f"https://images.example/{locale}.jpg",
                    "language": locale,
                }
            ],
        }


class _ImageCache:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def put_image(self, *values):
        self.rows.append(values)


class MetadataServicesTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute("CREATE TABLE metadata_cache(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,payload TEXT,fetched_at TEXT,expires_at TEXT)")

    def tearDown(self):
        self.db.close()

    def test_asset_executor_deduplicates_pending_work(self):
        executor = MetadataAssetExecutor(max_workers=1)
        calls = []
        started = threading.Event()
        release = threading.Event()

        def first_work():
            calls.append(1)
            started.set()
            release.wait(5)

        try:
            executor.submit(("tmdb", "movie", "10", "en", "digest"), first_work)
            self.assertTrue(started.wait(5))
            executor.submit(("tmdb", "movie", "10", "en", "digest"), lambda: calls.append(2))
            release.set()
            executor.drain(5)
        finally:
            executor.shutdown()
        self.assertEqual(calls, [1])

    def test_unlimited_asset_executor_runs_tasks_concurrently(self):
        executor = MetadataAssetExecutor(max_workers=0)
        started = threading.Barrier(3)
        release = threading.Event()
        calls = []

        def work(value):
            calls.append(value)
            started.wait(5)
            release.wait(5)

        try:
            executor.submit(("asset", 1), lambda: work(1))
            executor.submit(("asset", 2), lambda: work(2))
            started.wait(5)
            release.set()
            executor.drain(5)
        finally:
            executor.shutdown()
        self.assertEqual(sorted(calls), [1, 2])

    def _cache(self, locale, payload):
        payload = {"_imageLanguageSchema": 3, **payload}
        self.db.execute(
            "INSERT INTO metadata_cache VALUES(?,?,?,?,?,?,?)",
            ("tmdb", "movie", "10", locale, json.dumps(payload), "now", "later"),
        )

    def test_read_fallback_is_field_level_and_does_not_use_arbitrary_locale(self):
        self._cache("ja", {
            "title": "Japanese",
            "originalLanguage": "ja",
            "images": [{"type": "Primary", "url": "neutral.jpg", "language": None, "provider": "tmdb"}],
        })
        self._cache("en", {"overview": "English overview", "originalLanguage": "ja"})
        self._cache("de", {"title": "German only"})
        service = MetadataReadService(self.db)
        value = service.resolve_raw("movie", [{"provider": "tmdb", "id": "10"}], "ja")
        self.assertEqual(value["title"], "Japanese")
        self.assertEqual(value["overview"], "English overview")
        self.assertEqual(value["images"][0]["url"], "neutral.jpg")

    def test_english_is_optional_and_original_is_only_used_when_cached(self):
        self._cache("ja", {"title": "Japanese", "originalLanguage": "ja"})
        service = MetadataReadService(self.db)
        value = service.resolve_raw("movie", [{"provider": "tmdb", "id": "10"}], "fr")
        self.assertEqual(value["title"], "Japanese")

    def test_public_artwork_exposes_the_matching_cached_blurhash(self):
        self.db.execute(
            "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,blur_hash TEXT,fetched_at TEXT)"
        )
        self._cache("en", {"images": [{"type": "Primary", "url": "poster.jpg", "language": "en", "provider": "tmdb"}]})
        self.db.execute(
            "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?)",
            ("tmdb", "movie", "10", "en", "Primary", "poster.jpg", "LEHV6nWB2yk8pyo0adR*.7kCMdnj", "now"),
        )

        value = MetadataReadService(self.db).resolve_public(
            "movie", "movie", [{"provider": "tmdb", "id": "10"}], "en"
        )

        self.assertEqual(value["metadata"]["images"]["Primary"]["blurHash"], "LEHV6nWB2yk8pyo0adR*.7kCMdnj")

    def test_original_language_provider_codes_match_canonical_cache_locales(self):
        self._cache("en", {"title": "English", "originalLanguage": "eng"})
        service = MetadataReadService(self.db)
        value = service.resolve_raw("movie", [{"provider": "tmdb", "id": "10"}], "fr")
        self.assertEqual(value["title"], "English")

    def test_ingest_fetches_only_configured_locales(self):
        fetcher = _Fetcher()
        ingest = MetadataIngestService(fetcher, _Settings(["ja", "de"]))
        ingest.ingest("tmdb", "movie", "10")
        self.assertEqual([call[3] for call in fetcher.calls], ["ja", "de"])
        self.assertNotIn("en", [call[3] for call in fetcher.calls])
        self.assertNotIn("original", [call[3] for call in fetcher.calls])

    def test_ingest_locale_rejects_unconfigured_language(self):
        fetcher = _Fetcher()
        ingest = MetadataIngestService(fetcher, _Settings(["ja"]))
        with self.assertRaises(ValueError):
            ingest.ingest_locale("tmdb", "movie", "10", "en")
        self.assertEqual(fetcher.calls, [])

    def test_ingest_downloads_all_configured_locale_images(self):
        cache = _ImageCache(self.db)
        with tempfile.TemporaryDirectory() as directory:
            image_ingest = MetadataImageIngestService(
                cache,
                directory,
                downloader=lambda url: b"image-data",
                encoder=lambda content, target, suffix: target.write_bytes(b"webp"),
                hasher=lambda target: "hash",
            )
            ingest = MetadataIngestService(
                _Fetcher(), _Settings(["ja", "de"]), image_ingest=image_ingest,
                background_assets=False,
            )
            ingest.ingest("tmdb", "movie", "10")

            self.assertEqual(len(cache.rows), 2)
            self.assertTrue(all(value[-1] and Path(value[-1]).is_file() for value in cache.rows))
            self.assertTrue(all(str(value[-1]).endswith(".webp") for value in cache.rows))

    def test_ingest_document_materializes_aggregated_series(self):
        cache = _ImageCache(self.db)
        with tempfile.TemporaryDirectory() as directory:
            image_ingest = MetadataImageIngestService(
                cache,
                directory,
                downloader=lambda url: b"series-image",
                encoder=lambda content, target, suffix: target.write_bytes(b"webp"),
                hasher=lambda target: "hash",
            )
            ingest = MetadataIngestService(
                _Fetcher(), _Settings(["en"]), image_ingest=image_ingest,
                background_assets=False,
            )
            ingest.ingest_document(
                "tvdb",
                "series",
                "series-1",
                "en",
                {
                    "images": [
                        {
                            "type": "Backdrop",
                            "url": "https://images.example/series.jpg",
                            "language": None,
                        }
                    ]
                },
            )

            self.assertEqual(len(cache.rows), 1)
            self.assertEqual(cache.rows[0][0:4], ("tvdb", "series", "series-1", None))

    def test_failed_webp_encoding_does_not_record_a_ready_image(self):
        cache = _ImageCache(self.db)
        with tempfile.TemporaryDirectory() as directory:
            image_ingest = MetadataImageIngestService(
                cache,
                directory,
                downloader=lambda url: b"image-data",
                encoder=lambda content, target, suffix: (_ for _ in ()).throw(
                    RuntimeError("encoder failed")
                ),
            )
            result = image_ingest.ingest(
                "tmdb",
                "movie",
                "10",
                "en",
                {
                    "images": [
                        {
                            "type": "Primary",
                            "url": "https://images.example/poster.jpg",
                            "language": "en",
                        }
                    ]
                },
            )

        self.assertEqual(result, {"ready": 0, "failed": 1, "skipped": 0})
        self.assertEqual(cache.rows, [])

    def test_ingest_persists_the_cached_artwork_blurhash(self):
        cache = _ImageCache(self.db)
        with tempfile.TemporaryDirectory() as directory:
            image_ingest = MetadataImageIngestService(
                cache,
                directory,
                downloader=lambda url: b"image-data",
                encoder=lambda content, target, suffix: target.write_bytes(b"webp"),
                hasher=lambda target: "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
            )
            image_ingest.ingest(
                "tmdb",
                "movie",
                "10",
                "en",
                {"images": [{"type": "Primary", "url": "https://images.example/poster.jpg"}]},
            )

        self.assertEqual(cache.rows[0][-2], "LEHV6nWB2yk8pyo0adR*.7kCMdnj")


if __name__ == "__main__":
    unittest.main()
