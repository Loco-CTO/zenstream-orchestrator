import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.trickplay import FRAMES_PER_SHEET, TrickplayExtractor, TrickplayStore


class TrickplayTest(unittest.TestCase):
    @patch("app.trickplay.ffmpeg_path", return_value="ffmpeg")
    def test_command_letterboxes_every_frame_before_tiling(self, _ffmpeg):
        command = TrickplayExtractor.command(
            {
                "path": Path("movie.mkv"),
                "width": 320,
                "height": 180,
                "intervalSeconds": 10,
            },
            Path("sheet-%05d.webp"),
        )
        graph = command[command.index("-vf") + 1]
        self.assertEqual(
            graph,
            "fps=1/10,scale=320:180:force_original_aspect_ratio=decrease,"
            "setsar=1,pad=320:180:(ow-iw)/2:(oh-ih)/2:black,"
            "tile=10x10:padding=0:margin=0",
        )
        self.assertEqual(FRAMES_PER_SHEET, 100)
        self.assertEqual(command[command.index("-c:v") + 1], "libwebp")
        self.assertEqual(command[command.index("-quality") + 1], "85")
        self.assertEqual(command[command.index("-threads") + 1], "1")

    def test_output_keys_change_for_source_or_extraction_settings(self):
        self.assertNotEqual(
            TrickplayStore.output_key("source-a", 320, 180, 10),
            TrickplayStore.output_key("source-b", 320, 180, 10),
        )
        self.assertNotEqual(
            TrickplayStore.output_key("source-a", 320, 180, 10),
            TrickplayStore.output_key("source-a", 320, 180, 20),
        )

    def test_orphan_cache_cleanup_stays_inside_cache_root(self):
        class Database:
            def __init__(self, root):
                self.db_file = str(root / "orchestrator.db")

            def execute(self, query, params=None):
                return [("present",)]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "trickplay-cache"
            (cache / "present").mkdir(parents=True)
            (cache / "missing").mkdir()
            extractor = object.__new__(TrickplayExtractor)
            extractor.db = Database(root)
            extractor.store = None
            extractor.remove_orphan_cache()
            self.assertTrue((cache / "present").is_dir())
            self.assertFalse((cache / "missing").exists())

    def test_extraction_uses_configured_workers_and_claims_each_asset_once(self):
        class Database:
            db_file = "orchestrator.db"

            def execute(self, query, params=None):
                return []

        class Store:
            def __init__(self):
                self.db = Database()
                self.lock = threading.Lock()
                self.assets = [{"mediaFileId": f"media-{index}"} for index in range(4)]
                self.processed = []

            def recover_generating(self):
                return 0

            def queue_pending(self, settings=None):
                return len(self.assets)

            def claim_next(self):
                with self.lock:
                    return self.assets.pop(0) if self.assets else None

            def mark_failed(self, asset, error):
                raise AssertionError(error)

        class JobStore:
            def update_run(self, *args, **kwargs):
                return None

        store = Store()
        extractor = TrickplayExtractor(store)
        extractor.remove_orphan_cache = lambda: None
        active = 0
        maximum = 0
        active_lock = threading.Lock()

        def extract(asset):
            nonlocal active, maximum
            with active_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with active_lock:
                active -= 1
            with store.lock:
                store.processed.append(asset["mediaFileId"])

        extractor.extract = extract
        with patch("app.trickplay.PlaybackSettings.get", return_value={"trickplayWorkers": 2}):
            extractor.run("run", JobStore())
        self.assertEqual(set(store.processed), {f"media-{index}" for index in range(4)})
        self.assertEqual(len(store.processed), 4)
        self.assertEqual(maximum, 2)
