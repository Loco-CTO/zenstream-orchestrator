import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.offline_assets import OfflineAssetRequest
from app.offline_bundle import selected_subtitles
from app.offline_conversion import audio_indices
from fastapi import HTTPException


class OfflineTrackSelectionTest(unittest.TestCase):
    def test_include_all_audio_tracks_puts_default_first_stably(self):
        source = {
            "probe": {
                "streams": [
                    {"index": 0, "codec_type": "video"},
                    {
                        "index": 3,
                        "codec_type": "audio",
                        "disposition": {"default": 0},
                    },
                    {
                        "index": 5,
                        "codec_type": "audio",
                        "disposition": {"default": 1},
                    },
                    {"index": 6, "codec_type": "audio"},
                ]
            }
        }

        self.assertEqual(
            audio_indices(source, {"includeAllTracks": True, "audioStreamId": 3}),
            [5, 3, 6],
        )

    def test_legacy_audio_selection_stays_single_track(self):
        source = {
            "probe": {
                "streams": [
                    {"index": 3, "codec_type": "audio"},
                    {
                        "index": 5,
                        "codec_type": "audio",
                        "disposition": {"default": 1},
                    },
                ]
            }
        }

        self.assertEqual(audio_indices(source, {"audioStreamId": 3}), [3])
        self.assertEqual(audio_indices(source, {}), [5])

    def test_include_all_subtitles_returns_each_text_track_and_language(self):
        assets = SimpleNamespace(media=MagicMock())
        assets.media.sources.return_value = [
            {
                "id": "source",
                "streams": [
                    {
                        "index": 4,
                        "codec_type": "subtitle",
                        "codec_name": "ass",
                        "tags": {"language": "jpn", "title": "Signs"},
                    },
                    {
                        "index": 2,
                        "codec_type": "subtitle",
                        "codec_name": "subrip",
                        "tags": {"language": "eng", "title": "English"},
                    },
                ],
            }
        ]
        request = OfflineAssetRequest(
            selectionId="selection",
            itemId="movie",
            sourceId="source",
            profile="video-720p",
        )

        subtitles = selected_subtitles(assets, "viewer", request, include_all=True)

        self.assertEqual([track["index"] for track in subtitles], [2, 4])
        self.assertEqual([track["language"] for track in subtitles], ["eng", "jpn"])
        self.assertEqual([track["name"] for track in subtitles], ["English", "Signs"])

    def test_include_all_bitmap_subtitles_require_original(self):
        assets = SimpleNamespace(media=MagicMock())
        assets.media.sources.return_value = [
            {
                "id": "source",
                "streams": [
                    {
                        "index": 2,
                        "codec_type": "subtitle",
                        "codec_name": "hdmv_pgs_subtitle",
                    }
                ],
            }
        ]
        request = OfflineAssetRequest(
            selectionId="selection",
            itemId="movie",
            sourceId="source",
            profile="video-720p",
        )

        with self.assertRaises(HTTPException) as error:
            selected_subtitles(assets, "viewer", request, include_all=True)

        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
