import unittest
from unittest.mock import patch

from app.providers import MusicBrainzClient


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
