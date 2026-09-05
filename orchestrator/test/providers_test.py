import unittest
from unittest.mock import patch

import httpx
from app.providers import MusicBrainzClient, ProviderClient


class MusicBrainzLookupTest(unittest.TestCase):
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
        self.assertEqual(value["tracks"][0]["contributingArtists"], value["tracks"][0]["artists"])

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
                    headers={"location": "https://archive.org/download/mbid-id/index.json"},
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
