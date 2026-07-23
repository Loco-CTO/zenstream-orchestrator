import json
import tempfile
import unittest
from pathlib import Path

from app.database import DatabaseHandler
from app.metadata_services import (
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
                cache, directory, downloader=lambda url: b"image-data"
            )
            ingest = MetadataIngestService(
                _Fetcher(), _Settings(["ja", "de"]), image_ingest=image_ingest
            )
            ingest.ingest("tmdb", "movie", "10")

            self.assertEqual(len(cache.rows), 2)
            self.assertTrue(all(value[-1] and Path(value[-1]).is_file() for value in cache.rows))

    def test_ingest_document_materializes_aggregated_series(self):
        cache = _ImageCache(self.db)
        with tempfile.TemporaryDirectory() as directory:
            image_ingest = MetadataImageIngestService(
                cache, directory, downloader=lambda url: b"series-image"
            )
            ingest = MetadataIngestService(
                _Fetcher(), _Settings(["en"]), image_ingest=image_ingest
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


if __name__ == "__main__":
    unittest.main()
