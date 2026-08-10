from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from app.catalog_read_model import CatalogReadModel
from app.config import Config
from app.logging_config import get_logger
from app.metadata_services import MetadataReadService
from app.models.metadata import MetadataLanguageSettings
from app.providers import PRIMARY_PROVIDER_BY_ENTITY

logger = get_logger("catalog_projections")
LEAF_TYPES = {"movie", "episode", "track", "release"}


def _timestamp(value: int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc).isoformat()


class CatalogProjectionStore:
    def __init__(self, db=None):
        self.db = db or Config().database

    def _library_entities(self, library_id: str):
        rows = self.db.read_execute(
            "SELECT id,parent_id,entity_type,created_at,library_id FROM library_entities WHERE library_id=?",
            (library_id,),
        )
        target_ids = {row[0] for row in rows}
        entities = {row[0]: row for row in rows}
        membership = self.db.read_execute(
            "SELECT m.collection_entity_id,m.source_entity_id,s.library_id "
            "FROM collection_members m "
            "JOIN library_entities c ON c.id=m.collection_entity_id "
            "JOIN library_entities s ON s.id=m.source_entity_id "
            "WHERE c.library_id=?",
            (library_id,),
        )
        source_libraries = {row[2] for row in membership}
        if source_libraries:
            placeholders = ",".join("?" for _ in source_libraries)
            rows += self.db.read_execute(
                f"SELECT id,parent_id,entity_type,created_at,library_id FROM library_entities WHERE library_id IN ({placeholders})",
                list(source_libraries),
            )
            entities.update({row[0]: row for row in rows})
        children = defaultdict(list)
        for row in rows:
            if row[1] in entities:
                children[row[1]].append(row[0])
        for parent_id, child_id, _ in membership:
            if parent_id in entities:
                children[parent_id].append(child_id)
        return entities, children, target_ids

    def rebuild_library(self, library_id: str, should_terminate=None) -> int:
        read_model = CatalogReadModel(self.db)
        if read_model.available():
            if should_terminate and should_terminate():
                raise RuntimeError("Projection rebuild cancelled")
            return read_model.rebuild()
        should_terminate = should_terminate or (lambda: False)
        entities, children, target_ids = self._library_entities(library_id)
        if not entities:
            return 0
        media_dates = {
            row[0]: (row[1], row[2])
            for row in self.db.read_execute(
                "SELECT entity_id,MIN(modified_ns),MAX(modified_ns) FROM media_files "
                "JOIN library_entities ON library_entities.id=media_files.entity_id "
                "WHERE media_files.role='media' AND library_entities.library_id=? "
                "GROUP BY entity_id",
                (library_id,),
            )
        }
        memo = {}

        def aggregate(entity_id: str):
            if entity_id in memo:
                return memo[entity_id]
            if should_terminate():
                raise RuntimeError("Projection rebuild cancelled")
            row = entities[entity_id]
            child_ids = [
                child for child in children.get(entity_id, []) if child in entities
            ]
            if not child_ids and row[2] in LEAF_TYPES:
                result = ([entity_id], 0)
            else:
                leaves = []
                descendant_count = 0
                for child_id in child_ids:
                    child_leaves, child_descendants = aggregate(child_id)
                    leaves.extend(child_leaves)
                    descendant_count += child_descendants + 1
                result = (leaves, descendant_count)
            memo[entity_id] = result
            return result

        values = []
        for entity_id in target_ids:
            row = entities[entity_id]
            leaves, descendant_count = aggregate(entity_id)
            dates = [
                media_dates.get(leaf_id)
                for leaf_id in leaves
                if media_dates.get(leaf_id)
            ]
            added = min(
                (value[0] for value in dates if value[0] is not None), default=None
            )
            last_added = max(
                (value[1] for value in dates if value[1] is not None), default=None
            )
            values.append(
                (
                    entity_id,
                    library_id,
                    descendant_count,
                    len(leaves),
                    0,
                    len(leaves),
                    added,
                    last_added,
                    1,
                    datetime.now(timezone.utc).isoformat(),
                )
            )
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM catalog_entity_rollups WHERE library_id=?", (library_id,)
            )
            for offset in range(0, len(values), 100):
                cursor.executemany(
                    "INSERT INTO catalog_entity_rollups(entity_id,library_id,descendant_count,playable_count,played_leaf_count,unplayed_leaf_count,added_ns,last_added_ns,generation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    values[offset : offset + 100],
                )
            cursor.execute(
                "INSERT INTO catalog_projection_status(library_id,generation,state,progress_current,progress_total,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(library_id) DO UPDATE SET generation=excluded.generation,state=excluded.state,progress_current=excluded.progress_current,progress_total=excluded.progress_total,error=NULL,updated_at=excluded.updated_at",
                (
                    library_id,
                    1,
                    "ready",
                    len(values),
                    len(values),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "catalog projection complete library_id=%s entities=%s",
            library_id,
            len(values),
        )
        return len(values)

    def rebuild_user(self, user_id: str, library_id: str, should_terminate=None) -> int:
        read_model = CatalogReadModel(self.db)
        if read_model.available():
            if should_terminate and should_terminate():
                raise RuntimeError("User projection rebuild cancelled")
            return read_model.rebuild()
        should_terminate = should_terminate or (lambda: False)
        entities, children, target_ids = self._library_entities(library_id)
        if not entities:
            return 0
        states = {
            row[0]: row[1:]
            for row in self.db.read_execute(
                "SELECT entity_id,favorite,played,play_count,position_seconds,duration_seconds,last_played_at "
                "FROM user_item_state WHERE user_id=?",
                (user_id,),
            )
        }
        memo = {}

        def aggregate(entity_id: str):
            if entity_id in memo:
                return memo[entity_id]
            if should_terminate():
                raise RuntimeError("User projection rebuild cancelled")
            row = entities[entity_id]
            child_ids = [
                child for child in children.get(entity_id, []) if child in entities
            ]
            if not child_ids and row[2] in LEAF_TYPES:
                state = states.get(entity_id, (0, 0, 0, 0, 0, None))
                result = (int(bool(state[1])), int(not state[1]), 1 if state[1] else 0)
            else:
                played = unplayed = 0
                count = 0
                for child_id in child_ids:
                    child_played, child_unplayed, child_count = aggregate(child_id)
                    played += child_played
                    unplayed += child_unplayed
                    count += child_count
                result = played, unplayed, count
            memo[entity_id] = result
            return result

        values = []
        now = datetime.now(timezone.utc).isoformat()
        for entity_id in target_ids:
            row = entities[entity_id]
            played, unplayed, _ = aggregate(entity_id)
            state = states.get(entity_id, (0, 0, 0, 0, 0, None))
            values.append(
                (
                    user_id,
                    entity_id,
                    state[0],
                    state[1],
                    state[2],
                    played,
                    unplayed,
                    state[3],
                    state[4],
                    state[5],
                    1,
                    now,
                )
            )
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM catalog_user_rollups WHERE user_id=? AND entity_id IN (SELECT id FROM library_entities WHERE library_id=?)",
                (user_id, library_id),
            )
            for offset in range(0, len(values), 100):
                cursor.executemany(
                    "INSERT INTO catalog_user_rollups(user_id,entity_id,favorite,played,play_count,played_leaf_count,unplayed_leaf_count,position_seconds,duration_seconds,last_played_at,generation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    values[offset : offset + 100],
                )
        return len(values)

    def rebuild_metadata(self, library_id: str, should_terminate=None) -> int:
        read_model = CatalogReadModel(self.db)
        if read_model.available():
            if should_terminate and should_terminate():
                raise RuntimeError("Metadata projection cancelled")
            return read_model.rebuild()
        should_terminate = should_terminate or (lambda: False)
        rows = self.db.read_execute(
            "SELECT id,entity_type FROM library_entities WHERE library_id=?",
            (library_id,),
        )
        locales = MetadataLanguageSettings().get()
        reader = MetadataReadService(self.db)
        values = []
        now = datetime.now(timezone.utc).isoformat()
        for index, (entity_id, entity_type) in enumerate(rows, start=1):
            if should_terminate():
                raise RuntimeError("Metadata projection cancelled")
            primary = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
            providers = self.db.read_execute(
                "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY CASE WHEN provider=? THEN 0 ELSE 1 END,provider",
                (entity_id, primary),
            )
            provider_ids = [
                {"provider": row[0], "type": row[1], "id": row[2]} for row in providers
            ]
            if not provider_ids:
                continue
            for locale in locales:
                try:
                    resolved = reader.resolve_public(
                        entity_id, entity_type, provider_ids, locale
                    )
                except Exception as error:
                    logger.debug(
                        "metadata projection skipped entity_id=%s locale=%s error=%s",
                        entity_id,
                        locale,
                        error,
                    )
                    continue
                values.append(
                    (
                        entity_id,
                        locale,
                        json.dumps(resolved["metadata"], ensure_ascii=False),
                        now,
                        1,
                    )
                )
                if len(values) >= 100:
                    self._write_metadata_batch(values)
                    values.clear()
        if values:
            self._write_metadata_batch(values)
        logger.info(
            "metadata projection complete library_id=%s entities=%s",
            library_id,
            len(rows),
        )
        return len(rows)

    def _write_metadata_batch(self, values) -> None:
        with self.db.transaction() as cursor:
            cursor.executemany(
                "INSERT INTO catalog_metadata_projection(entity_id,locale,payload,updated_at,generation) VALUES(?,?,?,?,?) ON CONFLICT(entity_id,locale) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at,generation=excluded.generation",
                values,
            )
