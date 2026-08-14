import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.trickplay import FRAMES_PER_SHEET, TrickplayExtractor, TrickplayStore


class TrickplayTest(unittest.TestCase):
    def test_queue_pending_repairs_global_incomplete_backlog(self):
        class Database:
            def __init__(self, root):
                self.db_file = str(root / "orchestrator.db")
                self.connection = sqlite3.connect(self.db_file)

            def execute(self, query, params=None):
                cursor = self.connection.execute(query, params or ())
                rows = cursor.fetchall()
                self.connection.commit()
                return rows

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = Database(root)
            db.connection.executescript(
                """
                CREATE TABLE media_files (
                    id TEXT PRIMARY KEY, entity_id TEXT, quick_fingerprint TEXT,
                    size INTEGER, modified_ns INTEGER, role TEXT
                );
                CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT);
                CREATE TABLE media_sources (
                    media_file_id TEXT PRIMARY KEY, video_codec TEXT, duration_seconds REAL
                );
                CREATE TABLE trickplay_assets (
                    media_file_id TEXT PRIMARY KEY, entity_id TEXT, source_fingerprint TEXT,
                    frame_width INTEGER, frame_height INTEGER, interval_seconds INTEGER,
                    state TEXT, output_key TEXT, error TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE trickplay_sheets (
                    media_file_id TEXT, output_key TEXT, sheet_index INTEGER,
                    first_frame INTEGER, frame_count INTEGER, relative_path TEXT
                );
                """
            )
            settings = {
                "trickplayFrameWidth": 320,
                "trickplayFrameHeight": 180,
                "trickplayIntervalSeconds": 10,
            }
            media = [
                ("ready", "entity-ready", "fp-ready", "lib-a"),
                ("invalid", "entity-invalid", "fp-invalid", "lib-b"),
                ("queued", "entity-queued", "fp-queued", "lib-a"),
                ("failed", "entity-failed", "fp-failed", "lib-b"),
                ("generating", "entity-generating", "fp-generating", "lib-a"),
                ("missing", "entity-missing", "fp-missing", "lib-b"),
            ]
            for media_file_id, entity_id, fingerprint, library_id in media:
                db.connection.execute(
                    "INSERT INTO media_files VALUES(?,?,?,?,?,?)",
                    (media_file_id, entity_id, fingerprint, 100, 1, "media"),
                )
                db.connection.execute(
                    "INSERT INTO library_entities VALUES(?,?)", (entity_id, library_id)
                )
                db.connection.execute(
                    "INSERT INTO media_sources VALUES(?,?,?)",
                    (media_file_id, "h264", 10),
                )
            for state, entity_id, fingerprint, _library_id in media[:-1]:
                output_key = f"{state}-key" if state in {"ready", "invalid"} else None
                db.connection.execute(
                    "INSERT INTO trickplay_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        state,
                        entity_id,
                        fingerprint,
                        320,
                        180,
                        10,
                        state,
                        output_key,
                        "old failure" if state == "failed" else None,
                        "created",
                        "updated",
                    ),
                )
                if output_key:
                    relative_path = f"{state}/{output_key}/sheet-00000.webp"
                    db.connection.execute(
                        "INSERT INTO trickplay_sheets VALUES(?,?,?,?,?,?)",
                        (state, output_key, 0, 0, 1, relative_path),
                    )
                    if state == "ready":
                        path = root / "trickplay-cache" / relative_path
                        path.parent.mkdir(parents=True)
                        path.write_bytes(b"webp")
            db.connection.commit()

            queued = TrickplayStore(db).queue_pending(settings=settings)

            self.assertEqual(queued, 4)
            states = dict(
                db.execute("SELECT media_file_id,state FROM trickplay_assets")
            )
            self.assertEqual(states["ready"], "ready")
            self.assertEqual(states["invalid"], "queued")
            self.assertEqual(states["queued"], "queued")
            self.assertEqual(states["failed"], "queued")
            self.assertEqual(states["generating"], "generating")
            self.assertEqual(states["missing"], "queued")
            db.connection.close()

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
        self.assertEqual(command[command.index("-threads") + 1], "4")
        self.assertIn("-nostdin", command)
        self.assertEqual(command[command.index("-compression_level") + 1], "5")
        self.assertIn("-progress", command)
        automatic = TrickplayExtractor.command(
            {
                "path": Path("movie.mkv"),
                "width": 320,
                "height": 180,
                "intervalSeconds": 10,
            },
            Path("sheet-%05d.webp"),
            0,
        )
        self.assertEqual(automatic[automatic.index("-threads") + 1], "0")

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
            def __init__(self):
                self.updates = []

            def update_run(self, run_id, **values):
                self.updates.append((run_id, values))

        store = Store()
        job_store = JobStore()
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
        with patch(
            "app.trickplay.PlaybackSettings.get", return_value={"trickplayWorkers": 2}
        ):
            extractor.run("run", job_store)
        self.assertEqual(set(store.processed), {f"media-{index}" for index in range(4)})
        self.assertEqual(len(store.processed), 4)
        self.assertEqual(maximum, 2)
        self.assertTrue(job_store.updates)
        self.assertTrue(all(run_id == "run" for run_id, _ in job_store.updates))
        self.assertIn(
            "extraction",
            {values.get("progress_phase") for _, values in job_store.updates},
        )
        self.assertIn(
            "completed", {values.get("state") for _, values in job_store.updates}
        )
