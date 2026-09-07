import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import DatabaseHandler
from app.metadata_services import MetadataReadService
from app.models.metadata import IMAGE_LANGUAGE_SCHEMA
from app.providers import LastFmClient, ProviderError


class _Settings:
    def get(self):
        return ["en"]

    def prefer_no_language_for_backdrop(self):
        return False


class LastFmClientTest(unittest.TestCase):
    def test_details_uses_api_key_and_structured_lookup_parameters(self):
        client = LastFmClient({"apiKey": "test-key"})
        lookup = LastFmClient.lookup_key(
            "release", artist_name="Beyoncé", album_name="Renaissance"
        )
        with patch.object(
            client,
            "_get",
            return_value={"album": {"name": "Renaissance"}},
        ) as request:
            client.details("release", lookup, "en-US")

        params = request.call_args.kwargs["params"]
        self.assertEqual(request.call_args.args[0], LastFmClient.base_url)
        self.assertEqual(params["method"], "album.getInfo")
        self.assertEqual(params["api_key"], "test-key")
        self.assertEqual(params["artist"], "Beyoncé")
        self.assertEqual(params["album"], "Renaissance")
        self.assertEqual(params["lang"], "en")
        self.assertEqual(params["autocorrect"], "0")

    def test_resolve_lookup_rejects_mismatched_mbid_then_uses_exact_name_match(self):
        client = LastFmClient({"apiKey": "test-key"})
        with patch.object(
            client,
            "details",
            side_effect=[
                {"album": {"name": "Different Album", "artist": {"name": "Artist"}}},
                {"album": {"name": "Album", "artist": {"name": "Artist"}}},
            ],
        ) as details:
            lookup, _payload = client.resolve_lookup(
                "release",
                artist_name="Artist",
                album_name="Album",
                mbid="release-mbid",
            )

        self.assertTrue(lookup.startswith("name:"))
        self.assertEqual(details.call_count, 2)

    def test_normalize_keeps_lastfm_namespace_and_unions_album_track_data(self):
        normalized = LastFmClient.normalize(
            "release",
            "name:lookup",
            {
                "album": {
                    "name": "Album",
                    "mbid": "album-mbid",
                    "url": "https://www.last.fm/music/Artist/Album",
                    "artist": {"name": "Artist", "mbid": "artist-mbid"},
                    "releasedate": "01 Jan 2024, 00:00",
                    "listeners": "42",
                    "playcount": "100",
                    "bio": {
                        "summary": "Biography summary <a href=\"https://www.last.fm/music/Artist/+wiki\">Read more on Last.fm</a>.",
                        "content": "Long biography <a href=\"https://www.last.fm/music/Artist/+wiki\">Read more on Last.fm</a>.",
                    },
                    "image": [
                        {"#text": "https://lastfm.freetls.fastly.net/large.jpg", "size": "large"},
                        {"#text": "https://lastfm.freetls.fastly.net/mega.jpg", "size": "mega"},
                    ],
                    "tags": {
                        "tag": [
                            {"name": "indie", "url": "https://www.last.fm/tag/indie", "count": "9"},
                            {"name": "Indie", "url": "https://www.last.fm/tag/indie"},
                        ]
                    },
                    "tracks": {
                        "track": [
                            {
                                "name": "Song",
                                "duration": "180000",
                                "artist": {"name": "Artist"},
                                "mbid": "recording-mbid",
                            }
                        ]
                    },
                }
            },
        )

        self.assertEqual(normalized["title"], "Album")
        self.assertEqual(normalized["year"], "2024")
        self.assertEqual(normalized["overview"], "Long biography")
        self.assertNotIn("Read more on Last.fm", normalized["providers"]["lastfm"]["wiki"]["content"])
        self.assertEqual(normalized["tags"], ["indie"])
        self.assertEqual(normalized["tracks"][0]["title"], "Song")
        self.assertEqual(normalized["tracks"][0]["durationSeconds"], 180.0)
        self.assertEqual(
            normalized["images"][0]["url"],
            "https://lastfm.freetls.fastly.net/mega.jpg",
        )
        namespace = normalized["providers"]["lastfm"]
        self.assertEqual(namespace["mbid"], "album-mbid")
        self.assertEqual(namespace["listeners"], 42)
        self.assertEqual(namespace["playcount"], 100)
        self.assertEqual(namespace["tags"][0]["count"], 9)
        self.assertEqual(namespace["tracklist"][0]["mbid"], "recording-mbid")
        self.assertEqual(normalized["ids"][0]["provider"], "lastfm")

    def test_lastfm_errors_are_provider_errors_without_exposing_invalid_json(self):
        client = LastFmClient({"apiKey": "test-key"})
        with patch.object(
            client,
            "_get",
            return_value={"error": 6, "message": "The artist was not found"},
        ):
            with self.assertRaises(ProviderError):
                client.details("artist", LastFmClient.lookup_key("artist", artist_name="Missing"), "en")

    def test_music_reads_union_lastfm_tags_and_preserve_provider_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler("sqlite", {}, str(Path(directory) / "db.sqlite"))
            database.execute(
                "CREATE TABLE metadata_cache(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,payload TEXT,PRIMARY KEY(provider,entity_type,provider_id,locale))"
            )
            try:
                musicbrainz = {
                    "title": "Artist",
                    "overview": "Authoritative overview",
                    "tags": ["rock"],
                    "provider": "musicbrainz",
                    "providerId": "mb-artist",
                    "ids": [{"provider": "musicbrainz", "id": "mb-artist"}],
                    "images": [],
                }
                lastfm = {
                    "title": "Artist",
                    "overview": "Richer Last.fm overview",
                    "tags": ["indie", "rock"],
                    "provider": "lastfm",
                    "providerId": "name:artist",
                    "ids": [{"provider": "lastfm", "id": "name:artist"}],
                    "images": [],
                    "providers": {
                        "lastfm": {
                            "url": "https://www.last.fm/music/Artist",
                            "stats": {"listeners": 10},
                            "wiki": {
                                "content": "Cached biography <a href=\"https://www.last.fm/music/Artist/+wiki\">Read more on Last.fm</a>."
                            },
                        }
                    },
                }
                for provider, provider_id, value in (
                    ("musicbrainz", "mb-artist", musicbrainz),
                    ("lastfm", "name:artist", lastfm),
                ):
                    value = {
                        **value,
                        "_imageLanguageSchema": IMAGE_LANGUAGE_SCHEMA,
                    }
                    database.execute(
                        "INSERT INTO metadata_cache VALUES(?,?,?,?,?)",
                        (provider, "artist", provider_id, "en", json.dumps(value)),
                    )
                with patch(
                    "app.metadata_services.MetadataLanguageSettings",
                    return_value=_Settings(),
                ):
                    result = MetadataReadService(database).resolve_raw(
                        "artist",
                        [
                            {"provider": "musicbrainz", "id": "mb-artist"},
                            {"provider": "lastfm", "id": "name:artist"},
                        ],
                        "en",
                    )
                self.assertEqual(result["overview"], "Authoritative overview")
                self.assertEqual(result["tags"], ["rock", "indie"])
                self.assertEqual(
                    result["providers"]["lastfm"]["stats"]["listeners"], 10
                )
                self.assertEqual(
                    result["providers"]["lastfm"]["wiki"]["content"],
                    "Cached biography",
                )
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
