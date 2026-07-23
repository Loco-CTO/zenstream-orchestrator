import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.playback import PlaybackManager


class PlaybackTest(unittest.TestCase):
    def test_empty_capabilities_mean_no_direct_support(self):
        source = {
            "container": "mp4",
            "videoCodec": "h264",
            "audioCodec": "aac",
        }
        self.assertFalse(
            PlaybackManager._direct(
                source,
                {"containers": [], "videoCodecs": [], "audioCodecs": []},
            )
        )

    def test_omitted_capabilities_use_defaults(self):
        source = {
            "container": "mp4",
            "videoCodec": "H264",
            "audioCodec": "AAC",
        }
        self.assertTrue(PlaybackManager._direct(source, {}))

    def test_matroska_and_bitrate_are_normalized(self):
        source = {
            "container": "matroska,webm",
            "videoCodec": "h264",
            "audioCodec": "aac",
            "bitrate": 4_000_000,
        }
        profile = {
            "containers": ["mkv"],
            "videoCodecs": ["h264"],
            "audioCodecs": ["aac"],
            "maxStreamingBitrate": 2_000_000,
        }
        self.assertFalse(PlaybackManager._direct(source, profile))

    def test_sources_reads_media_file_id_and_media_role(self):
        manager = object.__new__(PlaybackManager)
        manager.catalog = MagicMock()
        manager.db = MagicMock()
        manager.db.execute.side_effect = [
            [
                (
                    "source-1",
                    "file-1",
                    "mp4",
                    120.0,
                    1_000_000,
                    1920,
                    1080,
                    "h264",
                    "aac",
                    json.dumps({"streams": []}),
                )
            ],
            [],
        ]

        sources = manager.sources("user-1", "entity-1")

        self.assertEqual(sources[0]["mediaFileId"], "file-1")
        media_query = manager.db.execute.call_args_list[0].args[0]
        self.assertIn("role='subtitle'", manager.db.execute.call_args_list[1].args[0])
        self.assertIn("media_sources", media_query)

    @patch("app.playback.issue_ticket", return_value="ticket")
    def test_negotiate_returns_readiness_error_when_no_source(self, _ticket):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(return_value=[])

        with self.assertRaises(Exception) as context:
            manager.negotiate("user-1", "entity-1", {})

        error = context.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail["code"], "MEDIA_NOT_READY")

    @patch("app.playback.issue_ticket", return_value="ticket")
    def test_negotiate_selects_requested_source(self, _ticket):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[
                {"id": "source-1", "mediaFileId": "file-1", "container": "mp4", "videoCodec": "h264", "audioCodec": "aac"},
                {"id": "source-2", "mediaFileId": "file-2", "container": "webm", "videoCodec": "vp9", "audioCodec": "opus"},
            ]
        )

        result = manager.negotiate(
            "user-1",
            "entity-1",
            {"mediaSourceId": "source-2", "containers": ["webm"], "videoCodecs": ["vp9"], "audioCodecs": ["opus"]},
        )

        self.assertEqual(result["source"]["id"], "source-2")
        self.assertIn("mediaSourceId=source-2", result["url"])

    @patch("app.playback.issue_ticket", return_value="ticket")
    def test_direct_only_does_not_start_transcoding(self, _ticket):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[
                {"id": "source-1", "mediaFileId": "file-1", "container": "mkv", "videoCodec": "hevc", "audioCodec": "ac3"}
            ]
        )
        manager._transcode = MagicMock()

        with self.assertRaises(Exception) as context:
            manager.negotiate(
                "user-1",
                "entity-1",
                {"directPlayOnly": True, "containers": ["mp4"], "videoCodecs": ["h264"], "audioCodecs": ["aac"]},
            )

        self.assertEqual(context.exception.status_code, 409)
        manager._transcode.assert_not_called()

    @patch("app.playback.ffmpeg_path", return_value=None)
    def test_forced_transcoding_requires_ffmpeg(self, _ffmpeg):
        manager = object.__new__(PlaybackManager)
        manager.sources = MagicMock(
            return_value=[{"id": "source-1", "mediaFileId": "file-1", "container": "mp4", "videoCodec": "h264", "audioCodec": "aac"}]
        )

        with self.assertRaises(Exception) as context:
            manager.negotiate("user-1", "entity-1", {"forceTranscoding": True})

        self.assertEqual(context.exception.status_code, 503)
