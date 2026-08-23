import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.intro_outro import (
    DEFAULTS,
    SAMPLE_SECONDS,
    EmptyFingerprint,
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
    @staticmethod
    def _comparison_database():
        class Database:
            def __init__(self):
                self.connection = sqlite3.connect(":memory:")

            def execute(self, query, params=None):
                cursor = self.connection.execute(query, params or ())
                rows = cursor.fetchall()
                self.connection.commit()
                return rows

            @contextmanager
            def transaction(self):
                cursor = self.connection.cursor()
                try:
                    cursor.execute("BEGIN IMMEDIATE")
                    yield cursor
                    self.connection.commit()
                except Exception:
                    self.connection.rollback()
                    raise
                finally:
                    cursor.close()

        db = Database()
        db.connection.executescript(
            """
            CREATE TABLE media_sources (
                media_file_id TEXT PRIMARY KEY, duration_seconds REAL
            );
            CREATE TABLE intro_outro_assets (
                media_file_id TEXT PRIMARY KEY, season_id TEXT,
                source_fingerprint TEXT, intro_fingerprint BLOB,
                outro_fingerprint BLOB, state TEXT, error TEXT
            );
            CREATE TABLE intro_outro_segments (
                media_file_id TEXT, segment_type TEXT,
                start_seconds REAL, end_seconds REAL,
                PRIMARY KEY(media_file_id, segment_type)
            );
            CREATE TABLE intro_outro_comparison_state (
                season_id TEXT PRIMARY KEY, comparison_key TEXT, updated_at TEXT
            );
            """
        )
        return db

    @staticmethod
    def _add_comparison_asset(
        db,
        media_file_id,
        season_id,
        source_fingerprint=None,
        duration=600,
        intro=b"\0\0\0\0",
        outro=b"\0\0\0\0",
        state="scanned",
        error=None,
    ):
        db.connection.execute(
            "INSERT INTO media_sources(media_file_id,duration_seconds) VALUES(?,?)",
            (media_file_id, duration),
        )
        db.connection.execute(
            "INSERT INTO intro_outro_assets(media_file_id,season_id,source_fingerprint,"
            "intro_fingerprint,outro_fingerprint,state,error) VALUES(?,?,?,?,?,?,?)",
            (
                media_file_id,
                season_id,
                source_fingerprint or f"source-{media_file_id}",
                intro,
                outro,
                state,
                error,
            ),
        )
        db.connection.commit()

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
                ("partial", "season-a", "ep-partial", "fp-partial", "scanned"),
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
                        "INSERT INTO intro_outro_assets(media_file_id,entity_id,season_id,source_fingerprint,analysis_key,state,error,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            label,
                            entity_id,
                            season_id,
                            stored_fingerprint,
                            current_key,
                            state,
                            "outro: no audio" if label == "partial" else None,
                            "created",
                            "updated",
                        ),
                    )
            db.connection.commit()

            queued = IntroOutroStore(db).queue_pending(settings=settings)

            self.assertEqual(queued, 5)
            states = dict(
                db.execute("SELECT media_file_id,state FROM intro_outro_assets")
            )
            self.assertEqual(states["scanned"], "scanned")
            self.assertEqual(states["failed"], "queued")
            self.assertEqual(states["queued"], "queued")
            self.assertEqual(states["missing"], "queued")
            self.assertEqual(states["stale"], "queued")
            self.assertEqual(states["partial"], "queued")
            db.connection.close()

    def test_failed_asset_drops_stale_detected_segments(self):
        class Database:
            def __init__(self):
                self.connection = sqlite3.connect(":memory:")

            def execute(self, query, params=None):
                cursor = self.connection.execute(query, params or ())
                rows = cursor.fetchall()
                self.connection.commit()
                return rows

        db = Database()
        db.connection.executescript(
            """
            CREATE TABLE intro_outro_assets (
                media_file_id TEXT PRIMARY KEY, source_fingerprint TEXT,
                state TEXT, error TEXT, updated_at TEXT
            );
            CREATE TABLE intro_outro_segments (
                media_file_id TEXT, segment_type TEXT,
                start_seconds REAL, end_seconds REAL
            );
            INSERT INTO intro_outro_assets VALUES
                ('episode-1', 'source-1', 'generating', NULL, 'created');
            INSERT INTO intro_outro_segments VALUES
                ('episode-1', 'intro', 0, 30);
            """
        )

        IntroOutroStore(db).mark_failed(
            {"mediaFileId": "episode-1", "sourceFingerprint": "source-1"},
            "no audio window",
        )

        self.assertEqual(
            db.execute("SELECT state FROM intro_outro_assets"), [("failed",)]
        )
        self.assertEqual(db.execute("SELECT * FROM intro_outro_segments"), [])
        db.connection.close()

    def test_recompute_clears_segments_when_a_season_has_no_comparison_peer(self):
        class Database:
            def __init__(self):
                self.connection = sqlite3.connect(":memory:")

            def execute(self, query, params=None):
                cursor = self.connection.execute(query, params or ())
                rows = cursor.fetchall()
                self.connection.commit()
                return rows

            @contextmanager
            def transaction(self):
                cursor = self.connection.cursor()
                try:
                    cursor.execute("BEGIN IMMEDIATE")
                    yield cursor
                    self.connection.commit()
                except Exception:
                    self.connection.rollback()
                    raise
                finally:
                    cursor.close()

        db = Database()
        db.connection.executescript(
            """
            CREATE TABLE intro_outro_assets (
                media_file_id TEXT PRIMARY KEY, season_id TEXT,
                source_fingerprint TEXT, intro_fingerprint BLOB,
                outro_fingerprint BLOB, state TEXT, error TEXT
            );
            CREATE TABLE media_sources (
                media_file_id TEXT PRIMARY KEY, duration_seconds REAL
            );
            CREATE TABLE intro_outro_comparison_state (
                season_id TEXT PRIMARY KEY, comparison_key TEXT, updated_at TEXT
            );
            CREATE TABLE intro_outro_segments (
                media_file_id TEXT, segment_type TEXT,
                start_seconds REAL, end_seconds REAL
            );
            INSERT INTO media_sources VALUES ('episode-1', 600);
            INSERT INTO intro_outro_assets(
                media_file_id,season_id,source_fingerprint,intro_fingerprint,
                outro_fingerprint,state,error
            ) VALUES
                ('episode-1', 'season-1', 'source-1', X'01000000', X'02000000', 'scanned', NULL);
            INSERT INTO intro_outro_segments VALUES
                ('episode-1', 'intro', 0, 30);
            """
        )

        result = IntroOutroStore(db).recompute_season("season-1", DEFAULTS)
        self.assertEqual(result, 0)
        self.assertEqual(db.execute("SELECT * FROM intro_outro_segments"), [])
        db.connection.close()

    def test_bootstrap_comparison_skips_pairwise_matching_and_segment_writes_when_unchanged(
        self,
    ):
        db = self._comparison_database()
        self._add_comparison_asset(db, "episode-1", "season-1")
        self._add_comparison_asset(db, "episode-2", "season-1")
        store = IntroOutroStore(db)

        with patch(
            "app.intro_outro.shared_region",
            return_value=(0.0, 30.0, 0.0, 30.0),
        ):
            self.assertEqual(store.recompute_season("season-1", DEFAULTS), 4)

        before_segments = db.execute(
            "SELECT media_file_id,segment_type,start_seconds,end_seconds "
            "FROM intro_outro_segments ORDER BY media_file_id,segment_type"
        )
        before_state = db.execute(
            "SELECT comparison_key,updated_at FROM intro_outro_comparison_state "
            "WHERE season_id='season-1'"
        )
        statements = []
        db.connection.set_trace_callback(statements.append)
        with patch(
            "app.intro_outro.shared_region",
            side_effect=AssertionError("unchanged seasons must not compare pairs"),
        ):
            self.assertEqual(store.recompute_season("season-1", DEFAULTS), 4)
        db.connection.set_trace_callback(None)

        self.assertEqual(
            db.execute(
                "SELECT media_file_id,segment_type,start_seconds,end_seconds "
                "FROM intro_outro_segments ORDER BY media_file_id,segment_type"
            ),
            before_segments,
        )
        self.assertEqual(
            db.execute(
                "SELECT comparison_key,updated_at FROM intro_outro_comparison_state "
                "WHERE season_id='season-1'"
            ),
            before_state,
        )
        self.assertFalse(
            any(
                statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                and "INTRO_OUTRO_SEGMENTS" in statement.upper()
                for statement in statements
            )
        )

    def test_new_episode_recomputes_only_its_season(self):
        db = self._comparison_database()
        for season_id in ("season-a", "season-b"):
            self._add_comparison_asset(db, f"{season_id}-episode-1", season_id)
            self._add_comparison_asset(db, f"{season_id}-episode-2", season_id)
        store = IntroOutroStore(db)
        with patch(
            "app.intro_outro.shared_region",
            return_value=(0.0, 30.0, 0.0, 30.0),
        ):
            store.recompute_all(DEFAULTS)

        self._add_comparison_asset(db, "season-a-episode-3", "season-a")
        with patch(
            "app.intro_outro.shared_region",
            return_value=(0.0, 30.0, 0.0, 30.0),
        ) as matcher:
            store.recompute_all(DEFAULTS)

        self.assertEqual(matcher.call_count, 6)
        self.assertEqual(
            db.execute(
                "SELECT COUNT(*) FROM intro_outro_segments WHERE media_file_id LIKE 'season-b-%'"
            )[0][0],
            4,
        )

    def test_adding_a_peer_backfills_a_changed_episode(self):
        db = self._comparison_database()
        self._add_comparison_asset(
            db, "episode-13", "season-1", source_fingerprint="source-13-old"
        )
        store = IntroOutroStore(db)
        self.assertEqual(store.recompute_season("season-1", DEFAULTS), 0)

        db.connection.execute(
            "UPDATE intro_outro_assets SET source_fingerprint=?,intro_fingerprint=?,"
            "outro_fingerprint=? WHERE media_file_id='episode-13'",
            ("source-13-new", b"new-intro", b"new-outro"),
        )
        db.connection.commit()
        with patch(
            "app.intro_outro.shared_region",
            return_value=None,
        ):
            self.assertEqual(store.recompute_season("season-1", DEFAULTS), 0)
        self.assertEqual(db.execute("SELECT * FROM intro_outro_segments"), [])

        self._add_comparison_asset(
            db,
            "episode-14",
            "season-1",
            source_fingerprint="source-14",
            intro=b"new-intro",
            outro=b"new-outro",
        )
        with patch(
            "app.intro_outro.shared_region",
            return_value=(0.0, 30.0, 0.0, 30.0),
        ):
            self.assertEqual(store.recompute_season("season-1", DEFAULTS), 4)
        self.assertEqual(
            {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT media_file_id FROM intro_outro_segments"
                )
            },
            {"episode-13", "episode-14"},
        )

    def test_comparison_key_changes_for_duration_fingerprint_warning_and_removal(self):
        db = self._comparison_database()
        self._add_comparison_asset(db, "episode-1", "season-1")
        self._add_comparison_asset(db, "episode-2", "season-1")
        store = IntroOutroStore(db)
        with patch(
            "app.intro_outro.shared_region",
            return_value=(0.0, 30.0, 0.0, 30.0),
        ):
            store.recompute_season("season-1", DEFAULTS)

        keys = []
        keys.append(
            db.execute(
                "SELECT comparison_key FROM intro_outro_comparison_state WHERE season_id='season-1'"
            )[0][0]
        )
        db.connection.execute(
            "UPDATE media_sources SET duration_seconds=601 WHERE media_file_id='episode-1'"
        )
        db.connection.commit()
        with patch(
            "app.intro_outro.shared_region",
            return_value=(0.0, 30.0, 0.0, 30.0),
        ):
            store.recompute_season("season-1", DEFAULTS)
        keys.append(
            db.execute(
                "SELECT comparison_key FROM intro_outro_comparison_state WHERE season_id='season-1'"
            )[0][0]
        )

        db.connection.execute(
            "UPDATE intro_outro_assets SET intro_fingerprint=?,error=? "
            "WHERE media_file_id='episode-1'",
            (b"changed", "intro: temporary failure"),
        )
        db.connection.commit()
        with patch(
            "app.intro_outro.shared_region",
            return_value=(0.0, 30.0, 0.0, 30.0),
        ):
            store.recompute_season("season-1", DEFAULTS)
        keys.append(
            db.execute(
                "SELECT comparison_key FROM intro_outro_comparison_state WHERE season_id='season-1'"
            )[0][0]
        )

        db.connection.execute(
            "UPDATE intro_outro_assets SET state='failed' WHERE media_file_id='episode-2'"
        )
        db.connection.commit()
        store.recompute_season("season-1", DEFAULTS)
        keys.append(
            db.execute(
                "SELECT comparison_key FROM intro_outro_comparison_state WHERE season_id='season-1'"
            )[0][0]
        )
        self.assertEqual(len(set(keys)), len(keys))

    @patch("app.intro_outro.ffmpeg_path", return_value="ffmpeg")
    def test_fingerprint_command_uses_raw_chromaprint(self, _ffmpeg):
        command = IntroOutroDetector.fingerprint_command(Path("episode.mkv"), 0, 600)
        self.assertEqual(command[command.index("-f") + 1], "chromaprint")
        self.assertEqual(command[command.index("-fp_format") + 1], "raw")
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertEqual(command[command.index("-threads") + 1], "4")
        self.assertIn("-nostdin", command)

        automatic = IntroOutroDetector.fingerprint_command(
            Path("episode.mkv"), 0, 600, 0
        )
        self.assertEqual(automatic[automatic.index("-threads") + 1], "0")

    def test_decodes_little_endian_fingerprint_points(self):
        self.assertEqual(decode_fingerprint(struct.pack("<3I", 1, 2, 3)), (1, 2, 3))
        self.assertEqual(decode_fingerprint(b"bad"), ())

    @patch("app.intro_outro.run_ffmpeg", return_value=b"")
    @patch("app.intro_outro.ffmpeg_path", return_value="ffmpeg")
    def test_empty_ffmpeg_output_is_a_window_level_fingerprint_warning(
        self, _ffmpeg, _run_ffmpeg
    ):
        with tempfile.NamedTemporaryFile() as temporary, self.assertRaises(EmptyFingerprint):
            IntroOutroDetector()._fingerprint(
                Path(temporary.name), 0, 30, lambda: False
            )

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
        self.assertEqual(
            normalize_settings({"introOutroFfmpegThreads": 0})[
                "introOutroFfmpegThreads"
            ],
            0,
        )
        self.assertEqual(
            normalize_settings({"introOutroFfmpegThreads": 64})[
                "introOutroFfmpegThreads"
            ],
            64,
        )
        with self.assertRaisesRegex(
            ValueError, "introOutroFfmpegThreads must be between 0 and 64"
        ):
            normalize_settings({"introOutroFfmpegThreads": -1})
        with self.assertRaisesRegex(
            ValueError, "introOutroFfmpegThreads must be between 0 and 64"
        ):
            normalize_settings({"introOutroFfmpegThreads": 65})

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

            def mark_fingerprinted(self, asset, intro, outro, warning=None):
                with self.lock:
                    self.processed.append(asset["mediaFileId"])

            def mark_failed(self, asset, error):
                raise AssertionError(error)

            def recompute_all(self, settings, progress=None):
                return 0

        class JobStore:
            def __init__(self):
                self.updates = []

            def update_run(self, run_id, **values):
                self.updates.append((run_id, values))

        store = Store()
        job_store = JobStore()
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
        detector.run("run", job_store)
        self.assertEqual(
            set(store.processed), {f"episode-{index}" for index in range(4)}
        )
        self.assertEqual(len(store.processed), 4)
        self.assertEqual(maximum, 2)
        self.assertTrue(job_store.updates)
        self.assertTrue(all(run_id == "run" for run_id, _ in job_store.updates))
        self.assertIn(
            "fingerprinting",
            {values.get("progress_phase") for _, values in job_store.updates},
        )
        self.assertIn(
            "completed", {values.get("state") for _, values in job_store.updates}
        )

    def test_partial_window_fingerprints_are_kept_and_comparison_continues(self):
        class Store:
            def __init__(self):
                self.asset = {
                    "mediaFileId": "episode-1",
                    "entityId": "entity-1",
                    "durationSeconds": 600,
                    "path": Path("episode-1.mkv"),
                }
                self.processed = []
                self.comparisons = 0

            def settings(self):
                return {**DEFAULTS, "introOutroWorkers": 1}

            def queue_pending(self, settings=None):
                return 1

            def claim_next(self):
                asset, self.asset = self.asset, None
                return asset

            def mark_fingerprinted(self, asset, intro, outro, warning=None):
                self.processed.append((intro, outro, warning))

            def mark_failed(self, asset, error):
                raise AssertionError(error)

            def recompute_all(self, settings, progress=None):
                self.comparisons += 1
                return 2

        class JobStore:
            def __init__(self):
                self.updates = []

            def update_run(self, run_id, **values):
                self.updates.append((run_id, values))

        store = Store()
        detector = IntroOutroDetector(store)

        def fingerprint(path, start, duration, should_terminate):
            if start > 0:
                raise EmptyFingerprint(
                    "FFmpeg did not return a raw Chromaprint fingerprint."
                )
            return b"intro"

        detector._fingerprint = fingerprint
        job_store = JobStore()
        detector.run("run", job_store)

        intro, outro, warning = store.processed[0]
        self.assertEqual(intro, b"intro")
        self.assertIsNone(outro)
        self.assertIn("outro:", warning)
        self.assertEqual(store.comparisons, 1)
        terminal = [
            values for _, values in job_store.updates if values.get("finished_at")
        ]
        self.assertEqual(terminal[-1]["state"], "completed_with_warnings")
        self.assertIn("1 partial", terminal[-1]["message"])

    def test_fingerprint_failures_do_not_skip_existing_fingerprint_comparison(self):
        class Store:
            def __init__(self):
                self.asset = {
                    "mediaFileId": "episode-1",
                    "entityId": "entity-1",
                    "durationSeconds": 600,
                    "path": Path("episode-1.mkv"),
                }
                self.failures = []
                self.comparisons = 0

            def settings(self):
                return {**DEFAULTS, "introOutroWorkers": 1}

            def queue_pending(self, settings=None):
                return 1

            def claim_next(self):
                asset, self.asset = self.asset, None
                return asset

            def mark_fingerprinted(self, asset, intro, outro, warning=None):
                raise AssertionError("a fully failed episode must not be scanned")

            def mark_failed(self, asset, error):
                self.failures.append((asset["mediaFileId"], error))

            def recompute_all(self, settings, progress=None):
                self.comparisons += 1
                return 3

        class JobStore:
            def __init__(self):
                self.updates = []

            def update_run(self, run_id, **values):
                self.updates.append((run_id, values))

        store = Store()
        job_store = JobStore()
        detector = IntroOutroDetector(store)
        detector._fingerprint = lambda *args: (_ for _ in ()).throw(
            EmptyFingerprint("no audio window")
        )

        detector.run("run", job_store)

        self.assertEqual(len(store.failures), 1)
        self.assertEqual(store.comparisons, 1)
        terminal = [
            values for _, values in job_store.updates if values.get("finished_at")
        ]
        self.assertEqual(terminal[-1]["state"], "completed_with_warnings")
        self.assertIn("3 intro/outro markers", terminal[-1]["message"])
        self.assertIn("1 failed", terminal[-1]["message"])
