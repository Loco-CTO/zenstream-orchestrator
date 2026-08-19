import asyncio
import os
import subprocess
import threading
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from api.zenstream.client_routes import _RATE_LIMIT_EVENTS, prune_rate_limit_events
from app.database import DatabaseHandler
from app.foreground import active_requests, run_foreground, wait_for_shutdown
from app.jobs import JobStore
from app.library import LibraryRuntime
from app.metadata_services import MetadataAssetExecutor
from app.models.playback_viewer import PlaybackViewerStore
from app.models.syncplay import SyncplayGroup
from app.playback import PlaybackManager
from app.resource_retention import _prune_subtitle_cache
from app.screen_extractor import ScreenExtractor


def _iso(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class ResourceRetentionTest(unittest.TestCase):
    def test_rate_limit_buckets_are_removed_after_idle_window(self):
        previous = dict(_RATE_LIMIT_EVENTS)
        try:
            _RATE_LIMIT_EVENTS.clear()
            _RATE_LIMIT_EVENTS[("login", "old")].extend([1.0])
            _RATE_LIMIT_EVENTS[("login", "fresh")].extend([999.0])

            prune_rate_limit_events(now=1000.0)

            self.assertNotIn(("login", "old"), _RATE_LIMIT_EVENTS)
            self.assertIn(("login", "fresh"), _RATE_LIMIT_EVENTS)
        finally:
            _RATE_LIMIT_EVENTS.clear()
            _RATE_LIMIT_EVENTS.update(previous)

    def test_metadata_asset_terminal_states_have_bounded_retention(self):
        executor = MetadataAssetExecutor(max_workers=1)
        key = ("tmdb", "movie", "1", "en", "digest")
        try:
            executor.submit(key, lambda: None)
            executor.drain(2)
            with executor._lock:
                executor._state_times[key] = time.monotonic() - executor.STATE_RETENTION_SECONDS - 1

            executor.prune()

            self.assertNotIn(key, executor._states)
            self.assertNotIn(key, executor._state_times)
        finally:
            executor.shutdown()

    def test_job_history_cleanup_keeps_recent_and_active_rows(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute(
                "CREATE TABLE job_runs(id TEXT PRIMARY KEY,state TEXT,finished_at TEXT)"
            )
            db.execute(
                "CREATE TABLE library_jobs(id TEXT PRIMARY KEY,state TEXT,finished_at TEXT)"
            )
            old = _iso(2)
            recent = _iso()
            for table in ("job_runs", "library_jobs"):
                db.execute(f"INSERT INTO {table} VALUES(?,?,?)", (f"{table}-old", "completed", old))
                db.execute(f"INSERT INTO {table} VALUES(?,?,?)", (f"{table}-recent", "completed", recent))
                db.execute(f"INSERT INTO {table} VALUES(?,?,?)", (f"{table}-active", "running", old))
            store = JobStore.__new__(JobStore)
            store.db = db

            result = store.cleanup_history(retention_days=1)

            self.assertEqual(result, {"job_runs": 1, "library_jobs": 1})
            for table in ("job_runs", "library_jobs"):
                self.assertEqual(
                    db.execute(f"SELECT id FROM {table} ORDER BY id"),
                    [(f"{table}-active",), (f"{table}-recent",)],
                )
        finally:
            db.close()

    def test_viewer_history_cleanup_removes_old_terminal_rows(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            for statement in (
                "CREATE TABLE user_sessions(id TEXT PRIMARY KEY,device_id TEXT)",
                "CREATE TABLE user_devices(id TEXT PRIMARY KEY,user_id TEXT,last_seen_at TEXT)",
                "CREATE TABLE playback_viewer_sessions(id TEXT PRIMARY KEY,state TEXT,ended_at TEXT,created_at TEXT,last_heartbeat_at TEXT,device_id TEXT)",
                "CREATE TABLE playback_viewer_commands(id TEXT PRIMARY KEY,viewer_session_id TEXT,state TEXT,acknowledged_at TEXT,expires_at TEXT,issued_at TEXT)",
            ):
                db.execute(statement)
            db.execute("INSERT INTO user_devices VALUES('device-old','user',?)", (_iso(2),))
            db.execute(
                "INSERT INTO playback_viewer_sessions VALUES('viewer-old','ended',?,?,?,?)",
                (_iso(2), _iso(2), _iso(2), "device-old"),
            )
            db.execute(
                "INSERT INTO playback_viewer_commands VALUES('command-old','viewer-old','acknowledged',?,?,?)",
                (_iso(2), _iso(2), _iso(2)),
            )
            store = PlaybackViewerStore(db)

            removed = store.cleanup_history(retention_days=1)

            self.assertEqual(removed, 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playback_viewer_sessions")[0][0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playback_viewer_commands")[0][0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM user_devices")[0][0], 0)
        finally:
            db.close()

    def test_syncplay_history_cleanup_removes_ended_groups_and_operations(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE syncplay_groups(id TEXT PRIMARY KEY,ended INTEGER,updated REAL)")
            db.execute("CREATE TABLE syncplay_members(group_id TEXT)")
            db.execute("CREATE TABLE syncplay_operations(operation_id TEXT PRIMARY KEY,group_id TEXT,user_id TEXT,state TEXT)")
            db.execute("INSERT INTO syncplay_groups VALUES('old',1,?)", (time.time() - 2 * 86400,))
            db.execute("INSERT INTO syncplay_members VALUES('old')")
            db.execute("INSERT INTO syncplay_operations VALUES('op','old','user','{}')")
            with patch("app.models.syncplay.Config") as config:
                config.return_value.database = db
                removed = SyncplayGroup.cleanup_history(retention_days=1)

            self.assertEqual(removed, 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM syncplay_groups")[0][0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM syncplay_operations")[0][0], 0)
        finally:
            db.close()

    def test_screen_extractor_kill_cleanup_has_a_second_deadline(self):
        process = MagicMock()
        process.terminate.side_effect = None
        process.kill.side_effect = None
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("ffmpeg", 10),
            subprocess.TimeoutExpired("ffmpeg", 10),
        ]
        process.stdout = MagicMock()
        process.stderr = MagicMock()

        started = time.monotonic()
        result = ScreenExtractor._stop(process)

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result[1], "FFmpeg process did not exit after termination.")
        self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual(process.communicate.call_args_list[1].kwargs["timeout"], 10)

    def test_old_subtitle_cache_files_are_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            db = MagicMock()
            db.db_file = f"{directory}/orchestrator.db"
            cache = Path(directory) / "subtitle-cache"
            cache.mkdir()
            old = cache / "old.vtt"
            fresh = cache / "fresh.vtt"
            old.write_text("old", encoding="utf-8")
            fresh.write_text("fresh", encoding="utf-8")
            os.utime(old, (time.time() - 2 * 86400, time.time() - 2 * 86400))

            removed = _prune_subtitle_cache(db, retention_days=1)

            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_old_playback_terminal_rows_release_output_before_delete(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute(
                "CREATE TABLE playback_sessions(id TEXT PRIMARY KEY,state TEXT,process_id INTEGER,expires_at TEXT,output_directory TEXT,completed_at TEXT,last_accessed_at TEXT,seek_generation INTEGER)"
            )
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "session"
                output.mkdir()
                (output / "segment.ts").write_text("segment", encoding="utf-8")
                db.execute(
                    "INSERT INTO playback_sessions VALUES('session','completed',NULL,?,?,?,?,?)",
                    (_iso(), str(output), _iso(8), _iso(8), 1),
                )
                manager = PlaybackManager.__new__(PlaybackManager)
                manager.db = db
                manager._lock = threading.RLock()

                manager._cleanup_expired()

                self.assertEqual(db.execute("SELECT COUNT(*) FROM playback_sessions")[0][0], 0)
                self.assertFalse(output.exists())
        finally:
            db.close()

    def test_library_shutdown_signals_and_joins_active_workers(self):
        runtime = LibraryRuntime.__new__(LibraryRuntime)
        runtime.stop_event = MagicMock()
        runtime.condition = MagicMock()
        runtime.observer = None
        runtime.thread = None
        runtime._watch_paths = set()
        runtime._active_lock = threading.RLock()
        runtime._cancel_events = {"job-1": threading.Event()}
        runtime._active_jobs = {"job-1"}
        runtime._worker_threads = {}
        runtime._job_targets = {"job-1": {"root"}}
        runtime._job_target_revisions = {"job-1": {}}
        runtime._flush_reconcile_updates = MagicMock()
        runtime.store = MagicMock()
        finished = threading.Event()

        def worker():
            runtime._cancel_events["job-1"].wait(1)
            finished.set()

        thread = threading.Thread(target=worker)
        runtime._worker_threads["job-1"] = thread
        thread.start()

        runtime.stop(timeout=1)

        self.assertTrue(finished.is_set())
        self.assertFalse(thread.is_alive())
        runtime.stop_event.set.assert_called_once()


class ForegroundShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_bridge_releases_executor_slot_after_worker_settles(self):
        started = threading.Event()
        release = threading.Event()

        def work():
            started.set()
            release.wait(2)
            return "done"

        task = asyncio.create_task(run_foreground(work))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await wait_for_shutdown(2)
        self.assertEqual(active_requests(), 0)


if __name__ == "__main__":
    unittest.main()
