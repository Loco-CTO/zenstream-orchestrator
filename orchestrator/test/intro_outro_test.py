import struct
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.intro_outro import (
    DEFAULTS,
    SAMPLE_SECONDS,
    IntroOutroDetector,
    audio_preview_command,
    decode_fingerprint,
    fingerprint_preview,
    normalize_settings,
    shared_region,
)


class IntroOutroTest(unittest.TestCase):
    @patch("app.intro_outro.ffmpeg_path", return_value="ffmpeg")
    def test_fingerprint_command_uses_raw_chromaprint(self, _ffmpeg):
        command = IntroOutroDetector.fingerprint_command(Path("episode.mkv"), 0, 600)
        self.assertEqual(command[command.index("-f") + 1], "chromaprint")
        self.assertEqual(command[command.index("-fp_format") + 1], "raw")
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertEqual(command[command.index("-threads") + 1], "1")

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
        points = max(130, int(DEFAULTS["minimumIntroDuration"] / SAMPLE_SECONDS) + 4)
        shared = tuple((1000 + index) * 0x9E3779B1 & 0xFFFFFFFF for index in range(points))
        result = shared_region((0xFFFFFFFF, 0xFFFFFFFE, *shared), (0xAAAA0000, 0xAAAA0001, 0xAAAA0002, *shared), DEFAULTS, 15, 120)
        self.assertIsNotNone(result)
        left_start, left_end, right_start, right_end = result
        self.assertAlmostEqual(left_start, 2 * SAMPLE_SECONDS)
        self.assertGreaterEqual(left_end - left_start, DEFAULTS["minimumIntroDuration"])
        self.assertAlmostEqual(right_start, 3 * SAMPLE_SECONDS)
        self.assertAlmostEqual(right_end - right_start, left_end - left_start)

    def test_rejects_short_matches(self):
        self.assertIsNone(shared_region(tuple(range(30)), tuple(range(30)), DEFAULTS, 15, 120))

    def test_rejects_sparse_near_matches(self):
        points = int(DEFAULTS["minimumIntroDuration"] / SAMPLE_SECONDS) + 10
        left = tuple(0xFFFFFFFF if index % 5 else index for index in range(points))
        right = tuple(0 if index % 5 else index for index in range(points))
        self.assertIsNone(shared_region(left, right, DEFAULTS, 15, 120))

    def test_worker_limit_defaults_and_is_bounded(self):
        self.assertEqual(DEFAULTS["introOutroWorkers"], 1)
        self.assertEqual(
            normalize_settings({"introOutroWorkers": 64})["introOutroWorkers"],
            64,
        )
        self.assertEqual(
            normalize_settings({"introOutroWorkers": 0})["introOutroWorkers"],
            1,
        )

    def test_detection_uses_configured_workers_and_claims_each_asset_once(self):
        class Store:
            def __init__(self):
                self.lock = threading.Lock()
                self.assets = [
                    {"mediaFileId": f"episode-{index}", "entityId": f"entity-{index}", "durationSeconds": 600, "path": Path(f"episode-{index}.mkv")}
                    for index in range(4)
                ]
                self.processed = []

            def settings(self):
                return {**DEFAULTS, "introOutroWorkers": 2}

            def queue_pending(self, settings=None):
                return len(self.assets)

            def claim_next(self):
                with self.lock:
                    return self.assets.pop(0) if self.assets else None

            def mark_fingerprinted(self, asset, intro, outro):
                with self.lock:
                    self.processed.append(asset["mediaFileId"])

            def mark_failed(self, asset, error):
                raise AssertionError(error)

            def recompute_all(self, settings):
                return 0

        class JobStore:
            def update_run(self, *args, **kwargs):
                return None

        store = Store()
        detector = IntroOutroDetector(store)
        active = 0
        maximum = 0
        active_lock = threading.Lock()

        def fingerprint(path, start, duration, should_terminate):
            nonlocal active, maximum
            with active_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with active_lock:
                active -= 1
            return b"\0\0\0\0"

        detector._fingerprint = fingerprint
        detector.run("run", JobStore())
        self.assertEqual(set(store.processed), {f"episode-{index}" for index in range(4)})
        self.assertEqual(len(store.processed), 4)
        self.assertEqual(maximum, 2)
