import tempfile
import threading
import unittest
from pathlib import Path

from app.database import DatabaseHandler
from app.library import LibraryRuntime, LibraryStore, guess_media, provider_ids
from app.providers import choose_image


class LibraryMetadataTest(unittest.TestCase):
    def test_jellyfin_style_provider_ids_are_extracted(self):
        self.assertEqual(
            provider_ids("The Matrix (1999) [tmdbid-603] [tvdbid-Movie-123]"),
            [("tmdb", "movie", "603"), ("tvdb", "series", "Movie-123")],
        )

    def test_guessit_fallback_reads_season_and_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Show - S02E03 - Episode.mkv"
            path.touch()
            parsed = guess_media(path)
            self.assertEqual(int(parsed["season"]), 2)
            self.assertEqual(int(parsed["episode"]), 3)

    def test_image_fallback_order_is_requested_no_language_english_any(self):
        images = [
            {"type": "poster", "language": "fr", "url": "fr"},
            {"type": "poster", "language": "en", "url": "en"},
            {"type": "poster", "language": None, "url": "neutral"},
            {"type": "poster", "language": "ja", "url": "ja"},
        ]
        self.assertEqual(choose_image(images, "ja-JP", "poster")["url"], "ja")
        self.assertEqual(choose_image(images, "de-DE", "poster")["url"], "neutral")


class LibraryJobControlTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute("CREATE TABLE library_jobs (id TEXT PRIMARY KEY, library_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)")
        store = LibraryStore.__new__(LibraryStore)
        store.db = self.db
        self.runtime = LibraryRuntime.__new__(LibraryRuntime)
        self.runtime.store = store
        self.runtime.condition = threading.Condition()
        self.runtime._active_lock = threading.RLock()
        self.runtime._cancel_events = {}

    def tearDown(self):
        self.db.close()

    def test_scan_and_reconcile_share_one_active_library_task(self):
        scan = self.runtime.enqueue("library-1", "scan")
        reconcile = self.runtime.enqueue("library-1", "reconcile")

        self.assertEqual(scan["id"], reconcile["id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM library_jobs")[0][0], 1)

    def test_queued_library_task_can_be_terminated(self):
        job = self.runtime.enqueue("library-1", "scan")

        terminated = self.runtime.terminate(job["id"])

        self.assertEqual(terminated["state"], "terminated")
        self.assertIsNotNone(terminated["finishedAt"])


if __name__ == "__main__":
    unittest.main()
