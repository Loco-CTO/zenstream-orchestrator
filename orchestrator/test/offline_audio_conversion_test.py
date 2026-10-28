import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.ffmpeg_supervisor import run_ffmpeg
from app.offline_assets import OfflineAssets, source_signature
from app.offline_conversion import audio_command, prepare_audio, probe_file
from app.playback import ffmpeg_path, ffprobe_path


class OfflineAudioConversionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ffmpeg_path() or not ffprobe_path():
            raise unittest.SkipTest(
                "FFmpeg and FFprobe are required for audio fixtures"
            )
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.path = Path(cls.directory.name) / "music.flac"
        run_ffmpeg(
            [
                ffmpeg_path(),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=96000",
                "-t",
                "1",
                "-ac",
                "2",
                "-c:a",
                "flac",
                "-metadata",
                "REPLAYGAIN_TRACK_GAIN=-4.5 dB",
                "-metadata",
                "LANGUAGE=jpn",
                "-metadata",
                "TITLE=Japanese FLAC",
                str(cls.path),
            ],
            timeout_seconds=30,
        )

    def test_both_aac_profiles_decode_and_preserve_duration_and_gain_metadata(self):
        for profile in ("audio-128k", "audio-256k"):
            with self.subTest(profile=profile):
                source = {
                    "path": self.path,
                    "probe": probe_file(self.path),
                    "videoCodec": None,
                    "durationSeconds": 1,
                }
                request = {
                    "profile": profile,
                    "sourceRevision": source_signature(self.path.stat()),
                    "audioStreamId": 0,
                }
                db = MagicMock()
                db.read_execute.return_value = [(0,)]
                assets = SimpleNamespace(
                    queue=SimpleNamespace(
                        cache_directory=Path(self.directory.name) / profile
                    ),
                    db=db,
                    tracks=OfflineAssets.tracks,
                )
                result = prepare_audio(assets, "a" * 64, source, request, lambda: False)
                output = assets.queue.cache_directory / result["storageName"]
                probe = probe_file(output)
                audio = probe["streams"][0]
                self.assertEqual(audio["codec_name"], "aac")
                self.assertEqual(audio["channels"], 2)
                self.assertEqual(int(audio["sample_rate"]), 48000)
                self.assertAlmostEqual(result["durationSeconds"], 1, delta=0.1)
                self.assertEqual(result["mimeType"], "audio/mp4")
                self.assertEqual(audio["tags"]["language"], "jpn")
                manifest = result["trackManifest"][0]
                self.assertEqual(manifest["language"], "jpn")
                self.assertEqual(manifest["displayTitle"], "Japanese FLAC")
                self.assertEqual(manifest["sourceIndex"], 0)
                self.assertEqual(
                    result["replayGain"]["replaygain_track_gain"], "-4.5 dB"
                )
                decoded = run_ffmpeg(
                    [
                        ffmpeg_path(),
                        "-v",
                        "error",
                        "-i",
                        str(output),
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "pipe:1",
                    ],
                    timeout_seconds=30,
                )
                self.assertGreater(len(decoded), 180000)
                self.assertGreater(len(set(decoded)), 30)
                command = audio_command(source, request, Path("output.m4a"))
                self.assertIn("128k" if profile == "audio-128k" else "256k", command)
