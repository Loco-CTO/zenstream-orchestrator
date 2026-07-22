import threading
import unittest
from app.database import DatabaseHandler
from app.jobs import JobScheduler, JobStore


class DatabaseRollbackTest(unittest.TestCase):
    def test_failed_statement_rolls_back_connection_before_next_transaction(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE child (parent_id TEXT NOT NULL REFERENCES parent(id))")
            self.assertIsInstance(db.execute("INSERT INTO child VALUES('missing')"), Exception)
            with db.transaction() as cursor:
                cursor.execute("INSERT INTO parent VALUES('valid')")
            self.assertEqual(db.execute("SELECT id FROM parent"), [("valid",)])
        finally:
            db.close()


class JobMappingTest(unittest.TestCase):
    def test_definition_mapping_uses_all_persisted_columns(self):
        row = ("id", "key", "Name", "Description", "kind", 60, 1, '{"libraryId":"library-1"}', "next", "last", "run", "completed", "done", "created", "updated")
        value = JobStore._definition(row)
        self.assertEqual(value["config"], {"libraryId": "library-1"})
        self.assertEqual(value["nextRunAt"], "next")
        self.assertEqual(value["lastRunAt"], "last")
        self.assertEqual(value["lastRunId"], "run")
        self.assertEqual(value["lastState"], "completed")
        self.assertEqual(value["lastMessage"], "done")
        self.assertEqual(value["createdAt"], "created")
        self.assertEqual(value["updatedAt"], "updated")
        self.assertTrue(value["enabled"])

    def test_run_mapping_preserves_progress_and_thread(self):
        row = ("run", "definition", None, "metadata_refresh", "running", 4, 10, "Working", None, None, "created", "started", None, "worker")
        value = JobStore._run(row)
        self.assertEqual(value["progressCurrent"], 4)
        self.assertEqual(value["threadName"], "worker")

    def test_default_tasks_include_orphan_cleanup(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE job_definitions (id TEXT PRIMARY KEY, job_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, description TEXT, kind TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 1440, enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}', next_run_at TEXT, last_run_at TEXT, last_run_id TEXT, last_state TEXT NOT NULL DEFAULT 'idle', last_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            store = JobStore.__new__(JobStore)
            store.db = db

            store.ensure_defaults()

            cleanup = store.by_key("metadata_cleanup")
            self.assertIsNotNone(cleanup)
            self.assertEqual(cleanup["kind"], "metadata_cleanup")
            self.assertEqual(cleanup["intervalMinutes"], 10080)
        finally:
            db.close()


class JobLockingTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute("CREATE TABLE job_definitions (id TEXT PRIMARY KEY, last_state TEXT, last_message TEXT, updated_at TEXT)")
        self.db.execute("CREATE TABLE job_runs (id TEXT PRIMARY KEY, definition_id TEXT, library_id TEXT, kind TEXT, state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, error_details TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, thread_name TEXT)")
        self.store = JobStore.__new__(JobStore)
        self.store.db = self.db

    def tearDown(self):
        self.db.close()

    def test_each_task_gets_one_active_run_without_blocking_other_tasks(self):
        first = {"id": "task-1", "kind": "metadata_refresh", "config": {}}
        second = {"id": "task-2", "kind": "metadata_refresh", "config": {}}
        self.db.execute("INSERT INTO job_definitions(id) VALUES('task-1')")
        self.db.execute("INSERT INTO job_definitions(id) VALUES('task-2')")

        first_run, first_created = self.store.create_or_get_active_run(first)
        duplicate, duplicate_created = self.store.create_or_get_active_run(first)
        second_run, second_created = self.store.create_or_get_active_run(second)

        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["id"], first_run["id"])
        self.assertTrue(second_created)
        self.assertNotEqual(second_run["id"], first_run["id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM job_runs")[0][0], 2)


if __name__ == "__main__":
    unittest.main()
