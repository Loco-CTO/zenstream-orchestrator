import unittest
from unittest.mock import patch

import httpx
from app.providers import (
    MusicBrainzClient,
    ProviderClient,
    ProviderError,
    _select_music_match,
)


class MusicBrainzLookupTest(unittest.TestCase):
    @patch.object(
        MusicBrainzClient,
        "_request",
        return_value={
            "releases": [
                {
                    "id": "release-id",
                    "title": "A+B (Live)",
                    "date": "2024-01-02",
                    "artist-credit": [
                        {"artist": {"id": "artist-id", "name": "Artist"}}
                    ],
                }
            ]
        },
    )
    def test_release_search_uses_structured_escaped_fields(self, request):
        values = MusicBrainzClient().search_releases("A+B (Live)", "Artist", "2024")

        self.assertEqual(values[0]["providerId"], "release-id")
        self.assertEqual(values[0]["artistIds"], ["artist-id"])
        request.assert_called_once_with(
            "/release",
            {
                "query": 'release:"A\\+B \\(Live\\)" AND artistname:"Artist" AND date:"2024"',
                "limit": 10,
            },
        )

    @patch.object(
        MusicBrainzClient,
        "_request",
        return_value={
            "recordings": [
                {
                    "id": "recording-id",
                    "title": "Track",
                    "artist-credit": [
                        {"artist": {"id": "artist-id", "name": "Artist"}}
                    ],
                }
            ]
        },
    )
    def test_recording_search_includes_album_year_and_duration_context(self, request):
        MusicBrainzClient().search_recordings("Track", "Artist", "Album", "2024", 123.4)

        request.assert_called_once_with(
            "/recording",
            {
                "query": 'recording:"Track" AND artistname:"Artist" AND release:"Album" AND firstreleasedate:"2024" AND dur:"123400"',
                "limit": 10,
            },
        )

    def test_music_match_keeps_unicode_and_requires_unique_high_confidence(self):
        candidate = {
            "providerId": "release-id",
            "title": "創生α",
            "artists": [{"id": "artist-id", "name": "ヰ世界情緒"}],
            "year": "2021",
        }
        self.assertEqual(
            _select_music_match([candidate], "創生α", "ヰ世界情緒", "2021"),
            "release-id",
        )
        with self.assertRaises(ProviderError):
            _select_music_match(
                [candidate, {**candidate, "providerId": "other-id"}],
                "創生α",
                "ヰ世界情緒",
                "2021",
            )

    @patch.object(MusicBrainzClient, "_request", return_value={})
    def test_recording_lookup_uses_recording_supported_includes(self, request):
        MusicBrainzClient().details(
            "track", "32247b86-994f-405c-ae9f-1599aaec79c3", "en"
        )

        request.assert_called_once_with(
            "/recording/32247b86-994f-405c-ae9f-1599aaec79c3",
            {"inc": "artist-credits+isrcs+tags"},
        )

    def test_release_normalization_keeps_recording_artist_credits_on_tracks(self):
        value = MusicBrainzClient.normalize(
            "release",
            "release-id",
            {
                "id": "release-id",
                "title": "Album",
                "artist-credit": [
                    {"artist": {"id": "album-artist-id", "name": "Album Artist"}}
                ],
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {
                                "position": "1",
                                "title": "Track",
                                "recording": {
                                    "id": "recording-id",
                                    "title": "Track",
                                    "artist-credit": [
                                        {
                                            "artist": {
                                                "id": "track-artist-id",
                                                "name": "Track Artist",
                                            },
                                            "joinphrase": " & ",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
        )

        self.assertEqual(
            value["tracks"][0]["artists"],
            [
                {
                    "id": "track-artist-id",
                    "name": "Track Artist",
                    "joinPhrase": " & ",
                }
            ],
        )
        self.assertEqual(
            value["tracks"][0]["contributingArtists"], value["tracks"][0]["artists"]
        )

    def test_release_normalization_keeps_ordered_multi_artist_credit(self):
        value = MusicBrainzClient.normalize(
            "release",
            "release-id",
            {
                "id": "release-id",
                "title": "new world",
                "artist-credit": [
                    {
                        "artist": {"id": "aiobahn-id", "name": "Aiobahn"},
                        "joinphrase": " feat. ",
                    },
                    {
                        "artist": {
                            "id": "uisekai-id",
                            "name": "ヰ世界情緒",
                        },
                        "joinphrase": "",
                    },
                ],
            },
        )

        self.assertEqual(value["albumArtist"], "Aiobahn")
        self.assertEqual(
            value["artists"],
            [
                {"id": "aiobahn-id", "name": "Aiobahn", "joinPhrase": " feat. "},
                {"id": "uisekai-id", "name": "ヰ世界情緒", "joinPhrase": ""},
            ],
        )
        self.assertEqual(value["contributingArtists"], value["artists"])

    def test_release_normalization_keeps_release_group_types(self):
        value = MusicBrainzClient.normalize(
            "release",
            "release-id",
            {
                "id": "release-id",
                "title": "Live EP",
                "release-group": {
                    "id": "release-group-id",
                    "primary-type": "EP",
                    "secondary-types": ["Live", "Remix"],
                },
            },
        )

        self.assertEqual(value["albumType"], "EP")
        self.assertEqual(value["albumSecondaryTypes"], ["Live", "Remix"])

    def test_recording_normalization_keeps_work_provider_ids(self):
        value = MusicBrainzClient.normalize(
            "track",
            "recording-id",
            {
                "id": "recording-id",
                "title": "Track",
                "works": [{"id": "work-id", "title": "Work"}],
            },
        )

        self.assertIn(
            {
                "provider": "musicbrainz",
                "identifierType": "work",
                "id": "work-id",
            },
            value["ids"],
        )

    @patch.object(MusicBrainzClient, "_get", return_value={})
    @patch.object(MusicBrainzClient, "_request", return_value={})
    def test_release_lookup_keeps_media_and_label_includes(self, request, get):
        MusicBrainzClient().details("release", "release-id", "en")

        request.assert_called_once_with(
            "/release/release-id",
            {
                "inc": "artist-credits+labels+recordings+release-groups+media+discids+isrcs+tags"
            },
        )
        get.assert_called_once()
        self.assertTrue(get.call_args.kwargs["follow_redirects"])

    @patch.object(MusicBrainzClient, "_get", return_value={})
    @patch.object(MusicBrainzClient, "_request", return_value={"id": "release-id"})
    def test_locale_batch_fetches_musicbrainz_once(self, request, get):
        values = MusicBrainzClient().details_all_locales(
            "release", "release-id", ["en", "ja", "zh-TW"]
        )

        self.assertEqual(request.call_count, 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(set(values), {"en", "ja", "zh-TW"})
        self.assertIsNot(values["en"], values["ja"])

    def test_provider_requests_can_follow_cover_art_archive_redirects(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            if request.url.path == "/release/release-id":
                return httpx.Response(
                    307,
                    headers={
                        "location": "https://archive.org/download/mbid-id/index.json"
                    },
                )
            return httpx.Response(200, json={"images": []})

        client = ProviderClient()
        transport_client = httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        try:
            with patch.object(client, "_http_client", return_value=transport_client):
                self.assertEqual(
                    client._get(
                        "https://coverartarchive.org/release/release-id",
                        follow_redirects=True,
                    ),
                    {"images": []},
                )
        finally:
            transport_client.close()

        self.assertEqual(
            requests,
            [
                "https://coverartarchive.org/release/release-id",
                "https://archive.org/download/mbid-id/index.json",
            ],
        )
