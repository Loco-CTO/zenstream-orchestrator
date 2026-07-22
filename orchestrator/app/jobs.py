"""Persistent scheduler definitions and non-blocking background job execution."""

from __future__ import annotations

import json
import threading
import time
import uuid
import traceback
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.library import JobTerminated, runtime as library_runtime
from app.providers import ProviderError, MetadataService
from app.models.metadata import MetadataLanguageSettings
from app.logging_config import get_logger


logger = get_logger("jobs")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class JobStore:
    def __init__(self):
        self.db = Config().database

    @staticmethod
    def _definition(row) -> dict:
        try:
            config = json.loads(row[7] or "{}")
        except json.JSONDecodeError:
            config = {}
        return {
            "id": row[0], "key": row[1], "name": row[2], "description": row[3],
            "kind": row[4], "intervalMinutes": row[5], "enabled": bool(row[6]),
            "config": config, "nextRunAt": row[8], "lastRunAt": row[9],
            "lastRunId": row[10], "lastState": row[11], "lastMessage": row[12],
            "createdAt": row[13], "updatedAt": row[14],
        }

    @staticmethod
    def _run(row) -> dict:
        return {
            "id": row[0], "definitionId": row[1], "libraryId": row[2], "kind": row[3],
            "state": row[4], "progressCurrent": row[5], "progressTotal": row[6],
            "message": row[7], "error": row[8], "errorDetails": row[9], "createdAt": row[10],
            "startedAt": row[11], "finishedAt": row[12], "threadName": row[13],
        }

    def definitions(self) -> list[dict]:
        rows = self.db.execute("SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions ORDER BY name COLLATE NOCASE")
        return [self._definition(row) for row in rows]

    def definition(self, definition_id: str) -> dict | None:
        rows = self.db.execute("SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions WHERE id=?", (definition_id,))
        return self._definition(rows[0]) if rows else None

    def by_key(self, key: str) -> dict | None:
        rows = self.db.execute("SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions WHERE job_key=?", (key,))
        return self._definition(rows[0]) if rows else None

    def ensure(self, key: str, name: str, description: str, kind: str, interval: int = 1440, config: dict | None = None, enabled: bool = True) -> dict:
        existing = self.by_key(key)
        if existing:
            return existing
        timestamp = now()
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=max(5, min(43200, int(interval or 1440))))).isoformat()
        definition_id = new_id()
        self.db.execute("INSERT INTO job_definitions(id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (definition_id, key, name, description, kind, max(5, min(43200, int(interval or 1440))), int(enabled), json.dumps(config or {}, ensure_ascii=False), next_run, timestamp, timestamp))
        return self.definition(definition_id)  # type: ignore[return-value]

    def ensure_defaults(self) -> None:
        definition = self.ensure("metadata_refresh", "Refresh metadata", "Fetch provider metadata and artwork for indexed IDs.", "metadata_refresh", 1440, {"locales": ["en"], "batchSize": 50})
        if definition["lastRunAt"] is None:
            self.db.execute("UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?", (now(), now(), definition["id"]))

    def ensure_library(self, library: dict) -> dict:
        description = "Index the library without moving or renaming files."
        definition = self.ensure(f"library_scan:{library['id']}", f"Scan {library['name']}", description, "library_scan", library.get("scanIntervalMinutes") or 1440, {"libraryId": library["id"]}, library.get("watchEnabled", True))
        # Repair older definitions whose config was lost by the former row mapper,
        # while preserving task-level interval and enabled settings.
        self.db.execute(
            "UPDATE job_definitions SET name=?,description=?,config=?,updated_at=? WHERE id=?",
            (f"Scan {library['name']}", description, json.dumps({"libraryId": library["id"]}), now(), definition["id"]),
        )
        return self.definition(definition["id"])  # type: ignore[return-value]

    def update_definition(self, definition_id: str, values: dict) -> dict:
        definition = self.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        interval = max(5, min(43200, int(values.get("intervalMinutes", definition["intervalMinutes"]) or 1440)))
        enabled = int(bool(values.get("enabled", definition["enabled"])))
        name = str(values.get("name", definition["name"])).strip() or definition["name"]
        config = values.get("config", definition["config"])
        self.db.execute("UPDATE job_definitions SET name=?,interval_minutes=?,enabled=?,config=?,next_run_at=?,updated_at=? WHERE id=?", (name, interval, enabled, json.dumps(config or {}, ensure_ascii=False), (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat() if enabled else None, now(), definition_id))
        return self.definition(definition_id)  # type: ignore[return-value]

    def runs(self, definition_id: str | None = None, limit: int = 100) -> list[dict]:
        if definition_id:
            rows = self.db.execute("SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,thread_name FROM job_runs WHERE definition_id=? ORDER BY created_at DESC LIMIT ?", (definition_id, limit))
        else:
            rows = self.db.execute("SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,thread_name FROM job_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._run(row) for row in rows]

    def library_runs(self, library_id: str, limit: int = 10) -> list[dict]:
        rows = self.db.execute("SELECT id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at FROM library_jobs WHERE library_id=? ORDER BY created_at DESC LIMIT ?", (library_id, limit))
        return [{"id": row[0], "definitionId": None, "libraryId": row[1], "kind": row[2], "state": row[3], "progressCurrent": row[4], "progressTotal": row[5], "message": row[6], "error": row[7], "errorDetails": row[8], "createdAt": row[9], "startedAt": row[10], "finishedAt": row[11], "threadName": None} for row in rows]

    def create_run(self, definition: dict) -> dict:
        run_id = new_id()
        library_id = (definition.get("config") or {}).get("libraryId")
        timestamp = now()
        self.db.execute("INSERT INTO job_runs(id,definition_id,library_id,kind,created_at) VALUES(?,?,?,?,?)", (run_id, definition["id"], library_id, definition["kind"], timestamp))
        self.db.execute("UPDATE job_definitions SET last_state='queued',last_message=?,updated_at=? WHERE id=?", ("Queued", timestamp, definition["id"]))
        return self.runs(definition["id"], 1)[0]

    def create_or_get_active_run(self, definition: dict) -> tuple[dict, bool]:
        """Atomically keep at most one queued/running run for a task definition."""
        timestamp = now()
        with self.db.transaction() as cursor:
            cursor.execute("SELECT id FROM job_runs WHERE definition_id=? AND state IN ('queued','running','terminating') ORDER BY created_at DESC LIMIT 1", (definition["id"],))
            existing = cursor.fetchone()
            if existing:
                run_id = existing[0]
                created = False
            else:
                run_id = new_id()
                library_id = (definition.get("config") or {}).get("libraryId")
                cursor.execute("INSERT INTO job_runs(id,definition_id,library_id,kind,created_at) VALUES(?,?,?,?,?)", (run_id, definition["id"], library_id, definition["kind"], timestamp))
                cursor.execute("UPDATE job_definitions SET last_state='queued',last_message=?,updated_at=? WHERE id=?", ("Queued", timestamp, definition["id"]))
                created = True
        runs = [run for run in self.runs(definition["id"], 100) if run["id"] == run_id]
        return runs[0], created

    def queued_or_running(self, definition_id: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM job_runs WHERE definition_id=? AND state IN ('queued','running','terminating') LIMIT 1", (definition_id,)))

    def due(self) -> list[dict]:
        rows = self.db.execute("SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at", (now(),))
        return [self._definition(row) for row in rows]

    def mark_scheduled(self, definition_id: str, run_id: str | None, message: str = "Queued") -> None:
        definition = self.definition(definition_id)
        if not definition:
            return
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=definition["intervalMinutes"])).isoformat()
        self.db.execute("UPDATE job_definitions SET next_run_at=?,last_run_at=?,last_run_id=?,last_state='queued',last_message=?,updated_at=? WHERE id=?", (next_run, now(), run_id, message, now(), definition_id))

    def update_run(self, run_id: str, **values) -> None:
        allowed = {"state", "progress_current", "progress_total", "message", "error", "error_details", "started_at", "finished_at", "thread_name"}
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if updates:
            fields = ",".join(f"{key}=?" for key, _ in updates)
            self.db.execute(f"UPDATE job_runs SET {fields} WHERE id=?", [value for _, value in updates] + [run_id])
        row = self.db.execute("SELECT definition_id,state,message,error FROM job_runs WHERE id=?", (run_id,))
        if row:
            self.db.execute("UPDATE job_definitions SET last_state=?,last_message=?,updated_at=? WHERE id=?", (row[0][1], row[0][2] or row[0][3], now(), row[0][0]))


class MetadataRefreshJob:
    def __init__(self, store: JobStore):
        self.store = store
        self.db = store.db

    def run(self, run_id: str, definition: dict, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        locales = MetadataLanguageSettings().get()
        rows = self.db.execute(
            "SELECT DISTINCT p.provider,e.entity_type,p.provider_id "
            "FROM entity_provider_ids p JOIN library_entities e ON e.id=p.entity_id "
            "WHERE p.provider IN ('tmdb','tvdb','musicbrainz') ORDER BY p.provider,e.entity_type,p.provider_id"
        )
        items = {}
        for provider, entity_type, provider_id in rows:
            for locale in locales:
                items.setdefault((entity_type, provider_id, locale), []).append(provider)
        self.store.update_run(run_id, state="running", started_at=now(), thread_name=threading.current_thread().name, progress_total=len(items), message="Refreshing provider metadata")
        service = MetadataService()
        completed = 0
        failures = []
        for (entity_type, provider_id, locale), providers in items.items():
            if should_terminate():
                raise JobTerminated()
            for provider in providers:
                try:
                    service.fetch(provider, entity_type, provider_id, locale, force=True)
                except (ProviderError, ValueError) as error:
                    failure = {"provider": provider, "entityType": entity_type, "providerId": provider_id, "locale": locale, "error": f"{type(error).__name__}: {error}"}
                    failures.append(failure)
                    logger.exception("scheduled metadata refresh failed provider=%s entity_type=%s provider_id=%s locale=%s", provider, entity_type, provider_id, locale)
            completed += 1
            if completed % 10 == 0 or completed == len(items):
                self.store.update_run(run_id, progress_current=completed, message=f"Refreshed {completed} of {len(items)} entities")
        if failures:
            summary = f"Refreshed {completed} entities; {len(failures)} provider refreshes failed"
            self.store.update_run(run_id, state="failed", progress_current=completed, progress_total=len(items), finished_at=now(), message=summary, error=summary, error_details=json.dumps({"operation": "metadata_refresh", "failures": failures}))
        else:
            self.store.update_run(run_id, state="completed", progress_current=completed, progress_total=len(items), finished_at=now(), message=f"Refreshed {completed} entities")


def _hydrate_request(db, service: MetadataService, entity_id: str, locale: str) -> None:
    logger.info("hydration started entity_id=%s locale=%s", entity_id, locale)
    db.execute("UPDATE metadata_hydration_requests SET state='running',attempts=attempts+1,started_at=?,last_error=NULL WHERE entity_id=? AND locale=?", (now(), entity_id, locale))
    entity_rows = db.execute("SELECT entity_type FROM library_entities WHERE id=?", (entity_id,))
    if not entity_rows or isinstance(entity_rows, Exception):
        reason = "Library entity no longer exists" if not isinstance(entity_rows, Exception) else f"Database lookup failed: {type(entity_rows).__name__}: {entity_rows}"
        details = {"entityId": entity_id, "locale": locale, "operation": "hydration_entity_lookup", "exception": type(entity_rows).__name__ if isinstance(entity_rows, Exception) else "EntityMissing"}
        db.execute("UPDATE metadata_hydration_requests SET state='error',last_error=?,error_details=?,finished_at=? WHERE entity_id=? AND locale=?", (reason, json.dumps(details), now(), entity_id, locale))
        return
    entity_type = entity_rows[0][0]
    provider_rows = db.execute("SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider", (entity_id,))
    succeeded = False
    errors = []
    priorities = {"series": ["tvdb", "tmdb"], "episode": ["tvdb", "tmdb"], "season": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"], "collection": ["tvdb"], "artist": ["musicbrainz"], "release": ["musicbrainz"], "track": ["musicbrainz"]}.get(entity_type, [])
    ordered = sorted(provider_rows, key=lambda value: priorities.index(value[0]) if value[0] in priorities else 99)
    required = priorities[0] if priorities else None
    if not provider_rows:
        errors.append({"provider": required or "unknown", "providerId": None, "error": "No provider ID was resolved during the scan", "required": True})
    for provider, provider_id in ordered:
        try:
            service.fetch(provider, entity_type, provider_id, locale, force=False)
            if provider == required or required is None:
                succeeded = True
        except (ProviderError, ValueError) as error:
            errors.append({"provider": provider, "providerId": provider_id, "error": f"{type(error).__name__}: {error}", "required": provider == required})
            logger.exception("hydration provider failed entity_id=%s locale=%s provider=%s provider_id=%s", entity_id, locale, provider, provider_id)
    state = "ready" if succeeded else "error"
    summary = None if succeeded else f"Metadata hydration failed for {entity_type} '{entity_id}' locale '{locale}': " + "; ".join(value["provider"] + ": " + value["error"] for value in errors)
    db.execute("UPDATE metadata_hydration_requests SET state=?,last_error=?,error_details=?,finished_at=? WHERE entity_id=? AND locale=?", (state, summary, json.dumps({"entityId": entity_id, "entityType": entity_type, "locale": locale, "errors": errors}), now(), entity_id, locale))
    logger.info("hydration finished entity_id=%s locale=%s state=%s", entity_id, locale, state)


class MetadataHydrationQueue:
    """Dedicated on-demand worker; localized requests are never scheduler tasks."""

    def __init__(self, scheduler: "JobScheduler"):
        self.scheduler = scheduler
        self.db = scheduler.store.db
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        # Requests claimed by a worker that died with the process are safe to
        # retry. They are durable requests, not scheduler runs.
        self.db.execute("UPDATE metadata_hydration_requests SET state='queued',started_at=NULL WHERE state='running'")
        self.thread = threading.Thread(target=self._run, name="zenstream-metadata-hydration", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=5)

    def enqueue(self, entity_ids: list[str], locale: str) -> dict:
        if not self.thread or not self.thread.is_alive():
            self.start()
        locale = (locale or "en").strip().lower()
        values = list(dict.fromkeys(str(value) for value in entity_ids if str(value).strip()))
        queued = 0
        request_ids = []
        already_ready = 0
        already_queued = 0
        with self.scheduler.store.db.transaction() as cursor:
            for entity_id in values:
                cursor.execute("SELECT state FROM metadata_hydration_requests WHERE entity_id=? AND locale=?", (entity_id, locale))
                existing = cursor.fetchone()
                if existing and existing[0] == "ready":
                    already_ready += 1
                    continue
                if existing and existing[0] in {"queued", "running"}:
                    already_queued += 1
                    continue
                timestamp = now()
                cursor.execute(
                    "INSERT INTO metadata_hydration_requests(entity_id,locale,state,attempts,last_error,requested_at,started_at,finished_at) VALUES(?,?, 'queued',0,NULL,?,?,NULL) "
                    "ON CONFLICT(entity_id,locale) DO UPDATE SET state='queued',last_error=NULL,requested_at=excluded.requested_at,started_at=NULL,finished_at=NULL",
                    (entity_id, locale, timestamp, None),
                )
                queued += 1
                request_ids.append(f"{entity_id}:{locale}")
        with self.condition:
            self.condition.notify_all()
        worker_state = "running" if self.thread and self.thread.is_alive() else "starting"
        return {"locale": locale, "requested": len(values), "queued": queued, "alreadyReady": already_ready, "alreadyQueued": already_queued, "requestIds": request_ids, "workerState": worker_state}

    def _run(self) -> None:
        while not self.stop_event.is_set():
            rows = self.db.execute("SELECT entity_id,locale FROM metadata_hydration_requests WHERE state='queued' ORDER BY requested_at LIMIT 50")
            if not rows:
                with self.condition:
                    self.condition.wait(timeout=1)
                continue
            service = MetadataService()
            for entity_id, locale in rows:
                if self.stop_event.is_set():
                    break
                try:
                    _hydrate_request(self.db, service, entity_id, locale)
                except Exception as error:
                    details = {"entityId": entity_id, "locale": locale, "exception": type(error).__name__, "traceback": traceback.format_exc()}
                    logger.exception("unhandled hydration failure entity_id=%s locale=%s", entity_id, locale)
                    self.db.execute("UPDATE metadata_hydration_requests SET state='error',last_error=?,error_details=?,finished_at=? WHERE entity_id=? AND locale=?", (f"Metadata hydration worker failed: {type(error).__name__}: {error}", json.dumps(details), now(), entity_id, locale))


class JobScheduler:
    """Dispatches every scheduled run on its own worker thread."""

    def __init__(self, library_runtime):
        self.store = JobStore()
        self.library_runtime = library_runtime
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active: set[str] = set()
        self.active_definitions: set[str] = set()
        self.cancel_events: dict[str, threading.Event] = {}
        self.active_lock = threading.RLock()
        self.hydration = MetadataHydrationQueue(self)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.store.ensure_defaults()
        legacy_hydration = self.store.by_key("metadata_hydration")
        if legacy_hydration:
            self.store.db.execute("DELETE FROM job_runs WHERE definition_id=?", (legacy_hydration["id"],))
            self.store.db.execute("DELETE FROM job_definitions WHERE id=?", (legacy_hydration["id"],))
        for library in self.library_runtime.store.list():
            self.store.ensure_library(library)
        self._recover_active_runs()
        self.stop_event.clear()
        self.hydration.start()
        self.thread = threading.Thread(target=self._dispatch, name="zenstream-job-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.hydration.stop()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=5)

    def refresh_library_definition(self, library: dict) -> dict:
        definition = self.store.ensure_library(library)
        values = {"intervalMinutes": library.get("scanIntervalMinutes"), "enabled": library.get("watchEnabled", True), "config": {"libraryId": library["id"]}}
        return self.store.update_definition(definition["id"], values)

    def remove_library_definition(self, library_id: str):
        definition = self.store.by_key(f"library_scan:{library_id}")
        if definition:
            self.store.db.execute("DELETE FROM job_definitions WHERE id=?", (definition["id"],))

    def run_now(self, definition_id: str) -> dict:
        definition = self.store.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        if definition["kind"] == "library_scan":
            library_id = (definition.get("config") or {}).get("libraryId")
            job = self.library_runtime.enqueue(library_id, "scan")
            self.store.db.execute("UPDATE job_definitions SET last_state=?,last_run_at=?,last_message=?,updated_at=? WHERE id=?", (job["state"], now(), job.get("message") or "Library scan queued", now(), definition_id))
            return job
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_metadata_hydration(self, entity_ids: list[str], locale: str) -> dict:
        return self.hydration.enqueue(entity_ids, locale)

    def enqueue_metadata_refresh(self) -> dict:
        definition = self.store.by_key("metadata_refresh")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("metadata_refresh")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def terminate(self, definition_id: str, run_id: str) -> dict | None:
        runs = [run for run in self.store.runs(definition_id, 100) if run["id"] == run_id]
        if not runs:
            return None
        run = runs[0]
        if run["state"] not in {"queued", "running", "terminating"}:
            return run
        with self.active_lock:
            cancel_event = self.cancel_events.get(run_id)
            if cancel_event:
                cancel_event.set()
                self.store.update_run(run_id, state="terminating", message="Termination requested")
            else:
                self.store.update_run(run_id, state="terminated", message="Terminated by administrator", error=None, finished_at=now())
        with self.condition:
            self.condition.notify_all()
        return next((value for value in self.store.runs(definition_id, 100) if value["id"] == run_id), None)

    def _recover_active_runs(self) -> None:
        """Resume one interrupted run per task and terminate stale duplicates."""
        rows = self.store.db.execute("SELECT id,definition_id,state FROM job_runs WHERE state IN ('queued','running','terminating') ORDER BY created_at DESC")
        by_definition: dict[str, list[tuple[str, str]]] = {}
        for run_id, definition_id, state in rows:
            by_definition.setdefault(definition_id, []).append((run_id, state))
        timestamp = now()
        with self.store.db.transaction() as cursor:
            for runs in by_definition.values():
                resumable = [run for run in runs if run[1] != "terminating"]
                keep_id = resumable[0][0] if resumable else None
                for run_id, state in runs:
                    if run_id == keep_id:
                        cursor.execute("UPDATE job_runs SET state='queued',progress_current=0,progress_total=0,message='Queued again after Orchestrator restart',error=NULL,started_at=NULL,finished_at=NULL,thread_name=NULL WHERE id=?", (run_id,))
                    else:
                        cursor.execute("UPDATE job_runs SET state='terminated',message='Superseded by the active task run',error=NULL,finished_at=? WHERE id=?", (timestamp, run_id))

    def _schedule_due(self):
        for definition in self.store.due():
            if self.store.queued_or_running(definition["id"]):
                continue
            if definition["kind"] == "library_scan":
                library_id = (definition.get("config") or {}).get("libraryId")
                if library_id:
                    self.library_runtime.enqueue(library_id, "scan")
                self.store.mark_scheduled(definition["id"], None, "Library scan queued")
            else:
                run, created = self.store.create_or_get_active_run(definition)
                if not created:
                    continue
                self.store.mark_scheduled(definition["id"], run["id"])

    def _dispatch(self):
        while not self.stop_event.is_set():
            self._schedule_due()
            queued = self.store.runs(limit=1000)
            for run in queued:
                if run["state"] != "queued":
                    continue
                with self.active_lock:
                    if run["id"] in self.active or run["definitionId"] in self.active_definitions:
                        continue
                    self.active.add(run["id"])
                    self.active_definitions.add(run["definitionId"])
                    self.cancel_events[run["id"]] = threading.Event()
                    with self.store.db.transaction() as cursor:
                        cursor.execute("UPDATE job_runs SET state='running',started_at=?,thread_name=?,message='Starting task' WHERE id=? AND state='queued'", (now(), f"zenstream-job-{run['id'][:8]}", run["id"]))
                        claimed = cursor.rowcount == 1
                    if not claimed:
                        self.active.discard(run["id"])
                        self.active_definitions.discard(run["definitionId"])
                        self.cancel_events.pop(run["id"], None)
                        continue
                thread = threading.Thread(target=self._execute, args=(run["id"],), name=f"zenstream-job-{run['id'][:8]}", daemon=True)
                thread.start()
            with self.condition:
                self.condition.wait(timeout=1)

    def _execute(self, run_id: str):
        try:
            rows = self.store.db.execute("SELECT r.id,r.definition_id,d.kind,d.config,d.name FROM job_runs r JOIN job_definitions d ON d.id=r.definition_id WHERE r.id=?", (run_id,))
            if not rows:
                return
            _, definition_id, kind, config_text, name = rows[0]
            try:
                config = json.loads(config_text or "{}")
            except json.JSONDecodeError:
                config = {}
            definition = self.store.definition(definition_id) or {"id": definition_id, "kind": kind, "config": config, "name": name}
            if kind == "metadata_refresh":
                MetadataRefreshJob(self.store).run(run_id, definition, self.cancel_events[run_id].is_set)
            else:
                self.store.update_run(run_id, state="failed", error=f"Unsupported job kind: {kind}", finished_at=now())
        except JobTerminated:
            self.store.update_run(run_id, state="terminated", message="Terminated by administrator", error=None, finished_at=now())
        except Exception as error:
            details = {"operation": "scheduled_job", "runId": run_id, "exception": type(error).__name__, "traceback": traceback.format_exc()}
            logger.exception("scheduled job failed run_id=%s", run_id)
            self.store.update_run(run_id, state="failed", error=f"{type(error).__name__}: {error}", error_details=json.dumps(details), finished_at=now())
        finally:
            with self.active_lock:
                self.active.discard(run_id)
                self.cancel_events.pop(run_id, None)
                row = self.store.db.execute("SELECT definition_id FROM job_runs WHERE id=?", (run_id,))
                if row:
                    self.active_definitions.discard(row[0][0])


scheduler = JobScheduler(library_runtime)
