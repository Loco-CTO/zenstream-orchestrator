import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from app.database import DatabaseHandler
from app.metadata_services import (
    MetadataAssetExecutor,
    MetadataImageIngestService,
    MetadataIngestService,
    MetadataReadService,
    MetadataSearchProjection,
    _asset_version,
    metadata_fetch_activity,
)
from app.models.metadata import MetadataCache


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

    def fetch_locales(self, provider, entity_type, provider_id, locales, force=False):
        self.bulk_calls.append(
            (provider, entity_type, provider_id, tuple(locales), force)
        )
        return {locale: {"title": locale, "images": []} for locale in locales}


class _ImageCache:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def put_image(self, *values):
        self.rows.append(values)


class MetadataServicesTest(unittest.TestCase):
    def test_trailer_resolution_uses_regional_match_then_configured_english(self):
        payloads = {
            ("tmdb", "ja-JP"): {
                "trailers": [
                    {"url": "https://youtube.com/ja-jp", "language": "ja-JP"},
                    {"url": "https://youtube.com/ja-us", "language": "ja-US"},
                ]
            },
            ("tmdb", "en-US"): {
                "trailers": [{"url": "https://youtube.com/en", "language": "en-US"}]
            },
            ("tmdb", "fr-FR"): {
                "trailers": [{"url": "https://youtube.com/fr", "language": "fr-FR"}]
            },
        }
        with patch(
            "app.metadata_services.MetadataLanguageSettings",
            return_value=_Settings(["ja-JP", "en-US"]),
        ):
            selected = MetadataReadService._localized_trailers(
                payloads, ["tmdb"], "ja-JP", "fr-FR"
            )
        self.assertEqual(
            [value["url"] for value in selected],
            ["https://youtube.com/ja-jp"],
        )

    def test_trailer_resolution_allows_neutral_media_metadata(self):
        payloads = {
            ("tmdb", "ja"): {"trailers": [{"url": "https://youtube.com/neutral"}]},
            ("tmdb", "fr-FR"): {
                "trailers": [{"url": "https://youtube.com/fr", "language": "fr-FR"}]
            },
        }
        with patch(
            "app.metadata_services.MetadataLanguageSettings",
            return_value=_Settings(["ja-JP"]),
        ):
            selected = MetadataReadService._localized_trailers(
                payloads, ["tmdb"], "ja-JP", "ja-JP"
            )
        self.assertEqual(
            [value["url"] for value in selected], ["https://youtube.com/neutral"]
        )

    def test_asset_version_changes_when_same_url_bytes_change(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "image.webp"
            target.write_bytes(b"first")
            first = _asset_version(target, "https://images.example/poster")
            target.write_bytes(b"second")
            second = _asset_version(target, "https://images.example/poster")
            self.assertNotEqual(first, second)

    def test_secondary_provider_does_not_replace_primary_artwork_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler("sqlite", {}, str(Path(directory) / "db.sqlite"))
            primary_path = Path(directory) / "primary.webp"
            secondary_path = Path(directory) / "secondary.webp"
            refreshed_logo_path = Path(directory) / "secondary-logo-refreshed.webp"
            primary_path.write_bytes(b"primary")
            secondary_path.write_bytes(b"secondary")
            refreshed_logo_path.write_bytes(b"secondary-logo-refreshed")
            try:
                for statement in (
                    "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)",
                    "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,provider_id TEXT,is_primary INTEGER)",
                    "CREATE TABLE catalog_search(entity_id TEXT,library_id TEXT,locale TEXT,title TEXT)",
                    "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,entity_type TEXT,payload TEXT,title_sort TEXT,rating_sort REAL,release_sort TEXT,runtime_sort REAL,updated_at TEXT,generation INTEGER,PRIMARY KEY(entity_id,locale))",
                    "CREATE TABLE catalog_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,PRIMARY KEY(gram,entity_id,locale))",
                    "CREATE TABLE catalog_root_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,title_sort TEXT,PRIMARY KEY(gram,entity_id,locale))",
                    "CREATE TABLE catalog_item_genres(entity_id TEXT,locale TEXT,genre_key TEXT,genre_name TEXT,PRIMARY KEY(entity_id,locale,genre_key))",
                    "CREATE TABLE catalog_artwork_selection(entity_id TEXT,locale TEXT,image_type TEXT,provider TEXT,local_path TEXT,blur_hash TEXT,version TEXT,updated_at TEXT,PRIMARY KEY(entity_id,locale,image_type))",
                    "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,local_path TEXT,fetched_at TEXT,blur_hash TEXT)",
                    "CREATE TABLE media_files(entity_id TEXT,relative_path TEXT,role TEXT,quick_fingerprint TEXT,image_blur_hash TEXT)",
                ):
                    database.execute(statement)
                database.execute(
                    "INSERT INTO library_entities VALUES('movie','library',NULL,'movie')"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids VALUES('movie','tmdb','1',1)"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids VALUES('movie','tvdb','2',0)"
                )
                database.execute(
                    "INSERT INTO media_files VALUES('movie','poster.jpg','image','local-v1','local-blur')"
                )
                database.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "tmdb",
                        "movie",
                        "1",
                        "en",
                        "Primary",
                        "https://primary",
                        str(primary_path),
                        "1",
                        "hash-primary",
                    ),
                )
                database.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "tvdb",
                        "movie",
                        "2",
                        "en",
                        "Primary",
                        "https://secondary",
                        str(secondary_path),
                        "2",
                        "hash-secondary",
                    ),
                )
                database.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "tvdb",
                        "movie",
                        "2",
                        "en",
                        "Logo",
                        "https://secondary-logo",
                        str(secondary_path),
                        "2",
                        None,
                    ),
                )
                MetadataSearchProjection(database).project(
                    "tmdb",
                    "movie",
                    "1",
                    "en",
                    {
                        "title": "Movie",
                        "images": [
                            {
                                "type": "Primary",
                                "url": "https://primary",
                                "language": "en",
                            }
                        ],
                    },
                )
                MetadataSearchProjection(database).project(
                    "tvdb",
                    "movie",
                    "2",
                    "en",
                    {
                        "title": "Secondary",
                        "images": [
                            {
                                "type": "Primary",
                                "url": "https://secondary",
                                "language": "en",
                            },
                            {
                                "type": "Logo",
                                "url": "https://secondary-logo",
                                "language": "en",
                            },
                        ],
                    },
                )
                self.assertEqual(
                    database.read_execute(
                        "SELECT local_path,blur_hash FROM catalog_artwork_selection WHERE entity_id='movie' AND locale='en' AND image_type='Primary'"
                    ),
                    [(str(primary_path), "hash-primary")],
                )
                database.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "tvdb",
                        "movie",
                        "2",
                        "en",
                        "Logo",
                        "https://secondary-logo",
                        str(refreshed_logo_path),
                        "3",
                        None,
                    ),
                )
                MetadataSearchProjection(database).project(
                    "tvdb",
                    "movie",
                    "2",
                    "en",
                    {
                        "images": [
                            {
                                "type": "Logo",
                                "url": "https://secondary-logo",
                                "language": "en",
                            }
                        ]
                    },
                )
                self.assertEqual(
                    database.read_execute(
                        "SELECT local_path FROM catalog_artwork_selection WHERE entity_id='movie' AND locale='en' AND image_type='Logo'"
                    ),
                    [(str(refreshed_logo_path),)],
                )
                self.assertEqual(
                    database.read_execute(
                        "SELECT local_path FROM catalog_artwork_selection WHERE entity_id='movie' AND locale='en' AND image_type='Primary'"
                    ),
                    [(str(primary_path),)],
                )
                projected = json.loads(
                    database.read_execute(
                        "SELECT payload FROM catalog_item_projection WHERE entity_id='movie' AND locale='en'"
                    )[0][0]
                )
                self.assertTrue(
                    projected["images"]["Primary"]["url"].endswith("v=local-v1")
                )
                database.execute(
                    "UPDATE media_files SET quick_fingerprint='local-v2' WHERE entity_id='movie'"
                )
                MetadataSearchProjection(database).project(
                    "tmdb",
                    "movie",
                    "1",
                    "en",
                    {
                        "title": "Movie",
                        "images": [
                            {
                                "type": "Primary",
                                "url": "https://primary",
                                "language": "en",
                            }
                        ],
                    },
                )
                refreshed = json.loads(
                    database.read_execute(
                        "SELECT payload FROM catalog_item_projection WHERE entity_id='movie' AND locale='en'"
                    )[0][0]
                )
                self.assertTrue(
                    refreshed["images"]["Primary"]["url"].endswith("v=local-v2")
                )
                database.execute("DELETE FROM media_files WHERE entity_id='movie'")
                MetadataSearchProjection(database).project(
                    "tmdb",
                    "movie",
                    "1",
                    "en",
                    {
                        "title": "Movie",
                        "images": [
                            {
                                "type": "Primary",
                                "url": "https://primary",
                                "language": "en",
                            }
                        ],
                    },
                )
                without_local = json.loads(
                    database.read_execute(
                        "SELECT payload FROM catalog_item_projection WHERE entity_id='movie' AND locale='en'"
                    )[0][0]
                )
                self.assertNotIn(
                    "v=local-v2", without_local["images"]["Primary"]["url"]
                )
                self.assertEqual(
                    without_local["_catalogArtworkProviders"]["Primary"], "tmdb"
                )
            finally:
                database.close()

    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE metadata_cache(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,payload TEXT,fetched_at TEXT,expires_at TEXT)"
        )

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
            executor.submit(
                ("tmdb", "movie", "10", "en", "digest"), lambda: calls.append(2)
            )
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
                cached_path = Path(directory) / "movie.webp"
                cached_path.write_bytes(b"cached")
                database.execute(
                    "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)"
                )
                database.execute(
                    "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,provider_id TEXT,is_primary INTEGER NOT NULL DEFAULT 0)"
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
                    "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,blur_hash TEXT,local_path TEXT,fetched_at TEXT,expires_at TEXT,PRIMARY KEY(provider,entity_type,provider_id,locale,image_type,image_url))"
                )
                database.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "tmdb",
                        "movie",
                        "1",
                        "en",
                        "Primary",
                        "https://images.example/movie.jpg",
                        "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
                        str(cached_path),
                        "now",
                        "later",
                    ),
                )
                database.execute(
                    "INSERT INTO library_entities VALUES('movie','library',NULL,'movie')"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids(entity_id,provider,provider_id,is_primary) VALUES('movie','tmdb','1',1)"
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
                        "trailers": [
                            {
                                "url": "https://www.youtube.com/watch?v=cached-trailer",
                                "site": "YouTube",
                                "key": "cached-trailer",
                                "language": "en",
                            }
                        ],
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
                self.assertEqual(
                    value["trailers"][0]["url"],
                    "https://www.youtube.com/watch?v=cached-trailer",
                )
                self.assertEqual(value["_catalogItemProjectionSchema"], 2)
                self.assertRegex(
                    value["images"]["Primary"]["url"],
                    r"^/api/catalog/items/movie/images/Primary\?language=en&v=[0-9a-f]{12}$",
                )
                self.assertEqual(
                    value["images"]["Primary"]["blurHash"],
                    "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
                )
            finally:
                database.close()

    def test_image_ingest_reprojects_cached_hash_and_keeps_logo_hash_free(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            try:
                database.execute(
                    "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT)"
                )
                database.execute(
                    "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,provider_id TEXT,is_primary INTEGER NOT NULL DEFAULT 0)"
                )
                database.execute(
                    "CREATE TABLE catalog_search(entity_id TEXT,library_id TEXT,locale TEXT,title TEXT)"
                )
                database.execute(
                    "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,entity_type TEXT,payload TEXT,title_sort TEXT,rating_sort REAL,release_sort TEXT,runtime_sort REAL,updated_at TEXT,generation INTEGER,PRIMARY KEY(entity_id,locale))"
                )
                database.execute(
                    "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,blur_hash TEXT,local_path TEXT,fetched_at TEXT,expires_at TEXT,PRIMARY KEY(provider,entity_type,provider_id,locale,image_type,image_url))"
                )
                database.execute(
                    "INSERT INTO library_entities VALUES('movie','library',NULL,'movie')"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids(entity_id,provider,provider_id,is_primary) VALUES('movie','tmdb','1',1)"
                )
                database.execute(
                    "CREATE TABLE catalog_search_grams(gram TEXT,entity_id TEXT,locale TEXT,library_id TEXT,parent_id TEXT,PRIMARY KEY(gram,entity_id,locale))"
                )
                database.execute(
                    "CREATE TABLE catalog_item_genres(entity_id TEXT,locale TEXT,genre_key TEXT,genre_name TEXT,PRIMARY KEY(entity_id,locale,genre_key))"
                )
                database.execute(
                    "INSERT INTO catalog_item_projection(entity_id,locale,payload) VALUES(?,?,?)",
                    ("movie", "en", json.dumps({"images": {}})),
                )
                cache = MetadataCache.__new__(MetadataCache)
                cache.db = database
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
                    "1",
                    "en",
                    {
                        "images": [
                            {
                                "type": "Primary",
                                "language": "en",
                                "url": "https://images.example/movie.jpg",
                            },
                            {
                                "type": "Logo",
                                "language": "en",
                                "url": "https://images.example/movie-logo.png",
                            },
                        ]
                    },
                )
                value = json.loads(
                    database.execute(
                        "SELECT payload FROM catalog_item_projection WHERE entity_id='movie' AND locale='en'"
                    )[0][0]
                )
                self.assertEqual(
                    value["images"]["Primary"]["blurHash"],
                    "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
                )
                self.assertNotIn("blurHash", value["images"]["Logo"])
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
                    "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,provider_id TEXT,is_primary INTEGER NOT NULL DEFAULT 0)"
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
                    "INSERT INTO entity_provider_ids(entity_id,provider,provider_id,is_primary) VALUES('movie','tmdb','1',1)"
                )
                database.execute(
                    "INSERT INTO entity_provider_ids(entity_id,provider,provider_id,is_primary) VALUES('movie','tvdb','2',0)"
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
        self._cache(
            "ja",
            {
                "title": "Japanese",
                "originalLanguage": "ja",
                "images": [
                    {
                        "type": "Primary",
                        "url": "neutral.jpg",
                        "language": None,
                        "provider": "tmdb",
                    }
                ],
            },
        )
        self._cache("en", {"overview": "English overview", "originalLanguage": "ja"})
        self._cache("de", {"title": "German only"})
        service = MetadataReadService(self.db)
        value = service.resolve_raw("movie", [{"provider": "tmdb", "id": "10"}], "ja")
        self.assertEqual(value["title"], "Japanese")
        self.assertEqual(value["overview"], "English overview")
        self.assertEqual(value["images"][0]["url"], "neutral.jpg")

    def test_read_fallback_ignores_language_code_overview_placeholders(self):
        self._cache(
            "ja", {"title": "Japanese", "overview": "eng", "originalLanguage": "ja"}
        )
        self._cache("en", {"overview": "English overview", "originalLanguage": "ja"})
        service = MetadataReadService(self.db)
        value = service.resolve_raw("movie", [{"provider": "tmdb", "id": "10"}], "ja")
        self.assertEqual(value["overview"], "English overview")

    def test_english_is_optional_and_original_is_only_used_when_cached(self):
        self._cache("ja", {"title": "Japanese", "originalLanguage": "ja"})
        service = MetadataReadService(self.db)
        value = service.resolve_raw("movie", [{"provider": "tmdb", "id": "10"}], "fr")
        self.assertEqual(value["title"], "Japanese")

    def test_public_artwork_exposes_the_matching_cached_blurhash(self):
        self.db.execute(
            "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,blur_hash TEXT,fetched_at TEXT)"
        )
        self._cache(
            "en",
            {
                "images": [
                    {
                        "type": "Primary",
                        "url": "poster.jpg",
                        "language": "en",
                        "provider": "tmdb",
                    }
                ]
            },
        )
        self.db.execute(
            "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?)",
            (
                "tmdb",
                "movie",
                "10",
                "en",
                "Primary",
                "poster.jpg",
                "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
                "now",
            ),
        )

        value = MetadataReadService(self.db).resolve_public(
            "movie", "movie", [{"provider": "tmdb", "id": "10"}], "en"
        )

        self.assertEqual(
            value["metadata"]["images"]["Primary"]["blurHash"],
            "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        )

    def test_public_artwork_does_not_cache_empty_selection(self):
        service = MetadataReadService(self.db)
        service.resolve_raw = MagicMock(
            side_effect=[
                {"images": [], "originalLanguage": "en"},
                {"images": [{"type": "Primary", "url": "poster.jpg"}]},
            ]
        )

        def ready_artwork(_entity_type, _provider_ids, _images, _requested, image_type, *_args, **_kwargs):
            if image_type == "Primary" and service.resolve_raw.call_count > 1:
                return {
                    "provider": "tmdb",
                    "url": "poster.jpg",
                    "language": "en",
                }
            return None

        service.ready_artwork = MagicMock(side_effect=ready_artwork)
        provider_ids = [{"provider": "tmdb", "id": "10"}]

        first = service.resolve_public("entity", "movie", provider_ids, "en")
        second = service.resolve_public("entity", "movie", provider_ids, "en")

        self.assertEqual(first["metadata"]["images"], {})
        self.assertEqual(
            second["metadata"]["images"]["Primary"]["url"],
            "/api/catalog/items/entity/images/Primary?language=en",
        )
        self.assertEqual(service.resolve_raw.call_count, 2)

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
                _Fetcher(),
                _Settings(["ja", "de"]),
                image_ingest=image_ingest,
                background_assets=False,
            )
            ingest.ingest("tmdb", "movie", "10")

            self.assertEqual(len(cache.rows), 2)
            self.assertTrue(
                all(value[-1] and Path(value[-1]).is_file() for value in cache.rows)
            )
            self.assertTrue(
                all(str(value[-1]).endswith(".webp") for value in cache.rows)
            )

    def test_single_locale_refresh_removes_replaced_cached_artwork(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            try:
                database.execute(
                    "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,blur_hash TEXT,local_path TEXT,fetched_at TEXT,expires_at TEXT,PRIMARY KEY(provider,entity_type,provider_id,locale,image_type,image_url))"
                )
                old_path = Path(directory) / "old.webp"
                old_path.write_bytes(b"old")
                old_url = "https://images.example/old.jpg"
                new_url = "https://images.example/new.jpg"
                database.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "tmdb",
                        "movie",
                        "10",
                        "en",
                        "Primary",
                        old_url,
                        "old-hash",
                        str(old_path),
                        "now",
                        "later",
                    ),
                )
                cache = MetadataCache.__new__(MetadataCache)
                cache.db = database
                fetcher = MagicMock()
                fetcher.cache = cache
                fetcher.fetch_locales.return_value = {
                    "en": {
                        "images": [
                            {
                                "type": "Primary",
                                "language": "en",
                                "url": new_url,
                            }
                        ]
                    }
                }
                image_ingest = MetadataImageIngestService(
                    cache,
                    directory,
                    downloader=lambda url: b"new-image",
                    encoder=lambda content, target, suffix: target.write_bytes(b"webp"),
                    hasher=lambda target: "new-hash",
                )
                ingest = MetadataIngestService(
                    fetcher,
                    _Settings(["en"]),
                    image_ingest=image_ingest,
                    background_assets=False,
                )
                with patch.object(MetadataSearchProjection, "project"):
                    ingest.ingest_locales("tmdb", "movie", "10", ["en"])

                rows = database.execute(
                    "SELECT image_url,local_path FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=? AND image_type=?",
                    ("tmdb", "movie", "10", "Primary"),
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], new_url)
                self.assertTrue(Path(rows[0][1]).is_file())
                self.assertFalse(old_path.exists())
            finally:
                database.close()

    def test_partial_locale_refresh_keeps_multilocale_artwork_non_destructive(self):
        image_ingest = MagicMock()
        ingest = MetadataIngestService(
            _Fetcher(),
            _Settings(["en", "ja"]),
            image_ingest=image_ingest,
            background_assets=False,
        )

        ingest.ingest_locale("tmdb", "movie", "10", "en")

        image_ingest.ingest.assert_called_once_with(
            "tmdb",
            "movie",
            "10",
            "en",
            {
                "title": "en",
                "images": [
                    {
                        "type": "Primary",
                        "url": "https://images.example/en.jpg",
                        "language": "en",
                    }
                ],
            },
            force=False,
            complete_batch=False,
        )

    def test_image_ingest_materializes_only_the_first_provider_candidate(self):
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
            result = image_ingest.ingest(
                "tmdb",
                "movie",
                "10",
                "en",
                {
                    "images": [
                        {
                            "type": "Primary",
                            "url": "https://images.example/first.jpg",
                            "language": "en",
                            "score": 0,
                        },
                        {
                            "type": "Primary",
                            "url": "https://images.example/second.jpg",
                            "language": "en",
                            "score": 100,
                        },
                    ]
                },
            )

        self.assertEqual(downloads, ["https://images.example/first.jpg"])
        self.assertEqual(result, {"ready": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(cache.rows), 1)

    def test_batch_image_ingest_deduplicates_a_shared_winner_across_locales(self):
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
            image_ingest.ingest_documents(
                "tmdb",
                "movie",
                "10",
                {
                    "en": {
                        "images": [
                            {
                                "type": "Primary",
                                "url": "https://images.example/shared.jpg",
                                "language": "en",
                            }
                        ]
                    },
                    "en-US": {
                        "images": [
                            {
                                "type": "Primary",
                                "url": "https://images.example/shared.jpg",
                                "language": "en",
                            }
                        ]
                    },
                },
            )

        self.assertEqual(downloads, ["https://images.example/shared.jpg"])
        self.assertEqual(len(cache.rows), 2)

    def test_image_ingest_tries_only_one_native_fallback_after_winner_failure(self):
        cache = _ImageCache(self.db)
        downloads = []

        def downloader(url):
            downloads.append(url)
            if url.endswith("first.jpg"):
                raise RuntimeError("winner unavailable")
            return b"image-data"

        with tempfile.TemporaryDirectory() as directory:
            image_ingest = MetadataImageIngestService(
                cache,
                directory,
                downloader=downloader,
                encoder=lambda content, target, suffix: target.write_bytes(b"webp"),
                hasher=lambda target: "hash",
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
                            "url": "https://images.example/first.jpg",
                            "language": "en",
                        },
                        {
                            "type": "Primary",
                            "url": "https://images.example/second.jpg",
                            "language": "en",
                        },
                        {
                            "type": "Primary",
                            "url": "https://images.example/third.jpg",
                            "language": "en",
                        },
                    ]
                },
            )

        self.assertEqual(
            downloads,
            [
                "https://images.example/first.jpg",
                "https://images.example/second.jpg",
            ],
        )
        self.assertEqual(result, {"ready": 1, "failed": 1, "skipped": 0})

    @staticmethod
    def test_forced_ingest_redownloads_existing_artwork_and_portraits():
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
            complete_batch=True,
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

    def test_image_download_enters_stream_response_context(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            if request.url.path == "/poster.jpg":
                return httpx.Response(
                    302, headers={"location": "/redirected-poster.jpg"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"image-data",
            )

        cache = _ImageCache(self.db)
        client = httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                image_ingest = MetadataImageIngestService(
                    cache,
                    directory,
                    encoder=lambda content, target, suffix: target.write_bytes(
                        bytes(content)
                    ),
                )
                target = Path(directory) / "poster.webp"
                with (
                    patch.object(image_ingest, "_validate_provider_url"),
                    patch.object(
                        MetadataImageIngestService._http_local,
                        "client",
                        client,
                        create=True,
                    ),
                ):
                    image_ingest._download("https://images.example/poster.jpg", target)

                self.assertEqual(target.read_bytes(), b"image-data")
        finally:
            client.close()

        self.assertEqual(
            requests,
            [
                "https://images.example/poster.jpg",
                "https://images.example/redirected-poster.jpg",
            ],
        )

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
                _Fetcher(),
                _Settings(["en"]),
                image_ingest=image_ingest,
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
                {
                    "images": [
                        {"type": "Primary", "url": "https://images.example/poster.jpg"}
                    ]
                },
            )

        self.assertEqual(cache.rows[0][-2], "LEHV6nWB2yk8pyo0adR*.7kCMdnj")


if __name__ == "__main__":
    unittest.main()
