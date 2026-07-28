import tempfile
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
            Path("sheet-%05d.jpg"),
        )
        graph = command[command.index("-vf") + 1]
        self.assertEqual(
            graph,
            "fps=1/10,scale=320:180:force_original_aspect_ratio=decrease,"
            "setsar=1,pad=320:180:(ow-iw)/2:(oh-ih)/2:black,"
            "tile=10x10:padding=0:margin=0",
        )
        self.assertEqual(FRAMES_PER_SHEET, 100)

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
