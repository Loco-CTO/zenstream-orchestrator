import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.media_probe import select_usable_video_stream, stream_index
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

    def test_claim_next_only_claims_playable_sources_with_a_video_codec(self):
        class Database:
            def __init__(self, root):
                self.db_file = str(root / "orchestrator.db")
                self.connection = sqlite3.connect(self.db_file)

            def execute(self, query, params=None):
                cursor = self.connection.execute(query, params or ())
                rows = cursor.fetchall()
                self.connection.commit()
                return rows

            @contextmanager
            def transaction(self):
                cursor = self.connection.cursor()
                try:
                    yield cursor
                    self.connection.commit()
                except Exception:
                    self.connection.rollback()
                    raise
                finally:
                    cursor.close()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = Database(root)
            db.connection.executescript(
                """
                CREATE TABLE media_files (
                    id TEXT PRIMARY KEY, entity_id TEXT, relative_path TEXT, role TEXT
                );
                CREATE TABLE library_entities (id TEXT PRIMARY KEY, library_id TEXT);
                CREATE TABLE libraries (id TEXT PRIMARY KEY, directory TEXT);
                CREATE TABLE media_sources (
                    id TEXT PRIMARY KEY, media_file_id TEXT,
                    duration_seconds REAL, video_codec TEXT
                );
                CREATE TABLE trickplay_assets (
                    media_file_id TEXT PRIMARY KEY, entity_id TEXT,
                    source_fingerprint TEXT, frame_width INTEGER, frame_height INTEGER,
                    interval_seconds INTEGER, state TEXT, error TEXT, updated_at TEXT
                );
                """
            )
            db.connection.execute("INSERT INTO libraries VALUES(?,?)", ("library", "D:/media"))
            candidates = [
                ("valid", "media", "h264", "1"),
                ("no-codec", "media", "", "2"),
                ("non-playable", "image", "h264", "3"),
            ]
            for media_file_id, role, video_codec, updated_at in candidates:
                entity_id = f"entity-{media_file_id}"
                db.connection.execute(
                    "INSERT INTO library_entities VALUES(?,?)",
                    (entity_id, "library"),
                )
                db.connection.execute(
                    "INSERT INTO media_files VALUES(?,?,?,?)",
                    (media_file_id, entity_id, f"{media_file_id}.mkv", role),
                )
                db.connection.execute(
                    "INSERT INTO media_sources VALUES(?,?,?,?)",
                    (f"source-{media_file_id}", media_file_id, 10, video_codec),
                )
                db.connection.execute(
                    "INSERT INTO trickplay_assets VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        media_file_id,
                        entity_id,
                        "fingerprint",
                        320,
                        180,
                        10,
                        "queued",
                        None,
                        updated_at,
                    ),
                )
            db.connection.commit()

            store = TrickplayStore(db)
            claimed = store.claim_next()

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["mediaFileId"], "valid")
            self.assertIsNone(store.claim_next())
            states = dict(db.execute("SELECT media_file_id,state FROM trickplay_assets"))
            self.assertEqual(states["valid"], "generating")
            self.assertEqual(states["no-codec"], "skipped")
            self.assertEqual(states["non-playable"], "queued")
            db.connection.close()

    def test_video_stream_selection_ignores_attached_and_zero_duration_video(self):
        streams = [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "width": 640,
                "height": 360,
                "disposition": {"attached_pic": 0},
                "tags": {
                    "FILENAME": "cover.jpg",
                    "MIMETYPE": "image/jpeg",
                    "DURATION": "00:00:00.021000000",
                },
            },
            {
                "index": 2,
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 1920,
                "height": 1080,
                "duration": "1200",
                "disposition": {"attached_pic": 0},
            },
        ]

        selected = select_usable_video_stream(streams, 1200)

        self.assertIsNotNone(selected)
        self.assertEqual(stream_index(selected), 2)
        self.assertIsNone(
            select_usable_video_stream(streams[:1], 1200)
        )

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
            "fps=1/10,format=yuv420p,"
            "scale=320:180:force_original_aspect_ratio=decrease,"
            "setsar=1,pad=320:180:(ow-iw)/2:(oh-ih)/2:black,"
            "tpad=stop_mode=clone:stop_duration=1000,"
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

        hdr = TrickplayExtractor.command(
            {
                "path": Path("movie.mkv"),
                "width": 320,
                "height": 180,
                "intervalSeconds": 10,
                "durationSeconds": 1001,
                "videoStreamIndex": 3,
                "videoColorSpace": "bt2020c",
                "videoColorTransfer": "bt2020-10",
                "videoColorPrimaries": "bt2020",
            },
            Path("sheet-%05d.webp"),
        )
        hdr_graph = hdr[hdr.index("-vf") + 1]
        self.assertIn("zscale=matrixin=bt2020c", hdr_graph)
        self.assertIn("format=yuv420p", hdr_graph)
        self.assertIn("tpad=stop_mode=clone", hdr_graph)
        self.assertEqual(hdr[hdr.index("-map") + 1], "0:3")
        self.assertEqual(hdr[hdr.index("-frames:v") + 1], "2")

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

            @staticmethod
            def execute(query, params=None):
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

            @staticmethod
            def execute(query, params=None):
                return []

        class Store:
            def __init__(self):
                self.db = Database()
                self.lock = threading.Lock()
                self.assets = [{"mediaFileId": f"media-{index}"} for index in range(4)]
                self.processed = []

            @staticmethod
            def recover_generating():
                return 0

            def queue_pending(self, settings=None):
                return len(self.assets)

            def claim_next(self):
                with self.lock:
                    return self.assets.pop(0) if self.assets else None

            @staticmethod
            def mark_failed(asset, error):
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

    def test_extraction_progress_expands_for_assets_claimed_after_initial_snapshot(self):
        class Database:
            db_file = "orchestrator.db"

            @staticmethod
            def execute(query, params=None):
                return []

        class Store:
            def __init__(self):
                self.db = Database()
                self.lock = threading.Lock()
                self.assets = [
                    {
                        "mediaFileId": f"media-{index}",
                        "entityId": f"entity-{index}",
                    }
                    for index in range(2)
                ]

            @staticmethod
            def recover_generating():
                return 0

            def queue_pending(self, settings=None):
                return 1

            def claim_next(self):
                with self.lock:
                    return self.assets.pop(0) if self.assets else None

            @staticmethod
            def mark_failed(asset, error):
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
        extractor.extract = lambda asset: None

        with patch(
            "app.trickplay.PlaybackSettings.get", return_value={"trickplayWorkers": 1}
        ):
            extractor.run("run", job_store)

        progress_updates = [
            values
            for _, values in job_store.updates
            if values.get("progress_stage_unit") == "videos"
            and values.get("progress_stage_total") is not None
        ]
        self.assertTrue(progress_updates)
        self.assertTrue(
            all(
                values["progress_stage_current"]
                <= values["progress_stage_total"]
                for values in progress_updates
            )
        )
        self.assertEqual(progress_updates[-1]["progress_stage_current"], 2)
        self.assertEqual(progress_updates[-1]["progress_stage_total"], 2)
