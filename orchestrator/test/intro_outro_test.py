import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.intro_outro import (
    DEFAULTS,
    SAMPLE_SECONDS,
    IntroOutroDetector,
    IntroOutroStore,
    analysis_key,
    audio_preview_command,
    decode_fingerprint,
    fingerprint_preview,
    normalize_settings,
    shared_region,
)


class IntroOutroTest(unittest.TestCase):
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
            db = Database(Path(temporary))
            db.connection.executescript(
                """
                CREATE TABLE media_files (
                    id TEXT PRIMARY KEY, entity_id TEXT, quick_fingerprint TEXT,
                    size INTEGER, modified_ns INTEGER, role TEXT
                );
                CREATE TABLE media_sources (
                    media_file_id TEXT PRIMARY KEY, audio_codec TEXT
                );
                CREATE TABLE library_entities (
                    id TEXT PRIMARY KEY, parent_id TEXT, entity_type TEXT,
                    season_number INTEGER, library_id TEXT
                );
                CREATE TABLE libraries (id TEXT PRIMARY KEY, type TEXT);
                CREATE TABLE intro_outro_settings (
                    id INTEGER PRIMARY KEY, scan_on_added INTEGER, analysis_percent INTEGER,
                    analysis_length_limit_minutes INTEGER, scan_introduction INTEGER,
                    scan_credits INTEGER, maximum_credits_analysis_seconds INTEGER,
                    minimum_intro_duration INTEGER, maximum_intro_duration INTEGER,
                    minimum_credits_duration INTEGER, maximum_fingerprint_point_differences INTEGER,
                    maximum_time_skip_seconds REAL, inverted_index_shift INTEGER,
                    intro_outro_workers INTEGER
                );
                CREATE TABLE intro_outro_assets (
                    media_file_id TEXT PRIMARY KEY, entity_id TEXT, season_id TEXT,
                    source_fingerprint TEXT, analysis_key TEXT, state TEXT,
                    intro_fingerprint BLOB, outro_fingerprint BLOB, error TEXT,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE intro_outro_segments (
                    media_file_id TEXT, segment_type TEXT, start_seconds REAL, end_seconds REAL
                );
                """
            )
            settings = dict(DEFAULTS)
            current_key = analysis_key(settings)
            db.connection.execute("INSERT INTO libraries VALUES('lib-a','tv_series')")
            db.connection.execute("INSERT INTO libraries VALUES('lib-b','tv_series')")
            for library_id, series_id, season_id in (
                ("lib-a", "series-a", "season-a"),
                ("lib-b", "series-b", "season-b"),
            ):
                db.connection.execute(
                    "INSERT INTO library_entities VALUES(?,?,?,?,?)",
                    (series_id, None, "series", None, library_id),
                )
                db.connection.execute(
                    "INSERT INTO library_entities VALUES(?,?,?,?,?)",
                    (season_id, series_id, "season", 1, library_id),
                )
            episodes = (
                ("scanned", "season-a", "ep-scanned", "fp-scanned", "scanned"),
                ("failed", "season-b", "ep-failed", "fp-failed", "failed"),
                ("queued", "season-a", "ep-queued", "fp-queued", "queued"),
                ("missing", "season-b", "ep-missing", "fp-missing", None),
                ("stale", "season-a", "ep-stale", "fp-new", "scanned"),
            )
            for label, season_id, entity_id, fingerprint, state in episodes:
                library_id = "lib-a" if season_id == "season-a" else "lib-b"
                db.connection.execute(
                    "INSERT INTO library_entities VALUES(?,?,?,?,?)",
                    (entity_id, season_id, "episode", None, library_id),
                )
                db.connection.execute(
                    "INSERT INTO media_files VALUES(?,?,?,?,?,?)",
                    (label, entity_id, fingerprint, 100, 1, "media"),
                )
                db.connection.execute(
                    "INSERT INTO media_sources VALUES(?,?)", (label, "aac")
                )
                if state:
                    stored_fingerprint = "fp-old" if label == "stale" else fingerprint
                    db.connection.execute(
                        "INSERT INTO intro_outro_assets(media_file_id,entity_id,season_id,source_fingerprint,analysis_key,state,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            label,
                            entity_id,
                            season_id,
                            stored_fingerprint,
                            current_key,
                            state,
                            "created",
                            "updated",
                        ),
                    )
            db.connection.commit()

            queued = IntroOutroStore(db).queue_pending(settings=settings)

            self.assertEqual(queued, 4)
            states = dict(
                db.execute("SELECT media_file_id,state FROM intro_outro_assets")
            )
            self.assertEqual(states["scanned"], "scanned")
            self.assertEqual(states["failed"], "queued")
            self.assertEqual(states["queued"], "queued")
            self.assertEqual(states["missing"], "queued")
            self.assertEqual(states["stale"], "queued")
            db.connection.close()

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
        preview = fingerprint_preview(
            struct.pack("<4I", 0, 0xFFFFFFFF, 0, 0xFFFFFFFF), maximum_samples=2
        )
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
        shared = tuple(
            (1000 + index) * 0x9E3779B1 & 0xFFFFFFFF for index in range(points)
        )
        result = shared_region(
            (0xFFFFFFFF, 0xFFFFFFFE, *shared),
            (0xAAAA0000, 0xAAAA0001, 0xAAAA0002, *shared),
            DEFAULTS,
            15,
            120,
        )
        self.assertIsNotNone(result)
        left_start, left_end, right_start, right_end = result
        self.assertAlmostEqual(left_start, 2 * SAMPLE_SECONDS)
        self.assertGreaterEqual(left_end - left_start, DEFAULTS["minimumIntroDuration"])
        self.assertAlmostEqual(right_start, 3 * SAMPLE_SECONDS)
        self.assertAlmostEqual(right_end - right_start, left_end - left_start)

    def test_rejects_short_matches(self):
        self.assertIsNone(
            shared_region(tuple(range(30)), tuple(range(30)), DEFAULTS, 15, 120)
        )

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
                    {
                        "mediaFileId": f"episode-{index}",
                        "entityId": f"entity-{index}",
                        "durationSeconds": 600,
                        "path": Path(f"episode-{index}.mkv"),
                    }
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
        self.assertEqual(
            set(store.processed), {f"episode-{index}" for index in range(4)}
        )
        self.assertEqual(len(store.processed), 4)
        self.assertEqual(maximum, 2)
