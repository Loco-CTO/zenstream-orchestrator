import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.database import DatabaseHandler
from app.library import LibraryRuntime, LibraryStore
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError


class LibraryRuntimeDispatcherTest(unittest.TestCase):
    def _runtime(self):
        runtime = LibraryRuntime.__new__(LibraryRuntime)
        runtime.condition = threading.Condition()
        runtime.stop_event = threading.Event()
        runtime.thread = None
        runtime._lifecycle_lock = threading.RLock()
        runtime._active_lock = threading.RLock()
        runtime._active_jobs = set()
        runtime._cancel_events = {}
        runtime._worker_threads = {}
        runtime._last_loop_success = None
        runtime._last_loop_error = None
        runtime._consecutive_loop_failures = 0
        runtime._last_dispatch_backoff_seconds = 0.0
        runtime.store = SimpleNamespace(
            db=SimpleNamespace(
                metrics=lambda: {
                    "reader_active": 32,
                    "reader_peak": 32,
                    "reader_checkout_timeouts": 1,
                }
            )
        )
        runtime._configure_watchers = Mock()
        runtime._recover_active_jobs = Mock()
        return runtime

    def test_reader_timeout_keeps_dispatcher_alive_and_backoff_resets(self):
        runtime = self._runtime()
        first_failure = threading.Event()
        recovered = threading.Event()
        release = threading.Event()
        calls = 0

        def run_iteration():
            nonlocal calls
            calls += 1
            if calls == 1:
                first_failure.set()
                raise SQLAlchemyTimeoutError("reader pool busy")
            recovered.set()
            release.wait(2)
            runtime.stop_event.set()

        runtime._run_iteration = run_iteration
        with patch("app.library.LIBRARY_DISPATCH_BACKOFF_INITIAL", 0.01), patch(
            "app.library.LIBRARY_DISPATCH_BACKOFF_MAX", 0.02
        ):
            worker = threading.Thread(target=runtime._run)
            runtime.thread = worker
            worker.start()
            self.assertTrue(first_failure.wait(2))
            self.assertTrue(worker.is_alive())
            self.assertTrue(recovered.wait(2))
            release.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(runtime.diagnostics()["consecutive_failures"], 0)
        self.assertEqual(runtime.diagnostics()["last_backoff_seconds"], 0.0)
        self.assertIsNotNone(runtime.diagnostics()["last_loop_success"])

    def test_dispatch_setup_failure_requeues_claimed_job_and_clears_ownership(self):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            database.execute(
                "CREATE TABLE library_jobs(id TEXT PRIMARY KEY, state TEXT, progress_current INTEGER, progress_total INTEGER, message TEXT, error TEXT, error_details TEXT, started_at TEXT, finished_at TEXT)"
            )
            database.execute(
                "INSERT INTO library_jobs VALUES('job-1','running',3,10,'Starting scan',NULL,NULL,'started',NULL)"
            )
            store = LibraryStore.__new__(LibraryStore)
            store.db = database
            runtime = self._runtime()
            runtime.store = store
            runtime._active_jobs.add("job-1")
            runtime._cancel_events["job-1"] = threading.Event()
            runtime._worker_threads["job-1"] = threading.Thread()
            runtime._notify_job_status = Mock(side_effect=RuntimeError("notify failed"))

            with self.assertRaisesRegex(RuntimeError, "notify failed"):
                runtime._dispatch_claimed_job(
                    "job-1",
                    "library-1",
                    "scan",
                    {"id": "job-1", "state": "running"},
                    "started",
                )

            row = database.execute(
                "SELECT state,progress_current,progress_total,message,started_at,finished_at FROM library_jobs WHERE id='job-1'"
            )[0]
            self.assertEqual(
                row,
                ("queued", 0, 0, "Queued again after dispatcher recovery", None, None),
            )
            self.assertNotIn("job-1", runtime._active_jobs)
            self.assertNotIn("job-1", runtime._cancel_events)
            self.assertNotIn("job-1", runtime._worker_threads)
        finally:
            database.close()

    def test_ensure_running_restarts_dead_dispatcher_once(self):
        runtime = self._runtime()
        old_thread = threading.Thread(target=lambda: None)
        old_thread.start()
        old_thread.join(2)
        runtime.thread = old_thread
        release = threading.Event()
        runtime._run = lambda: release.wait(2)

        self.assertTrue(runtime.ensure_running())
        restarted = runtime.thread
        self.assertTrue(restarted and restarted.is_alive())
        self.assertTrue(runtime.ensure_running())
        self.assertIs(runtime.thread, restarted)
        runtime.stop_event.set()
        release.set()
        restarted.join(2)
        self.assertEqual(runtime._recover_active_jobs.call_count, 1)


if __name__ == "__main__":
    unittest.main()
