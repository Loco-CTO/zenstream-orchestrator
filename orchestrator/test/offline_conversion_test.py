import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.ffmpeg_supervisor import run_ffmpeg
from app.offline_assets import OfflineAssets, source_signature
from app.offline_conversion import prepare_video, probe_file, video_command
from app.playback import ffmpeg_path, ffprobe_path
from fastapi import HTTPException


class OfflineVideoConversionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ffmpeg_path() or not ffprobe_path():
            raise unittest.SkipTest(
                "FFmpeg and FFprobe are required for media fixtures"
            )
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.path = Path(cls.directory.name) / "pattern.mp4"
        run_ffmpeg(
            [
                ffmpeg_path(),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=24",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-c:a",
                "aac",
                str(cls.path),
            ],
            timeout_seconds=30,
        )

    def setUp(self):
        self.source = {
            "path": self.path,
            "probe": probe_file(self.path),
            "videoCodec": "h264",
            "durationSeconds": 1,
        }
        self.request = {
            "profile": "video-720p",
            "sourceRevision": source_signature(self.path.stat()),
            "audioStreamId": 1,
        }
        db = MagicMock()
        db.read_execute.return_value = [(0,)]
        self.assets = SimpleNamespace(
            queue=SimpleNamespace(
                cache_directory=Path(self.directory.name) / "prepared"
            ),
            db=db,
            tracks=OfflineAssets.tracks,
        )

    def test_portable_output_decodes_and_seeks_without_upscaling(self):
        result = prepare_video(
            self.assets, "a" * 64, self.source, self.request, lambda: False
        )
        target = self.assets.queue.cache_directory / result["storageName"]
        probe = probe_file(target)
        video = next(
            stream for stream in probe["streams"] if stream["codec_type"] == "video"
        )
        self.assertEqual((video["width"], video["height"]), (320, 180))
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(result["mimeType"], "video/mp4")
        pixels = run_ffmpeg(
            [
                ffmpeg_path(),
                "-v",
                "error",
                "-ss",
                "0.5",
                "-i",
                str(target),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            timeout_seconds=30,
        )
        self.assertEqual(len(pixels), 320 * 180 * 3)
        self.assertGreater(len(set(pixels)), 50)
        self.assertFalse(list(target.parent.glob("*.part")))

    @patch(
        "app.offline_conversion.conversion_capabilities",
        return_value={"video": True, "audio": True, "toneMapping": False},
    )
    def test_hdr_conversion_requires_verified_filter_support(self, _capabilities):
        self.source["probe"]["streams"][0]["color_transfer"] = "smpte2084"
        with self.assertRaises(HTTPException):
            video_command(self.source, self.request, Path("output.mp4"))

    def test_audio_selection_is_validated(self):
        self.request["audioStreamId"] = 999
        with self.assertRaises(HTTPException):
            video_command(self.source, self.request, Path("output.mp4"))

    def test_cancelled_conversion_never_publishes_a_complete_asset(self):
        with self.assertRaises(InterruptedError):
            prepare_video(
                self.assets, "b" * 64, self.source, self.request, lambda: True
            )
        self.assertFalse(
            (self.assets.queue.cache_directory / ("b" * 64 + ".mp4")).exists()
        )
        self.assertFalse(list(self.assets.queue.cache_directory.glob("*.part")))
