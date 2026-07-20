"""Persistent scheduler definitions and non-blocking background job execution."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.library import JobTerminated, runtime as library_runtime
from app.providers import ProviderError, MetadataService


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
            "message": row[7], "error": row[8], "createdAt": row[9],
            "startedAt": row[10], "finishedAt": row[11], "threadName": row[12],
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
            rows = self.db.execute("SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,created_at,started_at,finished_at,thread_name FROM job_runs WHERE definition_id=? ORDER BY created_at DESC LIMIT ?", (definition_id, limit))
        else:
            rows = self.db.execute("SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,created_at,started_at,finished_at,thread_name FROM job_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._run(row) for row in rows]

    def library_runs(self, library_id: str, limit: int = 10) -> list[dict]:
        rows = self.db.execute("SELECT id,library_id,kind,state,progress_current,progress_total,message,error,created_at,started_at,finished_at FROM library_jobs WHERE library_id=? ORDER BY created_at DESC LIMIT ?", (library_id, limit))
        return [{"id": row[0], "definitionId": None, "libraryId": row[1], "kind": row[2], "state": row[3], "progressCurrent": row[4], "progressTotal": row[5], "message": row[6], "error": row[7], "createdAt": row[8], "startedAt": row[9], "finishedAt": row[10], "threadName": None} for row in rows]

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
        allowed = {"state", "progress_current", "progress_total", "message", "error", "started_at", "finished_at", "thread_name"}
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
        rows = self.db.execute(
            "SELECT DISTINCT provider,entity_type,provider_id,locale FROM metadata_cache "
            "WHERE provider IN ('tmdb','tvdb','musicbrainz') ORDER BY provider,entity_type,provider_id,locale"
        )
        items = {}
        for provider, entity_type, provider_id, locale in rows:
            items.setdefault((entity_type, provider_id, locale), []).append(provider)
        self.store.update_run(run_id, state="running", started_at=now(), thread_name=threading.current_thread().name, progress_total=len(items), message="Refreshing provider metadata")
        service = MetadataService()
        completed = 0
        for (entity_type, provider_id, locale), providers in items.items():
            if should_terminate():
                raise JobTerminated()
            for provider in providers:
                try:
                    service.fetch(provider, entity_type, provider_id, locale, force=True)
                except (ProviderError, ValueError):
                    continue
            completed += 1
            if completed % 10 == 0 or completed == len(items):
                self.store.update_run(run_id, progress_current=completed, message=f"Refreshed {completed} of {len(items)} entities")
        self.store.update_run(run_id, state="completed", progress_current=completed, progress_total=len(items), finished_at=now(), message=f"Refreshed {completed} entities")


class MetadataHydrationJob:
    """Fetch only explicitly requested localized metadata in the background."""

    def __init__(self, store: JobStore):
        self.store = store
        self.db = store.db

    def run(self, run_id: str, definition: dict, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        rows = self.db.execute(
            "SELECT entity_id,locale FROM metadata_hydration_requests WHERE state='queued' ORDER BY requested_at LIMIT 500"
        )
        self.store.update_run(run_id, state="running", started_at=now(), thread_name=threading.current_thread().name, progress_total=len(rows), message="Hydrating requested metadata")
        service = MetadataService()
        completed = 0
        for entity_id, locale in rows:
            if should_terminate():
                raise JobTerminated()
            self.db.execute("UPDATE metadata_hydration_requests SET state='running',attempts=attempts+1,started_at=?,last_error=NULL WHERE entity_id=? AND locale=?", (now(), entity_id, locale))
            entity_rows = self.db.execute("SELECT entity_type FROM library_entities WHERE id=?", (entity_id,))
            if not entity_rows:
                self.db.execute("UPDATE metadata_hydration_requests SET state='error',last_error=?,finished_at=? WHERE entity_id=? AND locale=?", ("Library entity no longer exists", now(), entity_id, locale))
                continue
            entity_type = entity_rows[0][0]
            provider_rows = self.db.execute("SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC,provider", (entity_id,))
            succeeded = False
            errors = []
            priorities = {"series": ["tvdb", "tmdb"], "episode": ["tvdb", "tmdb"], "season": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"], "collection": ["tvdb"], "artist": ["musicbrainz"], "release": ["musicbrainz"], "track": ["musicbrainz"]}.get(entity_type, [])
            ordered = sorted(provider_rows, key=lambda value: priorities.index(value[0]) if value[0] in priorities else 99)
            for provider, provider_id in ordered:
                try:
                    service.fetch(provider, entity_type, provider_id, locale, force=False)
                    succeeded = True
                except (ProviderError, ValueError) as error:
                    errors.append(f"{provider}: {error}")
            state = "ready" if succeeded else "error"
            self.db.execute("UPDATE metadata_hydration_requests SET state=?,last_error=?,finished_at=? WHERE entity_id=? AND locale=?", (state, "; ".join(errors) if errors else None, now(), entity_id, locale))
            completed += 1
            self.store.update_run(run_id, progress_current=completed, message=f"Hydrated {completed} of {len(rows)} entities")
        self.store.update_run(run_id, state="completed", progress_current=completed, progress_total=len(rows), finished_at=now(), message=f"Hydrated {completed} entities")


class MetadataHydrationQueue:
    def __init__(self, scheduler: "JobScheduler"):
        self.scheduler = scheduler

    def enqueue(self, entity_ids: list[str], locale: str) -> dict:
        locale = (locale or "en").strip().lower()
        values = list(dict.fromkeys(str(value) for value in entity_ids if str(value).strip()))
        queued = 0
        with self.scheduler.store.db.transaction() as cursor:
            for entity_id in values:
                cursor.execute("SELECT state FROM metadata_hydration_requests WHERE entity_id=? AND locale=?", (entity_id, locale))
                existing = cursor.fetchone()
                if existing and existing[0] in {"queued", "running"}:
                    continue
                timestamp = now()
                cursor.execute(
                    "INSERT INTO metadata_hydration_requests(entity_id,locale,state,attempts,last_error,requested_at,started_at,finished_at) VALUES(?,?, 'queued',0,NULL,?,?,NULL) "
                    "ON CONFLICT(entity_id,locale) DO UPDATE SET state='queued',last_error=NULL,requested_at=excluded.requested_at,started_at=NULL,finished_at=NULL",
                    (entity_id, locale, timestamp, None),
                )
                queued += 1
        definition = self.scheduler.store.by_key("metadata_hydration")
        if not definition:
            definition = self.scheduler.store.ensure("metadata_hydration", "Hydrate requested metadata", "Fetch localized metadata requested by the administrator dashboard.", "metadata_hydration", 43200, {}, False)
        run, _ = self.scheduler.store.create_or_get_active_run(definition)
        with self.scheduler.condition:
            self.scheduler.condition.notify_all()
        return {"jobId": run["id"], "locale": locale, "requested": len(values), "queued": queued}


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
        self.store.ensure("metadata_hydration", "Hydrate requested metadata", "Fetch localized metadata requested by the administrator dashboard.", "metadata_hydration", 43200, {}, False)
        for library in self.library_runtime.store.list():
            self.store.ensure_library(library)
        self._recover_active_runs()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._dispatch, name="zenstream-job-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
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
            elif kind == "metadata_hydration":
                MetadataHydrationJob(self.store).run(run_id, definition, self.cancel_events[run_id].is_set)
            else:
                self.store.update_run(run_id, state="failed", error=f"Unsupported job kind: {kind}", finished_at=now())
        except JobTerminated:
            self.store.update_run(run_id, state="terminated", message="Terminated by administrator", error=None, finished_at=now())
        except Exception as error:
            self.store.update_run(run_id, state="failed", error=str(error), finished_at=now())
        finally:
            with self.active_lock:
                self.active.discard(run_id)
                self.cancel_events.pop(run_id, None)
                row = self.store.db.execute("SELECT definition_id FROM job_runs WHERE id=?", (run_id,))
                if row:
                    self.active_definitions.discard(row[0][0])


scheduler = JobScheduler(library_runtime)
