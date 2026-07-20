"""Persistent scheduler definitions and non-blocking background job execution."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.config import Config
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
            config = json.loads(row[8] or "{}")
        except json.JSONDecodeError:
            config = {}
        return {
            "id": row[0], "key": row[1], "name": row[2], "description": row[3],
            "kind": row[4], "intervalMinutes": row[5], "enabled": bool(row[6]),
            "config": config, "nextRunAt": row[9], "lastRunAt": row[10],
            "lastRunId": row[11], "lastState": row[12], "lastMessage": row[13],
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
        return self.ensure(f"library_scan:{library['id']}", f"Scan {library['name']}", "Index the library without moving or renaming files.", "library_scan", library.get("scanIntervalMinutes") or 1440, {"libraryId": library["id"]}, library.get("watchEnabled", True))

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

    def queued_or_running(self, definition_id: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM job_runs WHERE definition_id=? AND state IN ('queued','running') LIMIT 1", (definition_id,)))

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

    def run(self, run_id: str, definition: dict) -> None:
        locales = [str(locale).strip() for locale in (definition.get("config") or {}).get("locales", ["en"]) if str(locale).strip()]
        try:
            locales.extend(str(row[0]).strip() for row in self.db.execute("SELECT DISTINCT locale FROM user_preferences WHERE locale IS NOT NULL") if str(row[0]).strip())
        except Exception:
            pass
        locales = list(dict.fromkeys(locales))
        locales = locales or ["en"]
        rows = self.db.execute("SELECT DISTINCT e.id,e.entity_type,p.provider,p.provider_id FROM library_entities e JOIN entity_provider_ids p ON p.entity_id=e.id WHERE p.provider IN ('tmdb','tvdb','musicbrainz') ORDER BY e.id")
        items = {}
        for entity_id, entity_type, provider, provider_id in rows:
            items.setdefault((entity_id, entity_type), []).append((provider, provider_id))
        self.store.update_run(run_id, state="running", started_at=now(), thread_name=threading.current_thread().name, progress_total=len(items), message="Refreshing provider metadata")
        service = MetadataService()
        completed = 0
        for (entity_id, entity_type), identifiers in items.items():
            for locale in locales:
                priorities = {"series": ["tvdb", "tmdb"], "episode": ["tvdb", "tmdb"], "season": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"], "collection": ["tvdb"], "artist": ["musicbrainz"], "release": ["musicbrainz"], "track": ["musicbrainz"]}.get(entity_type, [])
                ordered = sorted(identifiers, key=lambda value: priorities.index(value[0]) if value[0] in priorities else 99)
                for provider, provider_id in ordered:
                    try:
                        service.fetch(provider, entity_type, provider_id, locale)
                    except ProviderError:
                        continue
            completed += 1
            if completed % 10 == 0 or completed == len(items):
                self.store.update_run(run_id, progress_current=completed, message=f"Refreshed {completed} of {len(items)} entities")
        self.store.update_run(run_id, state="completed", progress_current=completed, progress_total=len(items), finished_at=now(), message=f"Refreshed {completed} entities")


class JobScheduler:
    """Dispatches every scheduled run on its own worker thread."""

    def __init__(self, library_runtime):
        self.store = JobStore()
        self.library_runtime = library_runtime
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active: set[str] = set()
        self.active_lock = threading.RLock()
        self.max_workers = 4

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.store.ensure_defaults()
        for library in self.library_runtime.store.list():
            self.store.ensure_library(library)
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
            self.store.db.execute("UPDATE job_definitions SET last_state='queued',last_run_at=?,last_message=?,updated_at=? WHERE id=?", (now(), "Library scan queued", now(), definition_id))
            return job
        run = self.store.create_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

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
                run = self.store.create_run(definition)
                self.store.mark_scheduled(definition["id"], run["id"])

    def _dispatch(self):
        while not self.stop_event.is_set():
            self._schedule_due()
            with self.active_lock:
                capacity = self.max_workers - len(self.active)
            if capacity > 0:
                queued = self.store.runs(limit=capacity * 2)
                for run in queued:
                    if run["state"] != "queued":
                        continue
                    with self.active_lock:
                        if len(self.active) >= self.max_workers or run["id"] in self.active:
                            break
                        self.active.add(run["id"])
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
                MetadataRefreshJob(self.store).run(run_id, definition)
            else:
                self.store.update_run(run_id, state="failed", error=f"Unsupported job kind: {kind}", finished_at=now())
        except Exception as error:
            self.store.update_run(run_id, state="failed", error=str(error), finished_at=now())
        finally:
            with self.active_lock:
                self.active.discard(run_id)


from app.library import runtime as library_runtime

scheduler = JobScheduler(library_runtime)
