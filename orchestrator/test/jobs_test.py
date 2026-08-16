import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.database import DatabaseHandler
from app.jobs import (
    JobScheduler,
    JobStore,
    MetadataMissingJob,
    MetadataUpgradeJob,
    _metadata_document_gaps,
    _repair_missing_tv_child_identities,
)
from app.progress import PROGRESS_TOTAL, WholeJobProgress


class DatabaseRollbackTest(unittest.TestCase):
    def test_failed_statement_rolls_back_connection_before_next_transaction(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            db.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE child (parent_id TEXT NOT NULL REFERENCES parent(id))"
            )
            with self.assertRaises(Exception):
                db.execute("INSERT INTO child VALUES('missing')")
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
                            self.assertEqual(
                                db.read_execute("SELECT value FROM values_table"),
                                [(1,)],
                            )
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


class WholeJobProgressTest(unittest.TestCase):
    def test_scan_progress_retains_denominator_for_current_only_updates(self):
        progress = WholeJobProgress("reconcile")
        announced = progress.apply(
            {
                "state": "running",
                "progress_total": 618,
                "message": "Reconciling changed series roots (618 roots)",
            }
        )
        indexed = progress.apply(
            {"progress_current": 544, "message": "Indexing series 544/618"}
        )

        self.assertEqual(announced["progress_total"], PROGRESS_TOTAL)
        self.assertGreater(indexed["progress_current"], announced["progress_current"])
        self.assertEqual(indexed["progress_total"], PROGRESS_TOTAL)

    def test_progress_is_fixed_and_monotonic_across_stage_counter_resets(self):
        progress = WholeJobProgress("scan")
        first = progress.apply(
            {
                "state": "running",
                "progress_current": 1,
                "progress_total": 10,
                "message": "Indexed One",
            }
        )
        second = progress.apply(
            {
                "progress_current": 0,
                "progress_total": 50,
                "message": "Resolving new metadata",
            }
        )
        completed = progress.apply(
            {"state": "completed", "message": "Indexed 1 entries"}
        )

        self.assertEqual(first["progress_total"], PROGRESS_TOTAL)
        self.assertGreaterEqual(second["progress_current"], first["progress_current"])
        self.assertEqual(completed["progress_current"], PROGRESS_TOTAL)
        self.assertEqual(completed["progress_total"], PROGRESS_TOTAL)

    def test_terminal_failure_keeps_progress_partial(self):
        progress = WholeJobProgress("metadata_refresh")
        progress.apply(
            {
                "state": "running",
                "progress_current": 5,
                "progress_total": 10,
                "message": "Processing 5/10",
            }
        )
        failed = progress.apply(
            {
                "state": "failed",
                "progress_current": 5,
                "progress_total": 10,
                "message": "5 repair errors",
            }
        )

        self.assertLess(failed["progress_current"], PROGRESS_TOTAL)
        self.assertEqual(failed["progress_total"], PROGRESS_TOTAL)


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
        self.db.execute(
            "INSERT INTO library_entities VALUES('movie-1','library-1','movie')"
        )
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

    def test_detects_missing_provider_title_for_a_season(self):
        self.db.execute(
            "INSERT INTO library_entities VALUES('season-1','library-1','season')"
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES('season-1','tvdb','season','1726050',1)"
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("season-1", "en", json.dumps({"title": "Season 1", "images": {}})),
        )

        gaps, _linked = _metadata_document_gaps(
            self.db,
            "tvdb",
            "season",
            "1726050",
            "en",
            {"title": None, "seasonNumber": 1, "images": [], "children": []},
        )

        self.assertIn("metadata:title", gaps)

    def test_detects_projection_missing_a_cached_artwork_blurhash(self):
        self.db.execute("ALTER TABLE metadata_images ADD COLUMN blur_hash TEXT")
        primary_path = self._ready_file("primary-hash.webp")
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
            "INSERT INTO metadata_images VALUES(?,?,?,?,?,?,?,?)",
            (
                "tmdb",
                "movie",
                "42",
                "",
                "Primary",
                "https://img/primary",
                primary_path,
                "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
            ),
        )
        gaps, _linked = _metadata_document_gaps(
            self.db,
            "tmdb",
            "movie",
            "42",
            "en",
            {
                "title": "Example",
                "images": [{"type": "Primary", "url": "https://img/primary"}],
            },
        )
        self.assertIn("projection-artwork-blurhash:Primary", gaps)

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
        self.assertEqual(
            store.updates[0]["message"], "Processing 0/1 metadata documents"
        )
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

    def test_job_refetches_a_cached_season_without_a_provider_title(self):
        self.db.execute(
            "INSERT INTO library_entities VALUES('season-1','library-1','season')"
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES('season-1','tvdb','season','1726050',1)"
        )
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            ("season-1", "en", json.dumps({"title": "Season 1", "images": {}})),
        )
        cached = {"title": None, "seasonNumber": 1, "images": [], "children": []}
        fetched = {
            "title": "邂逅 (Kaikō / Kaikou)",
            "seasonNumber": 1,
            "images": [],
            "children": [],
        }

        class Cache:
            def get(self, _provider, entity_type, _provider_id, _locale):
                return (
                    cached
                    if entity_type == "season"
                    else {
                        "title": "Example",
                        "images": [],
                    }
                )

        class Ingest:
            metadata_service = type("MetadataService", (), {"cache": Cache()})()

            def __init__(self):
                self.fetches = []

            def locales(self):
                return ["en"]

            def ingest_locales(
                self, provider, entity_type, provider_id, locales, **_kwargs
            ):
                self.fetches.append((provider, entity_type, provider_id, locales))
                return {"en": fetched}

            def ingest_document(self, *_args, **_kwargs):
                return None

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

        with patch("app.jobs.MetadataIngestService", return_value=ingest):
            MetadataMissingJob(store).run("run-1", {"config": {"batchSize": 1}})

        self.assertEqual(ingest.fetches, [("tvdb", "season", "1726050", ["en"])])

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

    def test_forced_refresh_passes_asset_preservation_through_to_ingest(self):
        class Cache:
            def get(self, *_args):
                return None

        class Ingest:
            metadata_service = type("MetadataService", (), {"cache": Cache()})()

            def __init__(self):
                self.kwargs = None

            def locales(self):
                return ["en"]

            def ingest_locales(self, *_args, **kwargs):
                self.kwargs = kwargs
                return {"en": {"title": "Example", "images": []}}

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

        with patch("app.jobs.MetadataIngestService", return_value=ingest):
            MetadataMissingJob(store).run(
                "run-1",
                {"config": {"batchSize": 1}},
                force=True,
                force_assets=False,
            )

        self.assertEqual(ingest.kwargs, {"force": True, "force_assets": False})

    def test_upgrade_refetches_without_projecting_unchanged_documents(self):
        self.db.execute(
            "INSERT INTO catalog_item_projection VALUES(?,?,?)",
            (
                "movie-1",
                "en",
                json.dumps(
                    {"title": "Example", "overview": "Old overview", "images": {}}
                ),
            ),
        )
        previous = {"title": "Example", "overview": "Old overview", "images": []}
        fresh = {"title": "Example", "overview": "New overview", "images": []}

        class Cache:
            def get(self, *_args):
                return dict(previous)

        class Service:
            def __init__(self):
                self.cache = Cache()
                self.fetches = []

            def fetch_locales(self, *args, **kwargs):
                self.fetches.append((args, kwargs))
                return {"en": dict(fresh)}

        class Ingest:
            def __init__(self):
                self.metadata_service = Service()
                self.materialized = []

            def locales(self):
                return ["en"]

            def ingest_document(
                self, provider, entity_type, provider_id, locale, document, **kwargs
            ):
                self.materialized.append(
                    (provider, entity_type, provider_id, locale, kwargs)
                )
                self_db.execute(
                    "UPDATE catalog_item_projection SET payload=? WHERE entity_id='movie-1' AND locale='en'",
                    (
                        json.dumps(
                            {
                                "title": "Example",
                                "overview": document["overview"],
                                "images": {},
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

        with (
            patch("app.jobs.MetadataIngestService", return_value=ingest),
            patch("app.catalog_read_model.CatalogReadModel", return_value=read_model),
        ):
            MetadataUpgradeJob(store).run("run-1", {"config": {"batchSize": 1}})

        self.assertEqual(
            ingest.metadata_service.fetches[0][1], {"force": True, "project": False}
        )
        self.assertEqual(ingest.materialized[0][-1], {"force_assets": False})
        self.assertIn("upgraded 1", store.updates[-1]["message"])
        self.assertEqual(json.loads(store.updates[-1]["error_details"])["upgraded"], 1)
        read_model.refresh_roots.assert_called_once_with(["movie-1"])


class MissingTvChildIdentityRepairTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE library_entities("
            "id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,"
            "season_number INTEGER,episode_number INTEGER,match_status TEXT,"
            "match_confidence REAL,match_method TEXT,updated_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE entity_provider_ids("
            "entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,"
            "is_primary INTEGER,PRIMARY KEY(entity_id,provider,identifier_type))"
        )
        self.db.execute(
            "INSERT INTO library_entities VALUES"
            "('series-1','library-1',NULL,'series',NULL,NULL,'matched',1.0,'scan', 'now'),"
            "('season-1','library-1','series-1','season',1,NULL,'unresolved',NULL,NULL,'now'),"
            "('episode-1','library-1','season-1','episode',1,1,'unresolved',NULL,NULL,'now'),"
            "('episode-2','library-1','season-1','episode',1,2,'unresolved',NULL,NULL,'now')"
        )
        self.db.execute(
            "INSERT INTO entity_provider_ids VALUES"
            "('series-1','tvdb','series','458309',1)"
        )

    def tearDown(self):
        self.db.close()

    def test_reconstructs_missing_season_and_episode_ids_from_tvdb_series(self):
        class Service:
            def __init__(self):
                self.calls = []

            def series_child_ids(self, provider, provider_id):
                self.calls.append((provider, provider_id))
                return {
                    "seasons": [{"seasonNumber": 1, "providerId": "season-1"}],
                    "episodes": [
                        {
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "providerId": "episode-1",
                        },
                        {
                            "seasonNumber": 1,
                            "episodeNumber": 2,
                            "providerId": "episode-2",
                        },
                    ],
                }

        service = Service()

        repaired = _repair_missing_tv_child_identities(self.db, service)

        self.assertEqual(repaired, 3)
        self.assertEqual(service.calls, [("tvdb", "458309")])
        self.assertEqual(
            self.db.execute(
                "SELECT entity_id,identifier_type,provider_id,is_primary "
                "FROM entity_provider_ids WHERE entity_id<>'series-1' "
                "ORDER BY identifier_type,entity_id"
            ),
            [
                ("episode-1", "episode", "episode-1", 1),
                ("episode-2", "episode", "episode-2", 1),
                ("season-1", "season", "season-1", 1),
            ],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT id,match_status,match_method FROM library_entities "
                "WHERE id<>'series-1' ORDER BY id"
            ),
            [
                ("episode-1", "matched", "parent_resolution"),
                ("episode-2", "matched", "parent_resolution"),
                ("season-1", "matched", "parent_resolution"),
            ],
        )


class JobMappingTest(unittest.TestCase):
    def _scheduler_store(self):
        db = DatabaseHandler("sqlite", {}, ":memory:")
        db.execute(
            "CREATE TABLE job_definitions (id TEXT PRIMARY KEY, job_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, description TEXT, kind TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 1440, enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}', next_run_at TEXT, last_run_at TEXT, last_run_id TEXT, last_state TEXT NOT NULL DEFAULT 'idle', last_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE job_runs (id TEXT PRIMARY KEY, definition_id TEXT NOT NULL, library_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, error_details TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, thread_name TEXT)"
        )
        db.execute(
            "CREATE TABLE job_schedule_triggers (id TEXT PRIMARY KEY, definition_id TEXT NOT NULL, trigger_type TEXT NOT NULL, interval_seconds INTEGER, time_of_day TEXT, weekday INTEGER, next_run_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        store = JobStore.__new__(JobStore)
        store.db = db
        return db, store

    def test_definition_mapping_uses_all_persisted_columns(self):
        row = (
            "id",
            "key",
            "Name",
            "Description",
            "kind",
            60,
            1,
            '{"libraryId":"library-1"}',
            "next",
            "last",
            "run",
            "completed",
            "done",
            "created",
            "updated",
        )
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
        row = (
            "run",
            "definition",
            None,
            "metadata_refresh",
            "running",
            4,
            10,
            "Working",
            None,
            None,
            "created",
            "started",
            None,
            "worker",
        )
        value = JobStore._run(row)
        self.assertEqual(value["progressCurrent"], 4)
        self.assertEqual(value["threadName"], "worker")

    def test_run_mapping_accepts_structured_progress_detail(self):
        row = (
            "run",
            "definition",
            None,
            "trickplay_extract",
            "running",
            1200,
            10000,
            "Extracting trickplay",
            None,
            None,
            "created",
            "started",
            None,
            "worker",
            "extraction",
            "Extracting trickplay",
            12,
            87,
            "videos",
            "Dune.mkv",
            None,
            "{}",
        )
        value = JobStore._run(row)
        self.assertEqual(
            value["progressDetail"],
            {
                "phase": "extraction",
                "label": "Extracting trickplay",
                "current": 12,
                "total": 87,
                "unit": "videos",
                "item": "Dune.mkv",
            },
        )

    def test_default_tasks_include_orphan_cleanup(self):
        db, store = self._scheduler_store()
        try:
            store.ensure_defaults()

            cleanup = store.by_key("metadata_cleanup")
            self.assertIsNotNone(cleanup)
            self.assertEqual(cleanup["kind"], "metadata_cleanup")
            self.assertEqual(cleanup["intervalMinutes"], 10080)
        finally:
            db.close()

    def test_removes_legacy_library_definition_by_configured_owner(self):
        db, store = self._scheduler_store()
        try:
            db.execute(
                "INSERT INTO job_definitions(id,job_key,name,kind,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    "definition-1",
                    "library_delta_verify:library-1",
                    "Scan Library",
                    "library_scan",
                    '{"libraryId":"library-1"}',
                    "created",
                    "updated",
                ),
            )
            db.execute(
                "INSERT INTO job_runs(id,definition_id,kind,created_at) VALUES('run-1','definition-1','library_scan','created')"
            )
            db.execute(
                "INSERT INTO job_schedule_triggers(id,definition_id,trigger_type,created_at,updated_at) VALUES('trigger-1','definition-1','interval','created','updated')"
            )

            store.remove_library_definitions("library-1")

            self.assertEqual(db.execute("SELECT id FROM job_definitions"), [])
            self.assertEqual(db.execute("SELECT id FROM job_runs"), [])
            self.assertEqual(db.execute("SELECT id FROM job_schedule_triggers"), [])
        finally:
            db.close()

    def test_reconciles_legacy_and_orphaned_library_definitions(self):
        db, store = self._scheduler_store()
        try:
            for values in (
                (
                    "legacy",
                    "library_delta_verify:library-1",
                    "Scan Existing",
                    '{"libraryId":"library-1"}',
                ),
                (
                    "orphan",
                    "library_scan:deleted-library",
                    "Scan Deleted",
                    '{"libraryId":"deleted-library"}',
                ),
            ):
                db.execute(
                    "INSERT INTO job_definitions(id,job_key,name,kind,config,created_at,updated_at) VALUES(?,?,?,'library_scan',?,'created','updated')",
                    values,
                )
            db.execute(
                "INSERT INTO job_schedule_triggers(id,definition_id,trigger_type,created_at,updated_at) VALUES('orphan-trigger','orphan','interval','created','updated')"
            )

            store.reconcile_library_definitions(
                [
                    {
                        "id": "library-1",
                        "name": "Existing",
                        "scanIntervalMinutes": 60,
                        "watchEnabled": True,
                    }
                ]
            )

            self.assertEqual(
                db.execute("SELECT id,job_key,config FROM job_definitions ORDER BY id"),
                [
                    (
                        "legacy",
                        "library_scan:library-1",
                        '{"libraryId": "library-1"}',
                    )
                ],
            )
            self.assertEqual(db.execute("SELECT id FROM job_schedule_triggers"), [])
        finally:
            db.close()


class JobLockingTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE job_definitions (id TEXT PRIMARY KEY, last_state TEXT, last_message TEXT, updated_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE job_runs (id TEXT PRIMARY KEY, definition_id TEXT, library_id TEXT, kind TEXT, state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, error_details TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, thread_name TEXT)"
        )
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


class AnalysisCapacityTest(unittest.TestCase):
    def test_intro_outro_and_trickplay_analysis_jobs_can_run_together(self):
        scheduler = JobScheduler.__new__(JobScheduler)
        scheduler.store = MagicMock()
        barrier = threading.Barrier(2)
        errors = []

        class Worker:
            def run(self, run_id, store, should_terminate):
                barrier.wait(timeout=2)

        def run(kind):
            try:
                with patch("app.jobs.active_requests", return_value=0):
                    scheduler._run_analysis(kind, kind, Worker(), lambda: False)
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=run, args=("intro_outro_detect",))
        second = threading.Thread(target=run, args=("trickplay_extract",))
        first.start()
        second.start()
        first.join(timeout=3)
        second.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
