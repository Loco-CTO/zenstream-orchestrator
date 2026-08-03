from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Iterable

from app.config import Config
from app.logging_config import get_logger
from app.models.metadata import IMAGE_LANGUAGE_SCHEMA, MetadataLanguageSettings


logger = get_logger("catalog_read_model")
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

    def _summary_values(self, entities, children):
        media = {
            row[0]: (row[1], row[2], row[3])
            for row in self.db.read_execute(
                "SELECT entity_id,MIN(modified_ns),MAX(modified_ns),COUNT(*) "
                "FROM media_files WHERE role='media' GROUP BY entity_id"
            )
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
        return {"title": title, "_imageLanguageSchema": IMAGE_LANGUAGE_SCHEMA}

    def _projection_values(self, entities, locales: list[str]):
        old: dict[tuple[str, str], str] = {}
        if self._has_table("catalog_metadata_projection"):
            old = {
                (row[0], row[1]): row[2]
                for row in self.db.read_execute(
                    "SELECT entity_id,locale,payload FROM catalog_metadata_projection"
                )
            }
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
                seen_grams = set()
                searchable = normalize_search_text(f"{payload.get('title') or ''} {path_text}")
                for size in (1, 2):
                    for index in range(0, max(0, len(searchable) - size + 1)):
                        gram = searchable[index : index + size]
                        if gram and gram not in seen_grams:
                            grams.append((gram, entity_id, locale, row[1], row[2]))
                            seen_grams.add(gram)
                for genre in payload.get("genres") or payload.get("tags") or []:
                    if isinstance(genre, str) and genre.strip():
                        key = normalize_search_text(genre)
                        genres.append((entity_id, locale, key, genre.strip()))
        return values, genres, grams

    def _user_values(self, entities, children):
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
        for user_id, entity_id, played in states:
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
        now = _now()
        return [(user_id, entity_id, count, now) for (user_id, entity_id), count in counts.items()]

    def rebuild(self, locales: Iterable[str] | None = None) -> int:
        if not self.available():
            return 0
        locales = list(locales or MetadataLanguageSettings().get()) or ["en"]
        entities, children = self._load_entities()
        summaries, _ = self._summary_values(entities, children)
        projections, genres, grams = self._projection_values(entities, locales)
        users = self._user_values(entities, children)
        collections = self._collection_values(entities, summaries)
        now = _now()
        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM catalog_entity_summary")
            cursor.executemany(
                "INSERT INTO catalog_entity_summary(entity_id,library_id,parent_id,entity_type,playable_leaf_count,media_file_count,media_added_ns,media_last_added_ns,added_sort_ns,last_added_sort_ns,generation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                summaries,
            )
            cursor.execute("DELETE FROM catalog_item_projection")
            cursor.executemany(
                "INSERT INTO catalog_item_projection(entity_id,locale,library_id,parent_id,entity_type,payload,title_sort,rating_sort,release_sort,runtime_sort,updated_at,generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                projections,
            )
            cursor.execute("DELETE FROM catalog_item_genres")
            cursor.executemany(
                "INSERT OR IGNORE INTO catalog_item_genres(entity_id,locale,genre_key,genre_name) VALUES(?,?,?,?)",
                genres,
            )
            cursor.execute("DELETE FROM catalog_search_grams")
            cursor.executemany(
                "INSERT OR IGNORE INTO catalog_search_grams(gram,entity_id,locale,library_id,parent_id) VALUES(?,?,?,?,?)",
                grams,
            )
            cursor.execute("DELETE FROM catalog_user_summary")
            cursor.executemany(
                "INSERT INTO catalog_user_summary(user_id,entity_id,played_leaf_count,updated_at) VALUES(?,?,?,?)",
                users,
            )
            cursor.execute("DELETE FROM catalog_collection_summary")
            cursor.executemany(
                "INSERT INTO catalog_collection_summary(collection_entity_id,collection_library_id,source_library_id,playable_leaf_count,media_file_count,added_sort_ns,last_added_sort_ns,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                collections,
            )
            cursor.execute(
                "INSERT INTO catalog_read_model_status(id,state,generation,updated_at,error) VALUES(1,'ready',1,?,NULL) ON CONFLICT(id) DO UPDATE SET state='ready',generation=1,updated_at=excluded.updated_at,error=NULL",
                (now,),
            )
        logger.info(
            "catalog read model rebuild complete entities=%s projections=%s users=%s",
            len(entities),
            len(projections),
            len(users),
        )
        return len(entities)

    @staticmethod
    def _collection_values(entities, summaries):
        # The collection-specific contribution is rebuilt from source summaries.
        # Authorization is applied at read time by source_library_id.
        # This method is intentionally pure so bootstrap remains deterministic.
        return []

    def status(self):
        if not self._has_table("catalog_read_model_status"):
            return None
        rows = self.db.read_execute(
            "SELECT state,generation,updated_at,error FROM catalog_read_model_status WHERE id=1"
        )
        return rows[0] if rows else None

