from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Config
from app.foreground import active_requests
from app.intro_outro import IntroOutroDetector
from app.library import JobTerminated
from app.library import runtime as library_runtime
from app.library_cleanup import cleanup_orphans
from app.logging_config import get_logger
from app.metadata_domain import choose_artwork
from app.metadata_services import (
    FACT_FIELDS,
    TEXT_FIELDS,
    MetadataIngestService,
    metadata_task_results,
)
from app.providers import ProviderError
from app.trickplay import TrickplayExtractor

logger = get_logger("jobs")
VIDEO_ENTITY_TYPES = {"movie", "series", "season", "episode"}
ARTWORK_TYPES = {"Primary", "Backdrop", "Logo", "Banner"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def _usable_metadata_value(value) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _ready_cache_path(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _metadata_document_gaps(
    db,
    provider: str,
    entity_type: str,
    provider_id: str,
    locale: str,
    document: dict,
) -> tuple[set[str], list[tuple[str, str]]]:
    gaps: set[str] = set()
    linked = db.execute(
        "SELECT ep.entity_id,e.library_id,ep.is_primary FROM entity_provider_ids ep "
        "JOIN library_entities e ON e.id=ep.entity_id "
        "WHERE ep.provider=? AND ep.identifier_type=? AND ep.provider_id=?",
        (provider, entity_type, provider_id),
    )
    entity_libraries = [(row[0], row[1]) for row in linked]
    if not entity_libraries:
        return {"identity:orphaned"}, []

    projected_fields = TEXT_FIELDS | FACT_FIELDS
    source_images = {
        (str(image.get("type")), str(image.get("url")))
        for image in document.get("images", [])
        if isinstance(image, dict)
        and image.get("type") in ARTWORK_TYPES
        and isinstance(image.get("url"), str)
        and image.get("url")
    }
    image_columns = {row[1] for row in db.execute("PRAGMA table_info(metadata_images)")}
    blur_hash_column = ",blur_hash" if "blur_hash" in image_columns else ""
    image_rows = db.execute(
        "SELECT image_type,image_url,local_path" + blur_hash_column + " "
        "FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=?",
        (provider, entity_type, provider_id),
    )
    ready_images = {
        (str(image_type), str(image_url))
        for image_type, image_url, local_path, *_rest in image_rows
        if _ready_cache_path(local_path)
    }
    ready_hashes = {
        (str(row[0]), str(row[1])): str(row[3]).strip()
        for row in image_rows
        if len(row) > 3 and _ready_cache_path(row[2]) and row[3]
    }
    for image_type, image_url in source_images - ready_images:
        gaps.add(f"artwork:{image_type}")

    expected_credit_records = []
    credits = document.get("credits")
    if isinstance(credits, dict):
        for credit_type in ("cast", "crew"):
            values = credits.get(credit_type)
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict) and str(value.get("name") or "").strip():
                    expected_credit_records.append((credit_type, value))

    for entity_id, _library_id, is_primary in linked:
        if not is_primary:
            continue
        projection_rows = db.execute(
            "SELECT payload FROM catalog_item_projection WHERE entity_id=? AND locale=?",
            (entity_id, locale),
        )
        projection = {}
        if projection_rows:
            try:
                projection = json.loads(projection_rows[0][0] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                projection = {}
        if not isinstance(projection, dict):
            projection = {}
        if not projection_rows:
            gaps.add("projection")
        # A season can have its provider identity and episode children while
        # the season details request was interrupted (or returned a partial
        # response).  Do not treat the synthesized catalog label ("Season N")
        # as a provider title: the missing-metadata job must retry the
        # provider document so a localized season name can be materialized.
        if (
            provider == "tvdb"
            and entity_type == "season"
            and not _usable_metadata_value(document.get("title"))
        ):
            gaps.add("metadata:title")
        for field in projected_fields:
            source_value = document.get(field)
            if (
                _usable_metadata_value(source_value)
                and projection.get(field) != source_value
            ):
                gaps.add(f"metadata:{field}")
        projected_images = projection.get("images")
        if not isinstance(projected_images, dict):
            projected_images = {}
        for image_type in ARTWORK_TYPES:
            expected = choose_artwork(
                document.get("images", []),
                locale,
                image_type,
                document.get("originalLanguage"),
                [provider],
            )
            if expected and image_type not in projected_images:
                gaps.add(f"projection-artwork:{image_type}")
            elif (
                expected
                and image_type != "Logo"
                and image_type in projected_images
                and ready_hashes.get((image_type, str(expected.get("url"))))
                and not str(
                    (
                        projected_images.get(image_type)
                        if isinstance(projected_images.get(image_type), dict)
                        else {}
                    ).get("blurHash")
                    or ""
                ).strip()
            ):
                gaps.add(f"projection-artwork-blurhash:{image_type}")

        if (
            is_primary
            and provider in {"tmdb", "tvdb"}
            and entity_type in VIDEO_ENTITY_TYPES
            and expected_credit_records
        ):
            actual_credit_count = db.execute(
                "SELECT COUNT(*) FROM entity_person_credits WHERE entity_id=? AND provider=? AND locale=?",
                (entity_id, provider, locale),
            )[0][0]
            if int(actual_credit_count) != len(expected_credit_records):
                gaps.add("credits")

    if expected_credit_records and any(bool(row[2]) for row in linked):
        expected_portraits = {
            str(record.get("id")): record.get("imageUrl")
            for _credit_type, record in expected_credit_records
            if str(record.get("id") or "").strip()
            and isinstance(record.get("imageUrl"), str)
            and record.get("imageUrl")
        }
        people_by_id = {}
        person_ids = sorted(expected_portraits)
        for offset in range(0, len(person_ids), 400):
            batch = person_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            people_by_id.update(
                {
                    person_id: (image_url, local_path)
                    for person_id, image_url, local_path in db.execute(
                        f"SELECT provider_person_id,image_url,local_path FROM people "
                        f"WHERE provider=? AND provider_person_id IN ({placeholders})",
                        (provider, *batch),
                    )
                }
            )
        for person_id, image_url in expected_portraits.items():
            person = people_by_id.get(person_id)
            if (
                person is None
                or person[0] != image_url
                or not _ready_cache_path(person[1])
            ):
                gaps.add("portrait")
                break
    return gaps, entity_libraries


def _repair_missing_tv_child_identities(db, metadata_service) -> int:
    """Restore child provider IDs left behind by an interrupted TV scan.

    A scan can persist the series identity before the process is restarted,
    while the season/episode identity pass is still pending.  The missing
    metadata job must repair that durable gap before selecting provider
    documents; otherwise those children are invisible to the job forever.
    """
    entity_columns = {
        row[1] for row in db.execute("PRAGMA table_info(library_entities)")
    }
    if not {"parent_id", "season_number", "episode_number"} <= entity_columns:
        return 0

    child_identity_rows = db.execute(
        "SELECT child.id,child.entity_type,child.season_number,child.episode_number,"
        "CASE WHEN child.entity_type='season' THEN child.parent_id "
        "ELSE season.parent_id END "
        "FROM library_entities child "
        "LEFT JOIN library_entities season ON season.id=child.parent_id "
        "WHERE child.entity_type='season' AND child.parent_id IN "
        "(SELECT id FROM library_entities WHERE entity_type='series') "
        "OR child.entity_type='episode' AND season.entity_type='season' "
        "AND season.parent_id IN "
        "(SELECT id FROM library_entities WHERE entity_type='series') "
        "ORDER BY child.parent_id,child.entity_type,child.season_number,child.episode_number"
    )
    if not child_identity_rows:
        return 0

    series_rows = db.execute(
        "SELECT e.id,p.provider,p.provider_id "
        "FROM library_entities e JOIN entity_provider_ids p ON p.entity_id=e.id "
        "WHERE e.entity_type='series' AND p.identifier_type='series' "
        "AND p.provider IN ('tmdb','tvdb') ORDER BY e.id,p.provider"
    )
    children_by_series: dict[str, list[tuple]] = {}
    for row in child_identity_rows:
        child_id, entity_type, season_number, episode_number, series_id = row
        children_by_series.setdefault(series_id, []).append(
            (child_id, entity_type, season_number, episode_number)
        )

    existing = {
        (entity_id, provider, identifier_type)
        for entity_id, provider, identifier_type in db.execute(
            "SELECT entity_id,provider,identifier_type FROM entity_provider_ids "
            "WHERE provider IN ('tmdb','tvdb')"
        )
    }
    repaired = 0
    for series_id, provider, series_provider_id in series_rows:
        children = children_by_series.get(series_id, [])
        if not children:
            continue
        missing = [
            child
            for child in children
            if (child[0], provider, child[1]) not in existing
        ]
        if not missing:
            continue

        provider_ids: dict[tuple[int, int | None], str] = {}
        if provider == "tmdb":
            for _child_id, entity_type, season_number, episode_number in missing:
                if season_number is None:
                    continue
                key = (int(season_number), None)
                provider_ids[key] = f"{series_provider_id}:{season_number}"
                if entity_type == "episode" and episode_number is not None:
                    provider_ids[(int(season_number), int(episode_number))] = (
                        f"{series_provider_id}:{season_number}:{episode_number}"
                    )
        else:
            discover = getattr(metadata_service, "series_child_ids", None)
            if not callable(discover):
                continue
            try:
                hierarchy = discover("tvdb", str(series_provider_id)) or {}
            except Exception as error:
                logger.warning(
                    "missing metadata child identity discovery failed series_id=%s provider_id=%s: %s",
                    series_id,
                    series_provider_id,
                    error,
                )
                continue
            for value in hierarchy.get("seasons", []) or []:
                if value.get("seasonNumber") is not None and value.get("providerId"):
                    provider_ids[(int(value["seasonNumber"]), None)] = str(
                        value["providerId"]
                    )
            for value in hierarchy.get("episodes", []) or []:
                if (
                    value.get("seasonNumber") is not None
                    and value.get("episodeNumber") is not None
                    and value.get("providerId")
                ):
                    provider_ids[
                        (int(value["seasonNumber"]), int(value["episodeNumber"]))
                    ] = str(value["providerId"])

        for child_id, entity_type, season_number, episode_number in missing:
            if season_number is None:
                continue
            key = (
                (int(season_number), int(episode_number))
                if entity_type == "episode" and episode_number is not None
                else (int(season_number), None)
            )
            provider_id = provider_ids.get(key)
            if not provider_id:
                continue
            db.execute(
                "INSERT OR IGNORE INTO entity_provider_ids "
                "(entity_id,provider,identifier_type,provider_id,is_primary) "
                "VALUES(?,?,?,?,?)",
                (child_id, provider, entity_type, provider_id, int(provider == "tvdb")),
            )
            existing.add((child_id, provider, entity_type))
            repaired += 1
            if "match_status" in entity_columns:
                db.execute(
                    "UPDATE library_entities SET match_status='matched',"
                    "match_confidence=1.0,match_method='parent_resolution',updated_at=? "
                    "WHERE id=?",
                    (now(), child_id),
                )
    if repaired:
        logger.info("repaired missing TV child provider identities count=%s", repaired)
    return repaired


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
            "id": row[0],
            "key": row[1],
            "name": row[2],
            "description": row[3],
            "kind": row[4],
            "intervalMinutes": row[5],
            "enabled": bool(row[6]),
            "config": config,
            "nextRunAt": row[8],
            "lastRunAt": row[9],
            "lastRunId": row[10],
            "lastState": row[11],
            "lastMessage": row[12],
            "createdAt": row[13],
            "updatedAt": row[14],
        }

    @staticmethod
    def _run(row) -> dict:
        return {
            "id": row[0],
            "definitionId": row[1],
            "libraryId": row[2],
            "kind": row[3],
            "state": row[4],
            "progressCurrent": row[5],
            "progressTotal": row[6],
            "message": row[7],
            "error": row[8],
            "errorDetails": row[9],
            "createdAt": row[10],
            "startedAt": row[11],
            "finishedAt": row[12],
            "threadName": row[13],
        }

    def definitions(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions ORDER BY name COLLATE NOCASE"
        )
        return [self._definition(row) for row in rows]

    def definition(self, definition_id: str) -> dict | None:
        rows = self.db.execute(
            "SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions WHERE id=?",
            (definition_id,),
        )
        return self._definition(rows[0]) if rows else None

    def by_key(self, key: str) -> dict | None:
        rows = self.db.execute(
            "SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions WHERE job_key=?",
            (key,),
        )
        return self._definition(rows[0]) if rows else None

    def ensure(
        self,
        key: str,
        name: str,
        description: str,
        kind: str,
        interval: int = 1440,
        config: dict | None = None,
        enabled: bool = True,
    ) -> dict:
        existing = self.by_key(key)
        if existing:
            return existing
        timestamp = now()
        next_run = (
            datetime.now(timezone.utc)
            + timedelta(minutes=max(5, min(43200, int(interval or 1440))))
        ).isoformat()
        definition_id = new_id()
        self.db.execute(
            "INSERT INTO job_definitions(id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                definition_id,
                key,
                name,
                description,
                kind,
                max(5, min(43200, int(interval or 1440))),
                int(enabled),
                json.dumps(config or {}, ensure_ascii=False),
                next_run,
                timestamp,
                timestamp,
            ),
        )
        return self.definition(definition_id)  # type: ignore[return-value]

    def ensure_defaults(self) -> None:
        definition = self.ensure(
            "metadata_missing",
            "Find missing metadata",
            "Fetch missing provider metadata, artwork, and credits for indexed IDs.",
            "metadata_missing",
            1440,
            {"locales": ["en"], "batchSize": 50},
        )
        if definition["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), definition["id"]),
            )
        cleanup = self.ensure(
            "metadata_cleanup",
            "Clean orphaned library data",
            "Remove deleted-library inventory, metadata, and cached artwork leftovers.",
            "metadata_cleanup",
            10080,
            {},
        )
        if cleanup["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), cleanup["id"]),
            )
        trickplay = self.ensure(
            "trickplay_extract",
            "Extract trickplay sheets",
            "Generate cached sprite sheets for indexed video sources.",
            "trickplay_extract",
            60,
            {},
        )
        if trickplay["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), trickplay["id"]),
            )
        intro_outro = self.ensure(
            "intro_outro_detect",
            "Detect intros and outros",
            "Compare cached audio fingerprints for unscanned TV episodes.",
            "intro_outro_detect",
            60,
            {},
        )
        if intro_outro["lastRunAt"] is None:
            self.db.execute(
                "UPDATE job_definitions SET next_run_at=?,updated_at=? WHERE id=?",
                (now(), now(), intro_outro["id"]),
            )

    def ensure_library(self, library: dict) -> dict:
        description = "Index the library without moving or renaming files."
        definition = self.ensure(
            f"library_scan:{library['id']}",
            f"Scan {library['name']}",
            description,
            "library_scan",
            library.get("scanIntervalMinutes") or 1440,
            {"libraryId": library["id"]},
            library.get("watchEnabled", True),
        )
        # Repair older definitions whose config was lost by the former row mapper,
        # while preserving task-level interval and enabled settings.
        self.db.execute(
            "UPDATE job_definitions SET name=?,description=?,config=?,updated_at=? WHERE id=?",
            (
                f"Scan {library['name']}",
                description,
                json.dumps({"libraryId": library["id"]}),
                now(),
                definition["id"],
            ),
        )
        return self.definition(definition["id"])  # type: ignore[return-value]

    def update_definition(self, definition_id: str, values: dict) -> dict:
        definition = self.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        interval = max(
            5,
            min(
                43200,
                int(
                    values.get("intervalMinutes", definition["intervalMinutes"]) or 1440
                ),
            ),
        )
        enabled = int(bool(values.get("enabled", definition["enabled"])))
        name = str(values.get("name", definition["name"])).strip() or definition["name"]
        config = values.get("config", definition["config"])
        self.db.execute(
            "UPDATE job_definitions SET name=?,interval_minutes=?,enabled=?,config=?,next_run_at=?,updated_at=? WHERE id=?",
            (
                name,
                interval,
                enabled,
                json.dumps(config or {}, ensure_ascii=False),
                (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat()
                if enabled
                else None,
                now(),
                definition_id,
            ),
        )
        return self.definition(definition_id)  # type: ignore[return-value]

    def runs(self, definition_id: str | None = None, limit: int = 100) -> list[dict]:
        if definition_id:
            rows = self.db.execute(
                "SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,thread_name FROM job_runs WHERE definition_id=? ORDER BY created_at DESC LIMIT ?",
                (definition_id, limit),
            )
        else:
            rows = self.db.execute(
                "SELECT id,definition_id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at,thread_name FROM job_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [self._run(row) for row in rows]

    def library_runs(self, library_id: str, limit: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,library_id,kind,state,progress_current,progress_total,message,error,error_details,created_at,started_at,finished_at FROM library_jobs WHERE library_id=? ORDER BY created_at DESC LIMIT ?",
            (library_id, limit),
        )
        return [
            {
                "id": row[0],
                "definitionId": None,
                "libraryId": row[1],
                "kind": row[2],
                "state": row[3],
                "progressCurrent": row[4],
                "progressTotal": row[5],
                "message": row[6],
                "error": row[7],
                "errorDetails": row[8],
                "createdAt": row[9],
                "startedAt": row[10],
                "finishedAt": row[11],
                "threadName": None,
            }
            for row in rows
        ]

    def create_run(self, definition: dict) -> dict:
        run_id = new_id()
        library_id = (definition.get("config") or {}).get("libraryId")
        timestamp = now()
        self.db.execute(
            "INSERT INTO job_runs(id,definition_id,library_id,kind,created_at) VALUES(?,?,?,?,?)",
            (run_id, definition["id"], library_id, definition["kind"], timestamp),
        )
        self.db.execute(
            "UPDATE job_definitions SET last_state='queued',last_message=?,updated_at=? WHERE id=?",
            ("Queued", timestamp, definition["id"]),
        )
        return self.runs(definition["id"], 1)[0]

    def create_or_get_active_run(self, definition: dict) -> tuple[dict, bool]:
        """Atomically keep at most one queued/running run for a task definition."""
        timestamp = now()
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT id FROM job_runs WHERE definition_id=? AND state IN ('queued','running','terminating') ORDER BY created_at DESC LIMIT 1",
                (definition["id"],),
            )
            existing = cursor.fetchone()
            if existing:
                run_id = existing[0]
                created = False
            else:
                run_id = new_id()
                library_id = (definition.get("config") or {}).get("libraryId")
                cursor.execute(
                    "INSERT INTO job_runs(id,definition_id,library_id,kind,created_at) VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        definition["id"],
                        library_id,
                        definition["kind"],
                        timestamp,
                    ),
                )
                cursor.execute(
                    "UPDATE job_definitions SET last_state='queued',last_message=?,updated_at=? WHERE id=?",
                    ("Queued", timestamp, definition["id"]),
                )
                created = True
        runs = [run for run in self.runs(definition["id"], 100) if run["id"] == run_id]
        return runs[0], created

    def queued_or_running(self, definition_id: str) -> bool:
        return bool(
            self.db.execute(
                "SELECT 1 FROM job_runs WHERE definition_id=? AND state IN ('queued','running','terminating') LIMIT 1",
                (definition_id,),
            )
        )

    def due(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,job_key,name,description,kind,interval_minutes,enabled,config,next_run_at,last_run_at,last_run_id,last_state,last_message,created_at,updated_at FROM job_definitions WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at",
            (now(),),
        )
        return [self._definition(row) for row in rows]

    def mark_scheduled(
        self, definition_id: str, run_id: str | None, message: str = "Queued"
    ) -> None:
        definition = self.definition(definition_id)
        if not definition:
            return
        next_run = (
            datetime.now(timezone.utc)
            + timedelta(minutes=definition["intervalMinutes"])
        ).isoformat()
        self.db.execute(
            "UPDATE job_definitions SET next_run_at=?,last_run_at=?,last_run_id=?,last_state='queued',last_message=?,updated_at=? WHERE id=?",
            (next_run, now(), run_id, message, now(), definition_id),
        )

    def update_run(self, run_id: str, **values) -> None:
        allowed = {
            "state",
            "progress_current",
            "progress_total",
            "message",
            "error",
            "error_details",
            "started_at",
            "finished_at",
            "thread_name",
        }
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if updates:
            fields = ",".join(f"{key}=?" for key, _ in updates)
            self.db.execute(
                f"UPDATE job_runs SET {fields} WHERE id=?",
                [value for _, value in updates] + [run_id],
            )
        row = self.db.execute(
            "SELECT definition_id,state,message,error FROM job_runs WHERE id=?",
            (run_id,),
        )
        if row:
            self.db.execute(
                "UPDATE job_definitions SET last_state=?,last_message=?,updated_at=? WHERE id=?",
                (row[0][1], row[0][2] or row[0][3], now(), row[0][0]),
            )


class MetadataMissingJob:
    def __init__(self, store: JobStore):
        self.store = store
        self.db = store.db

    def run(
        self,
        run_id: str,
        definition: dict,
        should_terminate=None,
        force: bool = False,
        force_assets: bool | None = None,
    ) -> None:
        should_terminate = should_terminate or (lambda: False)
        ingest = MetadataIngestService(background_assets=False)
        locales = ingest.locales()
        _repair_missing_tv_child_identities(self.db, ingest.metadata_service)
        config = definition.get("config") or {}
        batch_size = max(1, min(500, int(config.get("batchSize") or 50)))
        has_enrichment_queue = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_queue'"
            )
        )
        rows = self.db.execute(
            "SELECT DISTINCT p.provider,p.identifier_type,p.provider_id "
            "FROM entity_provider_ids p JOIN library_entities e ON e.id=p.entity_id "
            "WHERE p.provider IN ('tmdb','tvdb','musicbrainz') ORDER BY p.provider,e.entity_type,p.provider_id"
        )
        items = list(rows)
        total = len(items) * len(locales)
        self.store.update_run(
            run_id,
            state="running",
            started_at=now(),
            thread_name=threading.current_thread().name,
            progress_total=total,
            message=f"Processing 0/{total} metadata documents",
        )

        def queue_failures(
            provider: str,
            entity_type: str,
            provider_id: str,
            item_failures: list[dict],
        ) -> None:
            if not item_failures or not has_enrichment_queue:
                return
            entity_rows = self.db.execute(
                "SELECT ep.entity_id,e.library_id FROM entity_provider_ids ep "
                "JOIN library_entities e ON e.id=ep.entity_id "
                "WHERE ep.provider=? AND ep.identifier_type=? AND ep.provider_id=?",
                (provider, entity_type, provider_id),
            )
            timestamp = now()
            failures_by_locale = {
                locale: [
                    failure
                    for failure in item_failures
                    if failure.get("locale") == locale
                ]
                for locale in {
                    str(failure.get("locale") or "") for failure in item_failures
                }
            }
            with self.db.transaction() as cursor:
                for entity_id, library_id in entity_rows:
                    for locale, locale_failures in failures_by_locale.items():
                        cursor.execute(
                            "INSERT INTO enrichment_queue(id,entity_id,library_id,kind,locale,priority,state,attempts,next_attempt_at,lease_owner,lease_expires_at,source_job_id,error,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,10,'retry',1,NULL,NULL,NULL,?,?,?,?) "
                            "ON CONFLICT(entity_id,kind,locale) DO UPDATE SET state='retry',priority=MAX(enrichment_queue.priority,excluded.priority),attempts=enrichment_queue.attempts+1,next_attempt_at=NULL,lease_owner=NULL,lease_expires_at=NULL,source_job_id=excluded.source_job_id,error=excluded.error,updated_at=excluded.updated_at",
                            (
                                str(uuid.uuid4()),
                                entity_id,
                                library_id,
                                "metadata",
                                locale,
                                run_id,
                                json.dumps(locale_failures, ensure_ascii=False),
                                timestamp,
                                timestamp,
                            ),
                        )

        def complete_repair(entity_ids: set[str], locale: str) -> None:
            if not entity_ids or not has_enrichment_queue:
                return
            placeholders = ",".join("?" for _ in entity_ids)
            self.db.execute(
                f"UPDATE enrichment_queue SET state='completed',next_attempt_at=NULL,lease_owner=NULL,lease_expires_at=NULL,error=NULL,updated_at=? "
                f"WHERE kind='metadata' AND locale=? AND entity_id IN ({placeholders})",
                (now(), locale, *sorted(entity_ids)),
            )

        def process_item(item):
            provider, entity_type, provider_id = item
            item_failures = []
            fetch_locales = []
            documents: dict[str, dict] = {}
            worked_locales: set[str] = set()
            for locale in locales:
                cached = ingest.metadata_service.cache.get(
                    provider, entity_type, provider_id, locale
                )
                if not cached:
                    fetch_locales.append(locale)
                    continue
                cached = dict(cached)
                cached.pop("_stale", None)
                documents[locale] = cached
                if force:
                    fetch_locales.append(locale)
                    continue
                gaps, _linked = _metadata_document_gaps(
                    self.db,
                    provider,
                    entity_type,
                    provider_id,
                    locale,
                    cached,
                )
                if gaps:
                    # A cache hit is normally replayed locally.  A missing
                    # provider title is different: replaying the same
                    # normalized document can never repair it, so request a
                    # fresh localized document instead.
                    if (
                        provider == "tvdb"
                        and entity_type == "season"
                        and not _usable_metadata_value(cached.get("title"))
                    ):
                        fetch_locales.append(locale)
                    else:
                        ingest.ingest_document(
                            provider, entity_type, provider_id, locale, cached
                        )
                        worked_locales.add(locale)
            if fetch_locales:
                try:
                    fetched = ingest.ingest_locales(
                        provider,
                        entity_type,
                        provider_id,
                        fetch_locales,
                        force=force,
                        force_assets=force_assets,
                    )
                    documents.update(fetched)
                    worked_locales.update(fetch_locales)
                except (ProviderError, ValueError, OSError) as error:
                    item_failures.extend(
                        {
                            "kind": "error",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": locale,
                            "error": f"{type(error).__name__}: {error}",
                        }
                        for locale in fetch_locales
                    )
                    logger.exception(
                        "scheduled missing metadata failed provider=%s entity_type=%s provider_id=%s locales=%s",
                        provider,
                        entity_type,
                        provider_id,
                        fetch_locales,
                    )
            failed_locales = {str(failure.get("locale")) for failure in item_failures}
            publish_ids: set[str] = set()
            for locale in locales:
                if locale in failed_locales:
                    continue
                document = documents.get(locale)
                if not isinstance(document, dict):
                    item_failures.append(
                        {
                            "kind": "incomplete",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": locale,
                            "error": "Metadata document is still missing after repair",
                        }
                    )
                    continue
                document = dict(document)
                document.pop("_stale", None)
                gaps, linked = _metadata_document_gaps(
                    self.db,
                    provider,
                    entity_type,
                    provider_id,
                    locale,
                    document,
                )
                linked_ids = {entity_id for entity_id, _library_id in linked}
                publish_ids.update(linked_ids)
                if gaps:
                    item_failures.append(
                        {
                            "kind": "incomplete",
                            "provider": provider,
                            "entityType": entity_type,
                            "providerId": provider_id,
                            "locale": locale,
                            "missing": sorted(gaps),
                            "error": "Metadata materialization remains incomplete",
                        }
                    )
                else:
                    complete_repair(linked_ids, locale)
            queue_failures(provider, entity_type, provider_id, item_failures)
            if worked_locales and publish_ids:
                from app.catalog_read_model import CatalogReadModel

                CatalogReadModel(self.db).refresh_roots(sorted(publish_ids))
            return len(locales), item_failures, len(worked_locales)

        completed = 0
        repaired = 0
        failures = []
        incomplete_repairs = []
        for offset in range(0, len(items), batch_size):
            batch = items[offset : offset + batch_size]
            for item, result, error in metadata_task_results(
                batch, process_item, should_terminate
            ):
                if error is not None:
                    raise error
                processed, item_failures, repaired_documents = result
                failures.extend(
                    failure
                    for failure in item_failures
                    if failure.get("kind") != "incomplete"
                )
                incomplete_repairs.extend(
                    failure
                    for failure in item_failures
                    if failure.get("kind") == "incomplete"
                )
                completed += processed
                repaired += repaired_documents
                provider, entity_type, provider_id = item
                self.store.update_run(
                    run_id,
                    progress_current=completed,
                    message=(
                        f"Processing {completed}/{total}: "
                        f"{entity_type} {provider}:{provider_id}"
                    ),
                )
        if should_terminate():
            raise JobTerminated()
        if failures:
            summary = (
                f"Checked {completed} metadata documents; repaired {repaired}; "
                f"{len(failures)} repair errors"
            )
            if incomplete_repairs:
                summary += f"; {len(incomplete_repairs)} repairs remain incomplete"
            self.store.update_run(
                run_id,
                state="failed",
                progress_current=completed,
                progress_total=total,
                finished_at=now(),
                message=summary,
                error=summary,
                error_details=json.dumps(
                    {
                        "operation": "metadata_refresh"
                        if force
                        else "metadata_missing",
                        "failures": failures,
                        "incompleteRepairs": incomplete_repairs,
                    }
                ),
            )
        else:
            summary = (
                f"Checked {completed} metadata documents; repaired {repaired} "
                "missing or partial documents"
            )
            if incomplete_repairs:
                summary += f"; {len(incomplete_repairs)} repairs remain incomplete"
            self.store.update_run(
                run_id,
                state="completed",
                progress_current=completed,
                progress_total=total,
                finished_at=now(),
                message=summary,
            )


class MetadataCleanupJob:
    def __init__(self, store: JobStore):
        self.store = store
        self.db = store.db

    def run(self, run_id: str, definition: dict, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        if should_terminate():
            raise JobTerminated()
        self.store.update_run(
            run_id,
            state="running",
            started_at=now(),
            progress_total=1,
            message="Cleaning orphaned library data",
            thread_name=threading.current_thread().name,
        )
        cleanup_orphans(self.db)
        self.store.update_run(
            run_id,
            state="completed",
            progress_current=1,
            progress_total=1,
            finished_at=now(),
            message="Orphaned library data cleaned",
        )


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

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.store.ensure_defaults()
        legacy_hydration = self.store.by_key("metadata_hydration")
        if legacy_hydration:
            self.store.db.execute(
                "DELETE FROM job_runs WHERE definition_id=?", (legacy_hydration["id"],)
            )
            self.store.db.execute(
                "DELETE FROM job_definitions WHERE id=?", (legacy_hydration["id"],)
            )
        legacy_projections = self.store.db.read_execute(
            "SELECT id FROM job_definitions WHERE kind='catalog_projection'"
        )
        for (definition_id,) in legacy_projections:
            self.store.db.execute(
                "DELETE FROM job_runs WHERE definition_id=?", (definition_id,)
            )
            self.store.db.execute(
                "DELETE FROM job_definitions WHERE id=?", (definition_id,)
            )
        for library in self.library_runtime.store.list():
            self.store.ensure_library(library)
        self._recover_active_runs()
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._dispatch, name="zenstream-job-scheduler", daemon=True
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=5)

    def refresh_library_definition(self, library: dict) -> dict:
        definition = self.store.ensure_library(library)
        values = {
            "intervalMinutes": library.get("scanIntervalMinutes"),
            "enabled": library.get("watchEnabled", True),
            "config": {"libraryId": library["id"]},
        }
        return self.store.update_definition(definition["id"], values)

    def remove_library_definition(self, library_id: str):
        for key in (f"library_scan:{library_id}",):
            definition = self.store.by_key(key)
            if definition:
                self.store.db.execute(
                    "DELETE FROM job_definitions WHERE id=?", (definition["id"],)
                )

    def run_now(self, definition_id: str) -> dict:
        definition = self.store.definition(definition_id)
        if not definition:
            raise KeyError("Job definition not found")
        if definition["kind"] == "library_scan":
            library_id = (definition.get("config") or {}).get("libraryId")
            job = self.library_runtime.enqueue(library_id, "scan")
            self.store.db.execute(
                "UPDATE job_definitions SET last_state=?,last_run_at=?,last_message=?,updated_at=? WHERE id=?",
                (
                    job["state"],
                    now(),
                    job.get("message") or "Library scan queued",
                    now(),
                    definition_id,
                ),
            )
            return job
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_metadata_missing(self) -> dict:
        definition = self.store.by_key("metadata_missing")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("metadata_missing")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_metadata_refresh(self) -> dict:
        definition = self.store.by_key("metadata_refresh")
        if not definition:
            definition = self.store.ensure(
                "metadata_refresh",
                "Refresh metadata",
                "Refetch all indexed provider metadata, artwork, and credits.",
                "metadata_refresh",
                43200,
                {},
            )
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_trickplay_extraction(self) -> dict:
        definition = self.store.by_key("trickplay_extract")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("trickplay_extract")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def enqueue_intro_outro_detection(self) -> dict:
        definition = self.store.by_key("intro_outro_detect")
        if not definition:
            self.store.ensure_defaults()
            definition = self.store.by_key("intro_outro_detect")
        run, _ = self.store.create_or_get_active_run(definition)
        with self.condition:
            self.condition.notify_all()
        return run

    def terminate(self, definition_id: str, run_id: str) -> dict | None:
        runs = [
            run for run in self.store.runs(definition_id, 100) if run["id"] == run_id
        ]
        if not runs:
            return None
        run = runs[0]
        if run["state"] not in {"queued", "running", "terminating"}:
            return run
        with self.active_lock:
            cancel_event = self.cancel_events.get(run_id)
            if cancel_event:
                cancel_event.set()
                self.store.update_run(
                    run_id, state="terminating", message="Termination requested"
                )
            else:
                self.store.update_run(
                    run_id,
                    state="terminated",
                    message="Terminated by administrator",
                    error=None,
                    finished_at=now(),
                )
        with self.condition:
            self.condition.notify_all()
        return next(
            (
                value
                for value in self.store.runs(definition_id, 100)
                if value["id"] == run_id
            ),
            None,
        )

    def _recover_active_runs(self) -> None:
        """Resume one interrupted run per task and terminate stale duplicates."""
        rows = self.store.db.execute(
            "SELECT id,definition_id,state FROM job_runs WHERE state IN ('queued','running','terminating') ORDER BY created_at DESC"
        )
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
                        cursor.execute(
                            "UPDATE job_runs SET state='queued',progress_current=0,progress_total=0,message='Queued again after Orchestrator restart',error=NULL,started_at=NULL,finished_at=NULL,thread_name=NULL WHERE id=?",
                            (run_id,),
                        )
                    else:
                        cursor.execute(
                            "UPDATE job_runs SET state='terminated',message='Superseded by the active task run',error=NULL,finished_at=? WHERE id=?",
                            (timestamp, run_id),
                        )

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
                    if (
                        run["id"] in self.active
                        or run["definitionId"] in self.active_definitions
                    ):
                        continue
                    self.active.add(run["id"])
                    self.active_definitions.add(run["definitionId"])
                    self.cancel_events[run["id"]] = threading.Event()
                    with self.store.db.transaction() as cursor:
                        cursor.execute(
                            "UPDATE job_runs SET state='running',started_at=?,thread_name=?,message='Starting task' WHERE id=? AND state='queued'",
                            (now(), f"zenstream-job-{run['id'][:8]}", run["id"]),
                        )
                        claimed = cursor.rowcount == 1
                    if not claimed:
                        self.active.discard(run["id"])
                        self.active_definitions.discard(run["definitionId"])
                        self.cancel_events.pop(run["id"], None)
                        continue
                thread = threading.Thread(
                    target=self._execute,
                    args=(run["id"],),
                    name=f"zenstream-job-{run['id'][:8]}",
                    daemon=True,
                )
                thread.start()
            with self.condition:
                self.condition.wait(timeout=1)

    def _execute(self, run_id: str):
        try:
            rows = self.store.db.execute(
                "SELECT r.id,r.definition_id,d.kind,d.config,d.name FROM job_runs r JOIN job_definitions d ON d.id=r.definition_id WHERE r.id=?",
                (run_id,),
            )
            if not rows:
                return
            _, definition_id, kind, config_text, name = rows[0]
            try:
                config = json.loads(config_text or "{}")
            except json.JSONDecodeError:
                config = {}
            definition = self.store.definition(definition_id) or {
                "id": definition_id,
                "kind": kind,
                "config": config,
                "name": name,
            }
            if kind == "metadata_missing":
                MetadataMissingJob(self.store).run(
                    run_id, definition, self.cancel_events[run_id].is_set
                )
            elif kind == "metadata_refresh":
                config = definition.get("config") or {}
                MetadataMissingJob(self.store).run(
                    run_id,
                    definition,
                    self.cancel_events[run_id].is_set,
                    force=True,
                    force_assets=not bool(config.get("preserveCachedAssets", False)),
                )
            elif kind == "metadata_cleanup":
                MetadataCleanupJob(self.store).run(
                    run_id, definition, self.cancel_events[run_id].is_set
                )
            elif kind == "trickplay_extract":
                self._run_analysis(
                    run_id,
                    kind,
                    TrickplayExtractor(),
                    self.cancel_events[run_id].is_set,
                )
            elif kind == "intro_outro_detect":
                self._run_analysis(
                    run_id,
                    kind,
                    IntroOutroDetector(),
                    self.cancel_events[run_id].is_set,
                )
            else:
                self.store.update_run(
                    run_id,
                    state="failed",
                    error=f"Unsupported job kind: {kind}",
                    finished_at=now(),
                )
            logger.info("scheduled job complete run_id=%s kind=%s", run_id, kind)
        except JobTerminated:
            self.store.update_run(
                run_id,
                state="terminated",
                message="Terminated by administrator",
                error=None,
                finished_at=now(),
            )
        except Exception as error:
            details = {
                "operation": "scheduled_job",
                "runId": run_id,
                "exception": type(error).__name__,
                "traceback": traceback.format_exc(),
            }
            logger.exception("scheduled job failed run_id=%s", run_id)
            self.store.update_run(
                run_id,
                state="failed",
                error=f"{type(error).__name__}: {error}",
                error_details=json.dumps(details),
                finished_at=now(),
            )
        finally:
            with self.active_lock:
                self.active.discard(run_id)
                self.cancel_events.pop(run_id, None)
                row = self.store.db.execute(
                    "SELECT definition_id FROM job_runs WHERE id=?", (run_id,)
                )
                if row:
                    self.active_definitions.discard(row[0][0])

    def _run_analysis(self, run_id, kind, worker, should_terminate):
        pressure_logged = False
        while active_requests():
            if should_terminate():
                raise JobTerminated()
            if not pressure_logged:
                logger.info(
                    "analysis job yielding to foreground traffic run_id=%s kind=%s active_requests=%s",
                    run_id,
                    kind,
                    active_requests(),
                )
                pressure_logged = True
            time.sleep(0.05)
        logger.info(
            "analysis job starting independent worker pool run_id=%s kind=%s",
            run_id,
            kind,
        )
        worker.run(run_id, self.store, should_terminate)
        logger.info(
            "analysis job completed worker pool run_id=%s kind=%s", run_id, kind
        )


scheduler = JobScheduler(library_runtime)
