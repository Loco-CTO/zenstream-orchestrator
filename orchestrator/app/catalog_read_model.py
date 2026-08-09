from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import threading
from pathlib import Path
import re
import time
from typing import Iterable

from app.config import Config
from app.logging_config import get_logger
from app.models.metadata import IMAGE_LANGUAGE_SCHEMA, MetadataLanguageSettings


logger = get_logger("catalog_read_model")

_latest_root_lock = threading.Lock()
_latest_root_by_library: dict[str, str] = {}


def latest_catalog_root(library_id: str) -> str | None:
    with _latest_root_lock:
        return _latest_root_by_library.get(library_id)
LEAF_TYPES = {"movie", "episode", "track", "release"}
_SPACE_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _created_ns(value: str | None) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_search_text(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "").casefold()).strip()


def _numeric(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class CatalogReadModel:
    """Maintains the bounded, indexed catalog projections used by reads."""

    PROGRESS_INTERVAL_SECONDS = 2.0
    PROGRESS_BATCH_SIZE = 1000

    def __init__(self, db=None):
        self.db = db or Config().database

    def available(self) -> bool:
        rows = self.db.read_execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('catalog_entity_summary','catalog_item_projection','catalog_user_summary')"
        )
        return len(rows) == 3

    def _load_entities(self):
        rows = self.db.read_execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,created_at "
            "FROM library_entities"
        )
        entities = {row[0]: row for row in rows}
        children: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            if row[2] in entities:
                children[row[2]].append(row[0])
        if self._has_table("collection_members"):
            for collection_id, source_id in self.db.read_execute(
                "SELECT collection_entity_id,source_entity_id FROM collection_members"
            ):
                if collection_id in entities and source_id in entities:
                    children[collection_id].append(source_id)
        return entities, children

    def _has_table(self, name: str) -> bool:
        return bool(
            self.db.read_execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
        )

    def _has_columns(self, table: str, columns: set[str]) -> bool:
        if not self._has_table(table):
            return False
        existing = {
            row[1] for row in self.db.read_execute(f"PRAGMA table_info({table})")
        }
        return columns.issubset(existing)

    def _summary_values(self, entities, children, entity_ids=None, progress=None):
        media_query = (
            "SELECT entity_id,MIN(modified_ns),MAX(modified_ns),COUNT(*) "
            "FROM media_files WHERE role='media'"
        )
        media_params = []
        if entity_ids is not None:
            ids = list(entity_ids)
            if not ids:
                return [], {}
            media_query += f" AND entity_id IN ({','.join('?' for _ in ids)})"
            media_params.extend(ids)
        media_query += " GROUP BY entity_id"
        media = {
            row[0]: (row[1], row[2], row[3])
            for row in self.db.read_execute(media_query, media_params)
        }
        memo: dict[str, tuple] = {}
        visiting: set[str] = set()

        def summarize(entity_id: str):
            if entity_id in memo:
                return memo[entity_id]
            if entity_id in visiting:
                row = entities[entity_id]
                fallback = _created_ns(row[5])
                return 0, 0, None, None, fallback, fallback
            visiting.add(entity_id)
            row = entities[entity_id]
            own_added, own_last, own_count = media.get(entity_id, (None, None, 0))
            leaf_count = 1 if not children.get(entity_id) and row[3] in LEAF_TYPES else 0
            media_count = int(own_count or 0)
            added_values = [own_added] if own_added is not None else []
            last_values = [own_last] if own_last is not None else []
            for child_id in children.get(entity_id, ()):
                child = summarize(child_id)
                leaf_count += child[0]
                media_count += child[1]
                if child[2] is not None:
                    added_values.append(child[2])
                if child[3] is not None:
                    last_values.append(child[3])
            media_added = min(added_values) if added_values else None
            media_last = max(last_values) if last_values else None
            fallback = _created_ns(row[5])
            value = (
                leaf_count,
                media_count,
                media_added,
                media_last,
                media_added if media_added is not None else fallback,
                media_last if media_last is not None else fallback,
            )
            visiting.remove(entity_id)
            memo[entity_id] = value
            if progress is not None:
                progress("summaries", len(memo), len(entities))
            return value

        values = []
        now = _now()
        for entity_id, row in entities.items():
            summary = summarize(entity_id)
            values.append((
                entity_id,
                row[1],
                row[2],
                row[3],
                summary[0],
                summary[1],
                summary[2],
                summary[3],
                summary[4],
                summary[5],
                1,
                now,
            ))
        return values, memo

    @staticmethod
    def _fallback_payload(row) -> dict:
        title = Path(row[4] or "").stem or row[3].replace("_", " ").title()
        return {
            "title": title,
            "images": {},
            "_imageLanguageSchema": IMAGE_LANGUAGE_SCHEMA,
            "_catalogItemProjectionSchema": 1,
        }

    def _projection_values(self, entities, locales: list[str], progress=None):
        old: dict[tuple[str, str], str] = {}
        if self._has_table("catalog_item_projection"):
            old = {
                (row[0], row[1]): row[2]
                for row in self.db.read_execute(
                    "SELECT entity_id,locale,payload FROM catalog_item_projection"
                )
            }
        if self._has_table("catalog_metadata_projection"):
            old.update(
                {
                    (row[0], row[1]): row[2]
                    for row in self.db.read_execute(
                        "SELECT entity_id,locale,payload FROM catalog_metadata_projection"
                    )
                    if (row[0], row[1]) not in old
                }
            )
        values = []
        genres = []
        grams = []
        now = _now()
        for entity_id, row in entities.items():
            path_text = normalize_search_text(row[4])
            for locale in locales:
                payload_text = old.get((entity_id, locale))
                try:
                    payload = json.loads(payload_text) if payload_text else self._fallback_payload(row)
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = self._fallback_payload(row)
                if not isinstance(payload, dict):
                    payload = self._fallback_payload(row)
                payload_text = json.dumps(payload, ensure_ascii=False)
                title = normalize_search_text(payload.get("title") or row[4] or row[3])
                rating = _numeric(payload.get("communityRating"))
                release = str(payload.get("date") or payload.get("releaseDate") or "")
                runtime = _numeric(payload.get("runtimeMinutes"))
                values.append((
                    entity_id,
                    locale,
                    row[1],
                    row[2],
                    row[3],
                    payload_text,
                    title,
                    rating,
                    release,
                    runtime,
                    now,
                    1,
                ))
                if progress is not None:
                    progress("projections", len(values), len(entities) * len(locales))
                if row[2] is None and row[3] in {"movie", "series", "collection"}:
                    seen_grams = set()
                    searchable = normalize_search_text(
                        f"{payload.get('title') or ''} {path_text}"
                    )
                    for size in (1, 2):
                        for index in range(0, max(0, len(searchable) - size + 1)):
                            gram = searchable[index : index + size]
                            if gram and gram not in seen_grams:
                                grams.append(
                                    (gram, entity_id, locale, row[1], title)
                                )
                                seen_grams.add(gram)
                for genre in payload.get("genres") or payload.get("tags") or []:
                    if isinstance(genre, str) and genre.strip():
                        key = normalize_search_text(genre)
                        genres.append(
                            (entity_id, locale, row[1], row[3], key, genre.strip())
                        )
        return values, genres, grams

    def _user_values(self, entities, children, progress=None):
        if not self._has_table("user_item_state"):
            return []
        states = self.db.read_execute(
            "SELECT user_id,entity_id,played FROM user_item_state"
        )
        parents: dict[str, list[str]] = defaultdict(list)
        for entity_id, row in entities.items():
            if row[2] in entities:
                parents[entity_id].append(row[2])
        if self._has_table("collection_members"):
            for collection_id, source_id in self.db.read_execute(
                "SELECT collection_entity_id,source_entity_id FROM collection_members"
            ):
                if source_id in entities and collection_id in entities:
                    parents[source_id].append(collection_id)
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for index, (user_id, entity_id, played) in enumerate(states, 1):
            if not played or entity_id not in entities:
                continue
            stack = [entity_id]
            seen = set()
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                row = entities[current]
                if row[3] in LEAF_TYPES and not children.get(current):
                    counts[(user_id, current)] += 1
                stack.extend(parents.get(current, ()))
            if progress is not None:
                progress("user_summary", index, len(states))
        now = _now()
        return [(user_id, entity_id, count, now) for (user_id, entity_id), count in counts.items()]

    def _has_progress_columns(self) -> bool:
        if not self._has_table("catalog_read_model_status"):
            return False
        columns = {
            row[1]
            for row in self.db.read_execute("PRAGMA table_info(catalog_read_model_status)")
        }
        return {
            "stage",
            "processed",
            "total",
            "started_at",
            "heartbeat_at",
        }.issubset(columns)

    def _persist_progress(
        self,
        stage: str,
        processed: int,
        total: int,
        started_at: str,
        state: str = "building",
        generation: int | None = None,
        error: str | None = None,
    ) -> None:
        if not self._has_progress_columns():
            return
        now = _now()
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE catalog_read_model_status SET state=?,generation=COALESCE(?,generation),"
                "updated_at=?,error=?,stage=?,processed=?,total=?,started_at=?,heartbeat_at=? WHERE id=1",
                (
                    state,
                    generation,
                    now,
                    error,
                    stage,
                    int(processed),
                    int(total),
                    started_at,
                    now,
                ),
            )

    def _progress_tracker(self, started_at: str):
        last_report = 0.0

        def report(
            stage: str,
            processed: int,
            total: int,
            force: bool = False,
            persist: bool = True,
        ) -> None:
            nonlocal last_report
            now = time.monotonic()
            if not force and now - last_report < self.PROGRESS_INTERVAL_SECONDS:
                return
            last_report = now
            percent = (processed / total * 100.0) if total else 0.0
            elapsed = now - report.started_monotonic
            logger.info(
                "catalog read model rebuild progress stage=%s processed=%s total=%s percent=%.1f elapsed_seconds=%.1f",
                stage,
                processed,
                total,
                percent,
                elapsed,
            )
            if persist:
                self._persist_progress(stage, processed, total, started_at)

        report.started_monotonic = time.monotonic()
        return report

    def rebuild(self, locales: Iterable[str] | None = None) -> int:
        if not self.available():
            return 0
        locales = list(locales or MetadataLanguageSettings().get()) or ["en"]
        started_at = _now()
        progress = self._progress_tracker(started_at)
        progress("loading_entities", 0, 0, force=True)
        self._persist_progress("loading_entities", 0, 0, started_at)
        entities, children = self._load_entities()
        progress("loading_entities", len(entities), len(entities), force=True)
        progress("summaries", 0, len(entities), force=True)
        summaries, _ = self._summary_values(entities, children, entities, progress)
        progress("summaries", len(entities), len(entities), force=True)
        projection_total = len(entities) * len(locales)
        progress("projections", 0, projection_total, force=True)
        projections, genres, grams = self._projection_values(entities, locales, progress)
        progress("projections", len(projections), projection_total, force=True)
        progress("user_summary", 0, 0, force=True)
        users = self._user_values(entities, children, progress)
        progress("user_summary", len(users), len(users), force=True)
        progress("collection_summary", 0, 0, force=True)
        collections = self._collection_values_from_db(entities, summaries, progress)
        progress("collection_summary", len(collections), len(collections), force=True)
        now = _now()
        write_total = len(summaries) + len(projections) + len(genres) + len(grams) + len(users) + len(collections)
        progress("writing", 0, write_total, force=True, persist=False)
        written = 0

        def write_rows(cursor, query, rows, stage):
            nonlocal written
            for offset in range(0, len(rows), self.PROGRESS_BATCH_SIZE):
                batch = rows[offset : offset + self.PROGRESS_BATCH_SIZE]
                cursor.executemany(query, batch)
                written += len(batch)
                progress(stage, written, write_total, persist=False)

        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM catalog_entity_summary")
            write_rows(cursor,
                "INSERT INTO catalog_entity_summary(entity_id,library_id,parent_id,entity_type,playable_leaf_count,media_file_count,media_added_ns,media_last_added_ns,added_sort_ns,last_added_sort_ns,generation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                summaries,
                "writing_summaries",
            )
            cursor.execute("DELETE FROM catalog_item_projection")
            write_rows(cursor,
                "INSERT INTO catalog_item_projection(entity_id,locale,library_id,parent_id,entity_type,payload,title_sort,rating_sort,release_sort,runtime_sort,updated_at,generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                projections,
                "writing_projections",
            )
            cursor.execute("DELETE FROM catalog_item_genres")
            if self._has_columns("catalog_item_genres", {"library_id", "entity_type"}):
                write_rows(cursor,
                    "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,library_id,entity_type,genre_key,genre_name) VALUES(?,?,?,?,?,?)",
                    genres,
                    "writing_genres",
                )
            else:
                write_rows(cursor,
                    "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,genre_key,genre_name) VALUES(?,?,?,?)",
                    [(row[0], row[1], row[4], row[5]) for row in genres],
                    "writing_genres",
                )
            cursor.execute("DELETE FROM catalog_search_grams")
            write_rows(cursor,
                "INSERT OR IGNORE INTO catalog_search_grams(gram,entity_id,locale,library_id,parent_id) VALUES(?,?,?,?,NULL)",
                [row[:4] for row in grams],
                "writing_search_grams",
            )
            if self._has_table("catalog_root_search_grams"):
                cursor.execute("DELETE FROM catalog_root_search_grams")
                write_rows(cursor,
                    "INSERT OR IGNORE INTO catalog_root_search_grams(gram,entity_id,locale,library_id,title_sort) VALUES(?,?,?,?,?)",
                    grams,
                    "writing_root_search_grams",
                )
            cursor.execute("DELETE FROM catalog_user_summary")
            write_rows(cursor,
                "INSERT INTO catalog_user_summary(user_id,entity_id,played_leaf_count,updated_at) VALUES(?,?,?,?)",
                users,
                "writing_user_summary",
            )
            cursor.execute("DELETE FROM catalog_collection_summary")
            write_rows(cursor,
                "INSERT INTO catalog_collection_summary(collection_entity_id,collection_library_id,source_library_id,playable_leaf_count,media_file_count,added_sort_ns,last_added_sort_ns,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                collections,
                "writing_collection_summary",
            )
            if self._has_progress_columns():
                cursor.execute(
                    "INSERT INTO catalog_read_model_status(id,state,generation,updated_at,error,stage,processed,total,started_at,heartbeat_at) VALUES(1,'ready',1,?,NULL,'complete',?,?,?,?) ON CONFLICT(id) DO UPDATE SET state='ready',generation=1,updated_at=excluded.updated_at,error=NULL,stage='complete',processed=excluded.processed,total=excluded.total,started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at",
                    (now, write_total, write_total, started_at, now),
                )
            else:
                cursor.execute(
                    "INSERT INTO catalog_read_model_status(id,state,generation,updated_at,error) VALUES(1,'ready',1,?,NULL) ON CONFLICT(id) DO UPDATE SET state='ready',generation=1,updated_at=excluded.updated_at,error=NULL",
                    (now,),
                )
            if self._has_table("catalog_library_summary"):
                cursor.execute("DELETE FROM catalog_library_summary")
                cursor.execute(
                    "INSERT INTO catalog_library_summary(library_id,generation,supports_last_added,last_root_entity_id,updated_at) "
                    "SELECT l.id,1,CASE WHEN EXISTS(SELECT 1 FROM catalog_entity_summary s WHERE s.library_id=l.id AND s.parent_id IS NOT NULL) THEN 1 ELSE 0 END,NULL,? FROM libraries l",
                    (now,),
                )
        progress("complete", write_total, write_total, force=True, persist=False)
        logger.info(
            "catalog read model rebuild complete entities=%s projections=%s users=%s",
            len(entities),
            len(projections),
            len(users),
        )
        return len(entities)

    def _collection_values_from_db(self, entities, summaries, progress=None):
        if not self._has_table("collection_members"):
            return []
        summary_by_id = {row[0]: row for row in summaries}
        grouped: dict[tuple[str, str], dict[str, object]] = {}
        rows = self.db.read_execute(
            "SELECT m.collection_entity_id,m.source_entity_id,s.library_id "
            "FROM collection_members m "
            "JOIN library_entities c ON c.id=m.collection_entity_id "
            "JOIN library_entities s ON s.id=m.source_entity_id"
        )
        now = _now()
        for index, (collection_id, source_id, source_library_id) in enumerate(rows, 1):
            collection = entities.get(collection_id)
            summary = summary_by_id.get(source_id)
            if collection is None or summary is None:
                continue
            key = (collection_id, source_library_id)
            value = grouped.setdefault(
                key,
                {
                    "collection_library_id": collection[1],
                    "playable_leaf_count": 0,
                    "media_file_count": 0,
                    "added": [],
                    "last": [],
                },
            )
            value["playable_leaf_count"] += int(summary[4] or 0)
            value["media_file_count"] += int(summary[5] or 0)
            if summary[8] is not None:
                value["added"].append(int(summary[8]))
            if summary[9] is not None:
                value["last"].append(int(summary[9]))
            if progress is not None:
                progress("collection_summary", index, len(rows))
        return [
            (
                collection_id,
                value["collection_library_id"],
                source_library_id,
                value["playable_leaf_count"],
                value["media_file_count"],
                min(value["added"], default=0),
                max(value["last"], default=0),
                now,
            )
            for (collection_id, source_library_id), value in grouped.items()
        ]

    def _coverage_gaps(
        self, configured: list[str]
    ) -> tuple[dict[str, tuple], list[str], list[str]]:
        entity_rows = self.db.read_execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,created_at FROM library_entities"
        )
        entities = {row[0]: row for row in entity_rows}
        summary_ids = {
            row[0]
            for row in self.db.read_execute(
                "SELECT entity_id FROM catalog_entity_summary"
            )
            if row[0] in entities
        }
        projection_locales: dict[str, set[str]] = defaultdict(set)
        for entity_id, locale in self.db.read_execute(
            "SELECT entity_id,locale FROM catalog_item_projection"
        ):
            if entity_id in entities:
                projection_locales[entity_id].add(locale)
        required = set(configured)
        missing_summaries = [
            entity_id for entity_id in entities if entity_id not in summary_ids
        ]
        missing_projections = [
            entity_id
            for entity_id in entities
            if not required.issubset(projection_locales.get(entity_id, set()))
        ]
        return entities, missing_summaries, missing_projections

    @staticmethod
    def _top_roots(entities: dict[str, tuple], entity_ids: Iterable[str]) -> list[str]:
        roots = []
        for entity_id in entity_ids:
            current = entity_id
            seen = set()
            while current in entities and current not in seen:
                seen.add(current)
                parent_id = entities[current][2]
                if parent_id not in entities:
                    break
                current = parent_id
            roots.append(current)
        return list(dict.fromkeys(roots))

    def _active_inventory_jobs(self) -> bool:
        if not self._has_table("library_jobs"):
            return False
        return bool(
            self.db.read_execute(
                "SELECT 1 FROM library_jobs WHERE kind IN ('scan','reconcile') "
                "AND state IN ('queued','running','terminating') LIMIT 1"
            )
        )

    def _repair_projection_roots(
        self, root_ids: Iterable[str], configured: list[str]
    ) -> int:
        roots = list(dict.fromkeys(root_ids))
        if not roots:
            return 0
        self.refresh_roots(roots)
        placeholders = ",".join("?" for _ in roots)
        rows = self.db.read_execute(
            "WITH RECURSIVE subtree(id) AS ("
            f"SELECT id FROM library_entities WHERE id IN ({placeholders}) "
            "UNION ALL SELECT e.id FROM library_entities e JOIN subtree s ON e.parent_id=s.id) "
            "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.created_at "
            "FROM library_entities e JOIN subtree s ON s.id=e.id",
            roots,
        )
        entities = {row[0]: row for row in rows}
        projections, genres, grams = self._projection_values(entities, configured)
        entity_ids = list(entities)
        entity_placeholders = ",".join("?" for _ in entity_ids)
        with self.db.transaction() as cursor:
            cursor.executemany(
                "INSERT INTO catalog_item_projection(entity_id,locale,library_id,parent_id,entity_type,payload,title_sort,rating_sort,release_sort,runtime_sort,updated_at,generation) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id,locale) DO UPDATE SET "
                "library_id=excluded.library_id,parent_id=excluded.parent_id,entity_type=excluded.entity_type,payload=excluded.payload,title_sort=excluded.title_sort,rating_sort=excluded.rating_sort,release_sort=excluded.release_sort,runtime_sort=excluded.runtime_sort,updated_at=excluded.updated_at,generation=excluded.generation",
                projections,
            )
            if entity_ids and self._has_table("catalog_item_genres"):
                cursor.execute(
                    f"DELETE FROM catalog_item_genres WHERE entity_id IN ({entity_placeholders})",
                    entity_ids,
                )
                if self._has_columns("catalog_item_genres", {"library_id", "entity_type"}):
                    cursor.executemany(
                        "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,library_id,entity_type,genre_key,genre_name) VALUES(?,?,?,?,?,?)",
                        genres,
                    )
                else:
                    cursor.executemany(
                        "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,genre_key,genre_name) VALUES(?,?,?,?)",
                        [(row[0], row[1], row[4], row[5]) for row in genres],
                    )
            if entity_ids and self._has_table("catalog_search_grams"):
                cursor.execute(
                    f"DELETE FROM catalog_search_grams WHERE entity_id IN ({entity_placeholders})",
                    entity_ids,
                )
                cursor.executemany(
                    "INSERT OR IGNORE INTO catalog_search_grams(gram,entity_id,locale,library_id,parent_id) VALUES(?,?,?,?,NULL)",
                    [row[:4] for row in grams],
                )
            if entity_ids and self._has_table("catalog_root_search_grams"):
                cursor.execute(
                    f"DELETE FROM catalog_root_search_grams WHERE entity_id IN ({entity_placeholders})",
                    entity_ids,
                )
                cursor.executemany(
                    "INSERT OR IGNORE INTO catalog_root_search_grams(gram,entity_id,locale,library_id,title_sort) VALUES(?,?,?,?,?)",
                    grams,
                )
        return len(entities)

    def bootstrap(self, locales: Iterable[str] | None = None) -> int:
        """Build the read model before interactive services become healthy."""
        if not self.available() or not self._has_table("catalog_read_model_status"):
            return 0
        configured = list(locales or MetadataLanguageSettings().get()) or ["en"]
        status = self.status()
        entity_count = int(self.db.read_execute("SELECT COUNT(*) FROM library_entities")[0][0])
        summary_count = int(self.db.read_execute("SELECT COUNT(*) FROM catalog_entity_summary")[0][0])
        projection_count = int(self.db.read_execute("SELECT COUNT(*) FROM catalog_item_projection")[0][0])
        expected_projections = entity_count * len(configured)
        entities, missing_summary_ids, missing_projection_ids = self._coverage_gaps(
            configured
        )
        if (
            status
            and status[0] == "ready"
            and not missing_summary_ids
            and not missing_projection_ids
        ):
            return entity_count
        missing_ids = list(
            dict.fromkeys([*missing_summary_ids, *missing_projection_ids])
        )
        if status and status[0] == "ready" and self._active_inventory_jobs():
            logger.info(
                "catalog read model bootstrap deferred coverage repair active_inventory=true entities=%s summaries=%s projections=%s expected_projections=%s missing_entities=%s",
                entity_count,
                summary_count,
                projection_count,
                expected_projections,
                len(missing_ids),
            )
            return summary_count
        if status and 0 < len(missing_ids) <= 300:
            try:
                roots = self._top_roots(entities, missing_ids)
                self._repair_projection_roots(roots, configured)
                _, remaining_summaries, remaining_projections = self._coverage_gaps(
                    configured
                )
                if not remaining_summaries and not remaining_projections:
                    logger.info(
                        "catalog read model bootstrap repaired roots=%s missing_summaries=%s missing_projections=%s",
                        len(roots),
                        len(missing_summary_ids),
                        len(missing_projection_ids),
                    )
                    return entity_count
            except Exception:
                logger.exception("catalog read model targeted coverage repair failed")
        logger.info(
            "catalog read model bootstrap state=%s entities=%s summaries=%s projections=%s expected_projections=%s",
            status[0] if status else "missing",
            entity_count,
            summary_count,
            projection_count,
            expected_projections,
        )
        try:
            count = self.rebuild(configured)
            self._retire_legacy_tables()
            return count
        except Exception as error:
            now = _now()
            if self._has_progress_columns():
                self._persist_progress(
                    "failed",
                    0,
                    0,
                    now,
                    state="failed",
                    error=str(error)[:1000],
                )
            else:
                with self.db.transaction() as cursor:
                    cursor.execute(
                        "UPDATE catalog_read_model_status SET state='failed',updated_at=?,error=? WHERE id=1",
                        (now, str(error)[:1000]),
                    )
            raise

    def _retire_legacy_tables(self) -> None:
        with self.db.transaction() as cursor:
            for table in (
                "catalog_entity_rollups",
                "catalog_user_rollups",
                "catalog_metadata_projection",
                "catalog_home_projection",
                "catalog_projection_status",
            ):
                cursor.execute(f"DROP TABLE IF EXISTS {table}")

    def refresh_roots(self, root_ids: Iterable[str]) -> int:
        """Refresh committed scanner subtrees without touching unrelated roots."""
        if not self.available():
            return 0
        roots = list(dict.fromkeys(root_ids))
        if not roots:
            return 0
        placeholders = ",".join("?" for _ in roots)
        rows = self.db.read_execute(
            "WITH RECURSIVE subtree(id) AS ("
            f"SELECT id FROM library_entities WHERE id IN ({placeholders}) "
            "UNION ALL SELECT e.id FROM library_entities e JOIN subtree s ON e.parent_id=s.id) "
            "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.created_at "
            "FROM library_entities e JOIN subtree s ON s.id=e.id",
            roots,
        )
        entities = {row[0]: row for row in rows}
        children: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            if row[2] in entities:
                children[row[2]].append(row[0])
        summaries, _ = self._summary_values(entities, children, entities)
        locales = list(MetadataLanguageSettings().get()) or ["en"]
        now = _now()
        projection_rows = []
        for entity_id, row in entities.items():
            payload = json.dumps(self._fallback_payload(row), ensure_ascii=False)
            title = normalize_search_text(json.loads(payload).get("title"))
            for locale in locales:
                projection_rows.append(
                    (entity_id, locale, row[1], row[2], row[3], payload, title, 0.0, "", 0.0, now, 1)
                )
        affected_collections: list[str] = []
        if self._has_table("collection_members") and entities:
            entity_placeholders = ",".join("?" for _ in entities)
            affected_collections = [
                row[0]
                for row in self.db.read_execute(
                    "SELECT DISTINCT collection_entity_id FROM collection_members "
                    f"WHERE source_entity_id IN ({entity_placeholders}) OR collection_entity_id IN ({entity_placeholders})",
                    [*entities.keys(), *entities.keys()],
                )
            ]
        with self.db.transaction() as cursor:
            cursor.executemany(
                "INSERT INTO catalog_entity_summary(entity_id,library_id,parent_id,entity_type,playable_leaf_count,media_file_count,media_added_ns,media_last_added_ns,added_sort_ns,last_added_sort_ns,generation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET library_id=excluded.library_id,parent_id=excluded.parent_id,entity_type=excluded.entity_type,playable_leaf_count=excluded.playable_leaf_count,media_file_count=excluded.media_file_count,media_added_ns=excluded.media_added_ns,media_last_added_ns=excluded.media_last_added_ns,added_sort_ns=excluded.added_sort_ns,last_added_sort_ns=excluded.last_added_sort_ns,generation=excluded.generation,updated_at=excluded.updated_at",
                summaries,
            )
            cursor.executemany(
                "INSERT INTO catalog_item_projection(entity_id,locale,library_id,parent_id,entity_type,payload,title_sort,rating_sort,release_sort,runtime_sort,updated_at,generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id,locale) DO NOTHING",
                projection_rows,
            )
            if affected_collections:
                collection_placeholders = ",".join("?" for _ in affected_collections)
                cursor.execute(
                    f"DELETE FROM catalog_collection_summary WHERE collection_entity_id IN ({collection_placeholders})",
                    affected_collections,
                )
                cursor.execute(
                    "INSERT INTO catalog_collection_summary(collection_entity_id,collection_library_id,source_library_id,playable_leaf_count,media_file_count,added_sort_ns,last_added_sort_ns,updated_at) "
                    "SELECT m.collection_entity_id,c.library_id,s.library_id,SUM(x.playable_leaf_count),SUM(x.media_file_count),MIN(x.added_sort_ns),MAX(x.last_added_sort_ns),? "
                    "FROM collection_members m JOIN library_entities c ON c.id=m.collection_entity_id "
                    "JOIN library_entities s ON s.id=m.source_entity_id "
                    "JOIN catalog_entity_summary x ON x.entity_id=m.source_entity_id "
                    f"WHERE m.collection_entity_id IN ({collection_placeholders}) "
                    "GROUP BY m.collection_entity_id,s.library_id",
                    [now, *affected_collections],
                )
            cursor.execute(
                "UPDATE catalog_read_model_status SET state='ready',generation=generation+1,updated_at=?,error=NULL WHERE id=1",
                (now,),
            )
            affected_libraries = {row[1] for row in summaries}
            if self._has_table("catalog_library_summary"):
                for library_id in affected_libraries:
                    library_roots = [
                        root_id
                        for root_id in roots
                        if entities.get(root_id) and entities[root_id][1] == library_id
                    ]
                    cursor.execute(
                        "INSERT INTO catalog_library_summary(library_id,generation,supports_last_added,last_root_entity_id,updated_at) "
                        "VALUES(?,1,EXISTS(SELECT 1 FROM catalog_entity_summary WHERE library_id=? AND parent_id IS NOT NULL),?,?) "
                        "ON CONFLICT(library_id) DO UPDATE SET generation=catalog_library_summary.generation+1,supports_last_added=excluded.supports_last_added,last_root_entity_id=COALESCE(excluded.last_root_entity_id,catalog_library_summary.last_root_entity_id),updated_at=excluded.updated_at",
                        (
                            library_id,
                            library_id,
                            library_roots[-1] if library_roots else None,
                            now,
                        ),
                    )
            if self._has_table("catalog_projection_status"):
                for library_id in affected_libraries:
                    cursor.execute(
                        "INSERT INTO catalog_projection_status(library_id,generation,state,progress_current,progress_total,error,updated_at) "
                        "VALUES(?,COALESCE((SELECT generation FROM catalog_projection_status WHERE library_id=?),0)+1,'ready',0,0,NULL,?) "
                        "ON CONFLICT(library_id) DO UPDATE SET generation=excluded.generation,state='ready',error=NULL,updated_at=excluded.updated_at",
                        (library_id, library_id, now),
                    )
        with _latest_root_lock:
            for root_id in roots:
                row = entities.get(root_id)
                if row:
                    _latest_root_by_library[row[1]] = root_id
        return len(summaries)

    def refresh_user_entities(self, user_id: str, entity_ids: Iterable[str]) -> int:
        if not self.available() or not self._has_table("catalog_user_summary"):
            return 0
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.read_execute(
            "WITH RECURSIVE roots(id) AS ("
            f"SELECT id FROM library_entities WHERE id IN ({placeholders})), "
            "tree(root_id,id) AS (SELECT id,id FROM roots UNION ALL "
            "SELECT tree.root_id,e.id FROM tree JOIN library_entities e ON e.parent_id=tree.id) "
            "SELECT tree.root_id,COUNT(state.entity_id) FROM tree "
            "JOIN library_entities leaf ON leaf.id=tree.id "
            "LEFT JOIN user_item_state state ON state.user_id=? AND state.entity_id=leaf.id "
            "AND state.played=1 AND leaf.entity_type IN ('movie','episode','track','release') "
            "GROUP BY tree.root_id",
            [*ids, user_id],
        )
        now = _now()
        with self.db.transaction() as cursor:
            cursor.executemany(
                "INSERT INTO catalog_user_summary(user_id,entity_id,played_leaf_count,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id,entity_id) DO UPDATE SET played_leaf_count=excluded.played_leaf_count,updated_at=excluded.updated_at",
                [(user_id, row[0], int(row[1] or 0), now) for row in rows],
            )
        return len(rows)

    def status(self):
        if not self._has_table("catalog_read_model_status"):
            return None
        if self._has_progress_columns():
            query = "SELECT state,generation,updated_at,error,stage,processed,total,started_at,heartbeat_at FROM catalog_read_model_status WHERE id=1"
        else:
            query = "SELECT state,generation,updated_at,error FROM catalog_read_model_status WHERE id=1"
        rows = self.db.read_execute(query)
        return rows[0] if rows else None
