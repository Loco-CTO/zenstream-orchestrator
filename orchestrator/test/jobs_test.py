import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from app.database import DatabaseHandler
from app.jobs import (
    JobScheduler,
    JobStore,
    MetadataMissingJob,
    _metadata_document_gaps,
)


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

    def test_concurrent_reads_share_a_safe_read_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseHandler("sqlite", {}, f"{directory}/orchestrator.db")
            try:
                db.execute("CREATE TABLE values_table (value INTEGER NOT NULL)")
                db.execute("INSERT INTO values_table VALUES (1)")
                errors = []

                def read_values():
                    try:
                        for _ in range(25):
                            self.assertEqual(db.read_execute("SELECT value FROM values_table"), [(1,)])
                    except Exception as error:
                        errors.append(error)

                threads = [threading.Thread(target=read_values) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [])
            finally:
                db.close()


class MetadataMissingInspectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,entity_type TEXT)"
        )
        self.db.execute(
            "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,is_primary INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE catalog_item_projection(entity_id TEXT,locale TEXT,payload TEXT,PRIMARY KEY(entity_id,locale))"
        )
        self.db.execute(
            "CREATE TABLE metadata_images(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,image_type TEXT,image_url TEXT,local_path TEXT)"
        )
        self.db.execute(
            "CREATE TABLE entity_person_credits(entity_id TEXT,provider TEXT,locale TEXT)"
        )
        self.db.execute(
            "CREATE TABLE people(provider TEXT,provider_person_id TEXT,image_url TEXT,local_path TEXT)"
        )
        self.db.execute("INSERT INTO library_entities VALUES('movie-1','library-1','movie')")
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES('movie-1','tmdb','movie','42',1)"
        )

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def _ready_file(self, name: str) -> str:
        path = Path(self.directory.name) / name
        path.write_bytes(b"webp")
        return str(path)

    def test_finds_partial_fields_and_each_missing_artwork_type(self):
        primary_path = self._ready_file("primary.webp")
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                "movie-1",
                "en",
                json.dumps(
                    {
                        "title": "Example",
                        "images": {"Primary": {"url": "/primary"}},
                    }
                ),
            ),
        )
        self.db.execute(
            "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?)",
            ("tmdb", "movie", "42", "", "Primary", "https://img/primary", primary_path),
        )
        document = {
            "title": "Example",
            "overview": "A complete overview",
            "images": [
                {"type": "Primary", "url": "https://img/primary"},
                {"type": "Backdrop", "url": "https://img/backdrop"},
            ],
        }

        gaps, linked = _metadata_document_gaps(
            self.db, "tmdb", "movie", "42", "en", document
        )

        self.assertEqual(linked, [("movie-1", "library-1")])
        self.assertIn("metadata:overview", gaps)
        self.assertIn("artwork:Backdrop", gaps)
        self.assertIn("projection-artwork:Backdrop", gaps)
        self.assertNotIn("artwork:Primary", gaps)

    def test_detects_missing_credits_and_portraits(self):
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("movie-1", "en", json.dumps({"title": "Example"})),
        )
        document = {
            "title": "Example",
            "images": [],
            "credits": {
                "cast": [
                    {
                        "id": "person-1",
                        "name": "Actor",
                        "imageUrl": "https://img/person",
                    }
                ],
                "crew": [],
            },
        }

        gaps, _linked = _metadata_document_gaps(
            self.db, "tmdb", "movie", "42", "en", document
        )

        self.assertIn("credits", gaps)
        self.assertIn("portrait", gaps)

    def test_job_synchronously_repairs_and_publishes_partial_cached_document(self):
        primary_path = self._ready_file("job-primary.webp")
        backdrop_path = str(Path(self.directory.name) / "job-backdrop.webp")
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                "movie-1",
                "en",
                json.dumps(
                    {
                        "title": "Example",
                        "images": {"Primary": {"url": "/primary"}},
                    }
                ),
            ),
        )
        self.db.execute(
            "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?)",
            ("tmdb", "movie", "42", "", "Primary", "https://img/primary", primary_path),
        )
        document = {
            "title": "Example",
            "overview": "A complete overview",
            "images": [
                {"type": "Primary", "url": "https://img/primary"},
                {"type": "Backdrop", "url": "https://img/backdrop"},
            ],
        }

        class Cache:
            def get(self, *_args):
                return dict(document)

        class Ingest:
            metadata_service = type("MetadataService", (), {"cache": Cache()})()

            def locales(self):
                return ["en"]

            def ingest_document(self, *_args):
                Path(backdrop_path).write_bytes(b"webp")
                self_db.execute(
                    "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?)",
                    (
                        "tmdb",
                        "movie",
                        "42",
                        "",
                        "Backdrop",
                        "https://img/backdrop",
                        backdrop_path,
                    ),
                )
                self_db.execute(
                    "UPDATE catalog_item_projection SET payload=? WHERE entity_id='movie-1' AND locale='en'",
                    (
                        json.dumps(
                            {
                                "title": "Example",
                                "overview": "A complete overview",
                                "images": {
                                    "Primary": {"url": "/primary"},
                                    "Backdrop": {"url": "/backdrop"},
                                },
                            }
                        ),
                    ),
                )

        self_db = self.db
        ingest = Ingest()
        store = type(
            "Store",
            (),
            {
                "db": self.db,
                "updates": [],
                "update_run": lambda value, _run_id, **fields: value.updates.append(
                    fields
                ),
            },
        )()
        read_model = MagicMock()

        with patch("app.jobs.MetadataIngestService", return_value=ingest) as factory:
            with patch(
                "app.catalog_read_model.CatalogReadModel", return_value=read_model
            ):
                MetadataMissingJob(store).run("run-1", {"config": {"batchSize": 1}})

        factory.assert_called_once_with(background_assets=False)
        read_model.refresh_roots.assert_called_once_with(["movie-1"])
        self.assertEqual(store.updates[0]["message"], "Processing 0/1 metadata documents")
        self.assertEqual(store.updates[-2]["message"], "Processing 1/1: movie tmdb:42")
        self.assertEqual(store.updates[-1]["state"], "completed")
        self.assertIn("repaired 1", store.updates[-1]["message"])

    def test_job_does_not_refetch_complete_stale_cache_document(self):
        document = {"title": "Example", "images": [], "_stale": True}
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("movie-1", "en", json.dumps({"title": "Example", "images": {}})),
        )
        ingest = MagicMock()
        ingest.locales.return_value = ["en"]
        ingest.metadata_service.cache.get.return_value = document
        store = type(
            "Store",
            (),
            {
                "db": self.db,
                "updates": [],
                "update_run": lambda value, _run_id, **fields: value.updates.append(
                    fields
                ),
            },
        )()

        with patch("app.jobs.MetadataIngestService", return_value=ingest):
            MetadataMissingJob(store).run("run-1", {"config": {"batchSize": 1}})

        ingest.ingest_locales.assert_not_called()
        ingest.ingest_document.assert_not_called()
        self.assertEqual(store.updates[-1]["state"], "completed")
        self.assertIn("repaired 0", store.updates[-1]["message"])

    def test_job_completes_when_metadata_is_legitimately_unavailable(self):
        class Cache:
            def get(self, *_args):
                return None

        class Ingest:
            metadata_service = type("MetadataService", (), {"cache": Cache()})()

            def locales(self):
                return ["en"]

            def ingest_locales(self, *_args, **_kwargs):
                return {}

        store = type(
            "Store",
            (),
            {
                "db": self.db,
                "updates": [],
                "update_run": lambda value, _run_id, **fields: value.updates.append(
                    fields
                ),
            },
        )()

        with patch("app.jobs.MetadataIngestService", return_value=Ingest()):
            MetadataMissingJob(store).run("run-1", {"config": {"batchSize": 1}})

        final_update = store.updates[-1]
        self.assertEqual(final_update["state"], "completed")
        self.assertIsNone(final_update.get("error"))
        self.assertIn("1 repairs remain incomplete", final_update["message"])

    def test_job_fails_when_provider_repair_raises_an_error(self):
        class Cache:
            def get(self, *_args):
                return None

        class Ingest:
            metadata_service = type("MetadataService", (), {"cache": Cache()})()

            def locales(self):
                return ["en"]

            def ingest_locales(self, *_args, **_kwargs):
                raise ValueError("provider response was invalid")

        store = type(
            "Store",
            (),
            {
                "db": self.db,
                "updates": [],
                "update_run": lambda value, _run_id, **fields: value.updates.append(
                    fields
                ),
            },
        )()

        with patch("app.jobs.MetadataIngestService", return_value=Ingest()):
            MetadataMissingJob(store).run("run-1", {"config": {"batchSize": 1}})

        final_update = store.updates[-1]
        self.assertEqual(final_update["state"], "failed")
        self.assertIn("1 repair errors", final_update["error"])


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
