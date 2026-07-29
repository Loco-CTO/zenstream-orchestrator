import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from app.intro_outro import (
    INTRO_MIN_SECONDS,
    SAMPLE_SECONDS,
    IntroOutroDetector,
    audio_preview_command,
    decode_fingerprint,
    fingerprint_preview,
    shared_region,
)


class IntroOutroTest(unittest.TestCase):
    @patch("app.intro_outro.ffmpeg_path", return_value="ffmpeg")
    def test_fingerprint_command_uses_raw_chromaprint(self, _ffmpeg):
        command = IntroOutroDetector.fingerprint_command(Path("episode.mkv"), 0, 600)
        self.assertEqual(command[command.index("-f") + 1], "chromaprint")
        self.assertEqual(command[command.index("-fp_format") + 1], "raw")
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")

    def test_decodes_little_endian_fingerprint_points(self):
        self.assertEqual(decode_fingerprint(struct.pack("<3I", 1, 2, 3)), (1, 2, 3))
        self.assertEqual(decode_fingerprint(b"bad"), ())

    def test_downsamples_fingerprint_bit_density_for_dashboard_preview(self):
        preview = fingerprint_preview(struct.pack("<4I", 0, 0xFFFFFFFF, 0, 0xFFFFFFFF), maximum_samples=2)
        self.assertEqual(preview["pointCount"], 4)
        self.assertEqual(preview["values"], [16.0, 16.0])

    @patch("app.intro_outro.ffmpeg_path", return_value="ffmpeg")
    def test_audio_preview_command_outputs_an_mp3_stream(self, _ffmpeg):
        command = audio_preview_command(Path("episode.mkv"), 5, 30)
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertEqual(command[command.index("-c:a") + 1], "mp3")
        self.assertEqual(command[-1], "-")

    def test_finds_a_long_shared_region_after_an_offset(self):
        points = max(130, int(INTRO_MIN_SECONDS / SAMPLE_SECONDS) + 4)
        shared = tuple(range(1000, 1000 + points))
        result = shared_region((0xFFFFFFFF, 0xFFFFFFFE, *shared), (0xAAAA0000, 0xAAAA0001, 0xAAAA0002, *shared), 15, 120)
        self.assertIsNotNone(result)
        left_start, left_end, right_start, right_end = result
        self.assertAlmostEqual(left_start, 2 * SAMPLE_SECONDS)
        self.assertGreaterEqual(left_end - left_start, INTRO_MIN_SECONDS)
        self.assertAlmostEqual(right_start, 3 * SAMPLE_SECONDS)
        self.assertAlmostEqual(right_end - right_start, left_end - left_start)

    def test_rejects_short_matches(self):
        self.assertIsNone(shared_region(tuple(range(30)), tuple(range(30)), 15, 120))
