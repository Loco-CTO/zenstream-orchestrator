import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.database import DatabaseHandler
from app.models.metadata import MetadataCache
from app.metadata_services import (
    MetadataAssetExecutor,
    MetadataImageIngestService,
    MetadataIngestService,
    MetadataReadService,
    MetadataSearchProjection,
    metadata_fetch_activity,
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


class _BulkFetcher(_Fetcher):
    def __init__(self):
        super().__init__()
        self.bulk_calls = []

    def fetch_locales(
        self, provider, entity_type, provider_id, locales, force=False
    ):
        self.bulk_calls.append(
            (provider, entity_type, provider_id, tuple(locales), force)
        )
        return {
            locale: {"title": locale, "images": []} for locale in locales
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

    def test_neutral_image_writes_use_one_non_null_identity(self):
        self.db.execute(
            "CREATE TABLE metadata_images("
            "provider TEXT NOT NULL,entity_type TEXT NOT NULL,provider_id TEXT NOT NULL,"
            "locale TEXT NOT NULL DEFAULT '',image_type TEXT NOT NULL,image_url TEXT NOT NULL,"
            "blur_hash TEXT,local_path TEXT,fetched_at TEXT,expires_at TEXT,"
            "PRIMARY KEY(provider,entity_type,provider_id,locale,image_type,image_url))"
        )
        cache = MetadataCache.__new__(MetadataCache)
        cache.db = self.db
        record = (
            "tmdb",
            "movie",
            "10",
            None,
            "Primary",
            "https://images.example/poster.jpg",
            "blurhash",
            "/cache/poster.webp",
        )
        cache.put_images([record])
        cache.put_images([record])
        self.assertEqual(
            self.db.read_execute("SELECT locale,COUNT(*) FROM metadata_images"),
            [("", 1)],
        )

    def test_asset_executor_clamps_worker_limits(self):
        zero = MetadataAssetExecutor(max_workers=0)
        oversized = MetadataAssetExecutor(max_workers=99)
        try:
            self.assertEqual(zero.max_workers, 1)
            self.assertEqual(oversized.max_workers, 64)
        finally:
            zero.shutdown()
            oversized.shutdown()

    def test_asset_executor_does_not_wait_for_metadata_fetch_quiet_period(self):
        executor = MetadataAssetExecutor(max_workers=1)
        started = threading.Event()
        try:
            with metadata_fetch_activity():
                executor.submit(("tmdb", "movie", "10", "en", "digest"), started.set)
                self.assertTrue(started.wait(1))
        finally:
            executor.shutdown()

    def test_cached_document_reprojects_after_provider_identity_is_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            try:
                database.execute(
                    "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)"
                )
                database.execute(
                    "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,provider_id TEXT)"
                )
                database.execute(
                    "CREATE TABLE catalog_search(entity_id TEXT,library_id TEXT,locale TEXT,title TEXT)"
                )
                database.execute(
                    "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,entity_type TEXT,payload TEXT,title_sort TEXT,rating_sort REAL,release_sort TEXT,runtime_sort REAL,updated_at TEXT,generation INTEGER,PRIMARY KEY(entity_id,locale))"
                )
                database.execute(
                    "CREATE TABLE catalog_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,PRIMARY KEY(gram,entity_id,locale))"
                )
                database.execute(
                    "CREATE TABLE catalog_item_genres(entity_id TEXT,locale TEXT,genre_key TEXT,genre_name TEXT,PRIMARY KEY(entity_id,locale,genre_key))"
                )
                database.execute(
                    "INSERT INTO library_entities VALUES('movie','library',NULL,'movie')"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids VALUES('movie','tmdb','1')"
                )
                fetcher = _BulkFetcher()
                fetcher.cache = type("Cache", (), {"db": database})()
                ingest = MetadataIngestService(
                    fetcher,
                    _Settings(["en"]),
                    background_assets=False,
                )

                ingest.ingest_document(
                    "tmdb",
                    "movie",
                    "1",
                    "en",
                    {
                        "title": "Cached Movie",
                        "overview": "Cached overview",
                        "description": "Cached description",
                        "images": [
                            {
                                "type": "Primary",
                                "language": "en",
                                "url": "https://images.example/movie.jpg",
                            }
                        ],
                    },
                )

                projected = database.execute(
                    "SELECT payload FROM catalog_item_projection WHERE entity_id='movie' AND locale='en'"
                )
                value = json.loads(projected[0][0])
                self.assertEqual(value["title"], "Cached Movie")
                self.assertEqual(value["overview"], "Cached overview")
                self.assertEqual(value["description"], "Cached description")
                self.assertEqual(value["_catalogItemProjectionSchema"], 1)
                self.assertEqual(
                    value["images"]["Primary"]["url"],
                    "/api/catalog/items/movie/images/Primary?language=en",
                )
            finally:
                database.close()

    def test_concurrent_projection_preparation_merges_after_writer_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            try:
                database.execute(
                    "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)"
                )
                database.execute(
                    "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,provider_id TEXT)"
                )
                database.execute(
                    "CREATE TABLE catalog_search(entity_id TEXT,library_id TEXT,locale TEXT,title TEXT)"
                )
                database.execute(
                    "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,entity_type TEXT,payload TEXT,title_sort TEXT,rating_sort REAL,release_sort TEXT,runtime_sort REAL,updated_at TEXT,generation INTEGER,PRIMARY KEY(entity_id,locale))"
                )
                database.execute(
                    "CREATE TABLE catalog_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,PRIMARY KEY(gram,entity_id,locale))"
                )
                database.execute(
                    "CREATE TABLE catalog_item_genres(entity_id TEXT,locale TEXT,genre_key TEXT,genre_name TEXT,PRIMARY KEY(entity_id,locale,genre_key))"
                )
                database.execute(
                    "INSERT INTO library_entities VALUES('movie','library',NULL,'movie')"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids VALUES('movie','tmdb','1')"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids VALUES('movie','tvdb','2')"
                )
                barrier = threading.Barrier(2)
                calls = 0
                calls_lock = threading.Lock()
                from app.catalog_read_model import normalize_search_text

                def synchronized_normalize(value):
                    nonlocal calls
                    with calls_lock:
                        calls += 1
                        should_wait = calls <= 2
                    if should_wait:
                        barrier.wait(2)
                    return normalize_search_text(value)

                errors = []

                def project(provider, provider_id, payload):
                    try:
                        MetadataSearchProjection(database).project(
                            provider, "movie", provider_id, "en", payload
                        )
                    except Exception as error:
                        errors.append(error)

                with patch(
                    "app.catalog_read_model.normalize_search_text",
                    side_effect=synchronized_normalize,
                ):
                    workers = [
                        threading.Thread(
                            target=project,
                            args=("tmdb", "1", {"title": "Movie"}),
                        ),
                        threading.Thread(
                            target=project,
                            args=("tvdb", "2", {"genres": ["Drama"]}),
                        ),
                    ]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join(3)

                self.assertEqual(errors, [])
                self.assertFalse(any(worker.is_alive() for worker in workers))
                projected = json.loads(
                    database.execute(
                        "SELECT payload FROM catalog_item_projection WHERE entity_id='movie' AND locale='en'"
                    )[0][0]
                )
                self.assertEqual(projected["title"], "Movie")
                self.assertEqual(projected["genres"], ["Drama"])
            finally:
                database.close()

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

    def test_ingest_batches_all_configured_locales(self):
        fetcher = _BulkFetcher()
        ingest = MetadataIngestService(fetcher, _Settings(["en", "ja", "zh-TW"]))

        values = ingest.ingest("tmdb", "movie", "10")

        self.assertEqual(len(fetcher.bulk_calls), 1)
        self.assertEqual(fetcher.bulk_calls[0][3], ("en", "ja", "zh-TW"))
        self.assertEqual([value["title"] for value in values], ["en", "ja", "zh-TW"])

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

    def test_forced_ingest_redownloads_existing_artwork_and_portraits(self):
        fetcher = _BulkFetcher()
        image_ingest = MagicMock()
        credit_ingest = MagicMock()
        ingest = MetadataIngestService(
            fetcher,
            _Settings(["en"]),
            image_ingest=image_ingest,
            credit_ingest=credit_ingest,
            background_assets=False,
        )

        ingest.ingest_locales("tmdb", "movie", "10", ["en"], force=True)

        image_ingest.ingest.assert_called_once_with(
            "tmdb",
            "movie",
            "10",
            "en",
            {"title": "en", "images": []},
            force=True,
        )
        credit_ingest.ingest.assert_called_once_with(
            "tmdb",
            "movie",
            "10",
            "en",
            {"title": "en", "images": []},
            force_images=True,
        )

    def test_forced_image_ingest_replaces_an_existing_cached_file(self):
        cache = _ImageCache(self.db)
        downloads = []
        with tempfile.TemporaryDirectory() as directory:
            image_ingest = MetadataImageIngestService(
                cache,
                directory,
                downloader=lambda url: downloads.append(url) or b"image-data",
                encoder=lambda content, target, suffix: target.write_bytes(b"webp"),
                hasher=lambda target: "hash",
            )
            document = {
                "images": [
                    {
                        "type": "Primary",
                        "url": "https://images.example/poster.jpg",
                    }
                ]
            }
            image_ingest.ingest("tmdb", "movie", "10", "en", document)
            result = image_ingest.ingest(
                "tmdb", "movie", "10", "en", document, force=True
            )

        self.assertEqual(len(downloads), 2)
        self.assertEqual(result, {"ready": 1, "failed": 0, "skipped": 0})

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
