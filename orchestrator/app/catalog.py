from __future__ import annotations

import unicodedata
import math
import contextvars
import time
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app.config import Config
from app.models.metadata import MetadataLanguageSettings, normalize_metadata_locale
from app.metadata_domain import choose_artwork, fallback_tiers, locale_variants
from app.metadata_services import MetadataReadService
from app.providers import IMAGE_TYPES, PRIMARY_PROVIDER_BY_ENTITY
from app.images import LocalArtworkCache
from app.logging_config import get_logger


LOCAL_ARTWORK_NAMES = {
    "Primary": {"poster", "folder", "cover", "primary", "tvshow", "movie", "season"},
    "Backdrop": {"backdrop", "fanart", "background"},
    "Logo": {"logo", "clearlogo", "clear-logo"},
    "Banner": {"banner"},
}

logger = get_logger("catalog")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _CatalogDatabase:
    def __init__(self, database):
        self._database = database

    def execute(self, query, params=None):
        return self._database.read_execute(query, params)

    def transaction(self):
        return self._database.transaction()

    def __getattr__(self, name):
        return getattr(self._database, name)


class _CatalogReadContext:
    def __init__(self, catalog, user_id: str):
        self.catalog = catalog
        self.user_id = user_id
        self.entity_rows: dict[str, tuple | None] = {}
        self.provider_ids: dict[str, list[dict]] = {}
        self.graph: tuple[dict, dict, dict] | None = None
        self.direct_states: dict[str, tuple] | None = None
        self.playable_descendants: dict[str, list[str]] = {}
        self.metadata_service = MetadataReadService(catalog.db)
        self.configured_languages: list[str] | None = None
        self.timings: dict[str, float] = {}

    def measure(self, stage: str, action):
        started = time.perf_counter()
        try:
            return action()
        finally:
            self.timings[stage] = self.timings.get(stage, 0.0) + (time.perf_counter() - started)


def _catalog_read(method):
    @wraps(method)
    def wrapped(self, user_id: str, *args, **kwargs):
        active = self._read_context.get()
        if active is not None and active.user_id == user_id:
            return method(self, user_id, *args, **kwargs)
        context = _CatalogReadContext(self, user_id)
        token = self._read_context.set(context)
        started = time.perf_counter()
        try:
            return method(self, user_id, *args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            details = " ".join(
                f"{stage}_seconds={duration:.3f}"
                for stage, duration in sorted(context.timings.items())
            )
            log = logger.warning if elapsed > 2 else logger.debug
            log(
                "catalog request complete operation=%s user_id=%s duration_seconds=%.3f %s",
                method.__name__,
                user_id,
                elapsed,
                details,
            )
            self._read_context.reset(token)
    return wrapped


class Catalog:
    _read_context: contextvars.ContextVar[_CatalogReadContext | None] = contextvars.ContextVar(
        "catalog_read_context", default=None
    )

    def __init__(self):
        self.db = _CatalogDatabase(Config().database)

    def _context(self, user_id: str) -> _CatalogReadContext | None:
        context = self._read_context.get()
        return context if context and context.user_id == user_id else None

    def _configured_languages(self, user_id: str) -> list[str]:
        context = self._context(user_id)
        if context is None:
            return MetadataLanguageSettings().get()
        if context.configured_languages is None:
            context.configured_languages = MetadataLanguageSettings().get()
        return context.configured_languages

    def allowed_libraries(self, user_id: str) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT library_id FROM user_library_access WHERE user_id=?",
                (user_id,),
            )
        }

    def _has_table(self, name: str) -> bool:
        return bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
        )

    def require_library(self, user_id: str, library_id: str) -> dict:
        if library_id not in self.allowed_libraries(user_id):
            raise HTTPException(404, "Library not found.")
        rows = self.db.execute(
            "SELECT id,name,type,scan_state,last_scan_finished_at FROM libraries WHERE id=?",
            (library_id,),
        )
        if not rows:
            raise HTTPException(404, "Library not found.")
        row = rows[0]
        return {
            "id": row[0],
            "name": row[1],
            "type": row[2],
            "scanState": row[3],
            "lastScanFinishedAt": row[4],
            "supportsLastAdded": self._supports_last_added(library_id),
        }

    def _supports_last_added(self, library_id: str) -> bool:
        return bool(
            self.db.execute(
                "SELECT 1 FROM library_entities child JOIN library_entities parent ON parent.id=child.parent_id WHERE parent.library_id=? LIMIT 1",
                (library_id,),
            )
            or (
                self._has_table("collection_members")
                and self.db.execute(
                "SELECT 1 FROM collection_members m JOIN library_entities parent ON parent.id=m.collection_entity_id WHERE parent.library_id=? LIMIT 1",
                (library_id,),
                )
            )
        )

    def libraries(self, user_id: str) -> list[dict]:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return []
        rows = self.db.execute(
            f"SELECT id,name,type,scan_state,last_scan_finished_at FROM libraries WHERE id IN ({','.join('?' for _ in allowed)}) AND type IN ('movies','tv_series','collection') ORDER BY name COLLATE NOCASE",
            list(allowed),
        )
        return [
            {
                "id": row[0],
                "name": row[1],
                "type": row[2],
                "scanState": row[3],
                "lastScanFinishedAt": row[4],
                "supportsLastAdded": self._supports_last_added(row[0]),
            }
            for row in rows
        ]

    def _entity_row(self, entity_id: str):
        context = self._read_context.get()
        if context is not None and entity_id in context.entity_rows:
            return context.entity_rows[entity_id]
        rows = self.db.execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE id=?",
            (entity_id,),
        )
        row = rows[0] if rows else None
        if context is not None:
            context.entity_rows[entity_id] = row
        return row

    def require_entity(self, user_id: str, entity_id: str):
        row = self._entity_row(entity_id)
        if not row or row[1] not in self.allowed_libraries(user_id):
            raise HTTPException(404, "Item not found.")
        return row

    def _provider_ids(self, entity_id: str, entity_type: str) -> list[dict]:
        context = self._read_context.get()
        if context is not None and entity_id in context.provider_ids:
            return context.provider_ids[entity_id]
        primary = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
        rows = self.db.execute(
            "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY CASE WHEN provider=? THEN 0 ELSE 1 END,provider",
            (entity_id, primary),
        )
        values = [{"provider": row[0], "type": row[1], "id": row[2]} for row in rows]
        if context is not None:
            context.provider_ids[entity_id] = values
        return values

    def _read_service(self) -> MetadataReadService:
        context = self._read_context.get()
        return context.metadata_service if context is not None else MetadataReadService(self.db)

    @_catalog_read
    def metadata(
        self, user_id: str, entity_id: str, language: str, include_credits: bool = False
    ) -> dict:
        row = self.require_entity(user_id, entity_id)
        configured = self._configured_languages(user_id)
        language = normalize_metadata_locale(language)
        if language not in configured:
            raise HTTPException(400, "Metadata language is not configured.")
        context = self._context(user_id)
        resolve = lambda: self._read_service().resolve_public(
            entity_id, row[3], self._provider_ids(entity_id, row[3]), language
        )
        resolved = context.measure("metadata", resolve) if context else resolve()
        images = resolved["metadata"].get("images")
        if isinstance(images, dict):
            for image_type, image in images.items():
                if not isinstance(image, dict):
                    continue
                if image_type == "Logo":
                    image.pop("blurHash", None)
                    continue
                local = self.local_artwork(entity_id, image_type)
                if local is None:
                    continue
                image.pop("blurHash", None)
                if local[1]:
                    image["blurHash"] = local[1]
        if include_credits:
            resolved["metadata"]["credits"] = self.credits(
                user_id, entity_id, language, resolved["metadata"].get("originalLanguage")
            )
        return resolved

    def credits(
        self, user_id: str, entity_id: str, language: str, original_language: str | None = None
    ) -> dict[str, list[dict]]:
        self.require_entity(user_id, entity_id)
        if not self._has_table("entity_person_credits"):
            return {"cast": [], "crew": []}
        available = {
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT locale FROM entity_person_credits WHERE entity_id=?",
                (entity_id,),
            )
        }
        configured = MetadataLanguageSettings().get()
        tiers = fallback_tiers(
            language, original_language, media=False, include_english="en" in configured
        )
        result = {"cast": [], "crew": []}
        for credit_type in ("cast", "crew"):
            selected_locale = None
            for tier in tiers:
                for variant in locale_variants(tier, available):
                    if self.db.execute(
                        "SELECT 1 FROM entity_person_credits WHERE entity_id=? AND locale=? AND credit_type=? LIMIT 1",
                        (entity_id, variant, credit_type),
                    ):
                        selected_locale = variant
                        break
                if selected_locale:
                    break
            if not selected_locale:
                continue
            rows = self.db.execute(
                "SELECT c.person_id,COALESCE(l.name,''),c.role,c.department,c.credit_order,p.local_path,p.image_blur_hash "
                "FROM entity_person_credits c JOIN people p ON p.id=c.person_id "
                "LEFT JOIN person_localizations l ON l.person_id=p.id AND l.locale=c.locale "
                "WHERE c.entity_id=? AND c.locale=? AND c.credit_type=? "
                "ORDER BY c.credit_order,c.id",
                (entity_id, selected_locale, credit_type),
            )
            for person_id, name, role, department, order, local_path, blur_hash in rows:
                value = {"id": person_id, "name": name, "order": order}
                if credit_type == "cast":
                    value["character"] = role
                else:
                    value["job"] = role
                    value["department"] = department
                if local_path and Path(local_path).is_file():
                    value["image"] = {
                        "url": f"/api/catalog/items/{entity_id}/people/{person_id}/image"
                    }
                    if blur_hash:
                        value["image"]["blurHash"] = blur_hash
                result[credit_type].append(value)
        return result

    def person_image(self, user_id: str, entity_id: str, person_id: str) -> Path | None:
        self.require_entity(user_id, entity_id)
        if not self._has_table("entity_person_credits"):
            return None
        rows = self.db.execute(
            "SELECT p.local_path FROM entity_person_credits c JOIN people p ON p.id=c.person_id "
            "WHERE c.entity_id=? AND c.person_id=? AND p.local_path IS NOT NULL LIMIT 1",
            (entity_id, person_id),
        )
        if not rows or not rows[0][0]:
            return None
        path = Path(rows[0][0])
        return path if path.is_file() else None

    def local_artwork(self, entity_id: str, image_type: str) -> tuple[Path, str | None] | None:
        row = self._entity_row(entity_id)
        if not row or image_type not in LOCAL_ARTWORK_NAMES:
            return None
        directory_rows = self.db.execute("SELECT directory FROM libraries WHERE id=?", (row[1],))
        if not directory_rows or not directory_rows[0][0]:
            return None
        columns = {value[1] for value in self.db.execute("PRAGMA table_info(media_files)")}
        if not columns:
            return None
        blur_field = ",image_blur_hash" if "image_blur_hash" in columns else ""
        cache = LocalArtworkCache(self.db)
        for values in self.db.execute(
            f"SELECT relative_path,quick_fingerprint{blur_field} FROM media_files WHERE entity_id=? AND role='image' ORDER BY relative_path COLLATE NOCASE",
            (entity_id,),
        ):
            relative_path, content_hash, *blur_hash = values
            candidate = Path(directory_rows[0][0]) / relative_path
            cached = cache.path(content_hash)
            if candidate.stem.lower() in LOCAL_ARTWORK_NAMES[image_type] and candidate.is_file() and cached and cached.is_file():
                return cached, blur_hash[0] if blur_hash else None
        return None

    def selected_image(
        self, user_id: str, entity_id: str, language: str, image_type: str
    ) -> dict | None:
        row = self.require_entity(user_id, entity_id)
        configured = MetadataLanguageSettings().get()
        language = normalize_metadata_locale(language)
        if language not in configured or image_type not in IMAGE_TYPES:
            raise HTTPException(400, "Unsupported metadata language or image type.")
        service = self._read_service()
        raw = service.resolve_raw(
            row[3], self._provider_ids(entity_id, row[3]), language
        )
        return choose_artwork(
            raw.get("images", []),
            language,
            image_type,
            raw.get("originalLanguage"),
            service.providers(row[3]),
        )

    @_catalog_read
    def item(self, user_id: str, entity_id: str, language: str) -> dict:
        row = self.require_entity(user_id, entity_id)
        metadata = self.metadata(user_id, entity_id, language, include_credits=True)["metadata"]
        if row[3] == "collection":
            allowed = self.allowed_libraries(user_id)
            placeholders = ",".join("?" for _ in allowed)
            children = (
                self.db.execute(
                    f"SELECT e.id FROM collection_members m JOIN library_entities e ON e.id=m.source_entity_id WHERE m.collection_entity_id=? AND e.library_id IN ({placeholders}) ORDER BY m.position,e.relative_path COLLATE NOCASE",
                    [entity_id, *allowed],
                )
                if allowed
                else []
            )
        else:
            children = self.db.execute(
                "SELECT id FROM library_entities WHERE parent_id=? ORDER BY season_number,episode_number,track_number,relative_path COLLATE NOCASE",
                (entity_id,),
            )
        return self._serialize(
            user_id, row, metadata, [child[0] for child in children], language=language
        )

    def _relationship_graph(self, user_id: str) -> tuple[dict[str, tuple[str | None, str]], dict[str, list[str]], dict[str, list[str]]]:
        context = self._context(user_id)
        if context and context.graph is not None:
            return context.graph
        def load_graph():
            return self._relationship_graph_uncached(user_id)
        graph = context.measure("graph", load_graph) if context else load_graph()
        if context:
            context.graph = graph
        return graph

    def _relationship_graph_uncached(self, user_id: str) -> tuple[dict[str, tuple[str | None, str]], dict[str, list[str]], dict[str, list[str]]]:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return {}, {}, {}
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            f"SELECT id,parent_id,entity_type FROM library_entities WHERE library_id IN ({placeholders})",
            list(allowed),
        )
        entities = {row[0]: (row[1], row[2]) for row in rows}
        children: dict[str, list[str]] = {}
        parents: dict[str, list[str]] = {}
        for entity_id, (parent_id, _) in entities.items():
            if parent_id in entities:
                children.setdefault(parent_id, []).append(entity_id)
                parents.setdefault(entity_id, []).append(parent_id)
        if self._has_table("collection_members"):
            membership_rows = self.db.execute(
                f"SELECT m.collection_entity_id,m.source_entity_id FROM collection_members m JOIN library_entities c ON c.id=m.collection_entity_id JOIN library_entities s ON s.id=m.source_entity_id WHERE c.library_id IN ({placeholders}) AND s.library_id IN ({placeholders})",
                [*allowed, *allowed],
            )
            for collection_id, source_id in membership_rows:
                if collection_id in entities and source_id in entities:
                    children.setdefault(collection_id, []).append(source_id)
                    parents.setdefault(source_id, []).append(collection_id)
        return entities, children, parents

    @staticmethod
    def _walk_children(entity_id: str, children: dict[str, list[str]]) -> list[str]:
        found: list[str] = []
        pending = list(children.get(entity_id, []))
        visited = {entity_id}
        while pending:
            child_id = pending.pop()
            if child_id in visited:
                continue
            visited.add(child_id)
            found.append(child_id)
            pending.extend(children.get(child_id, []))
        return found

    @staticmethod
    def _walk_parents(entity_id: str, parents: dict[str, list[str]]) -> list[str]:
        found: list[str] = []
        pending = list(parents.get(entity_id, []))
        visited = {entity_id}
        while pending:
            parent_id = pending.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            found.append(parent_id)
            pending.extend(parents.get(parent_id, []))
        return found

    def _playable_descendants(
        self,
        entity_id: str,
        entities: dict[str, tuple[str | None, str]],
        children: dict[str, list[str]],
    ) -> list[str]:
        context = self._read_context.get()
        if context and entity_id in context.playable_descendants:
            return context.playable_descendants[entity_id]
        leaf_types = {"movie", "episode", "track", "release"}
        result: list[str] = []
        for descendant_id in [entity_id, *self._walk_children(entity_id, children)]:
            if children.get(descendant_id):
                continue
            if entities.get(descendant_id, (None, ""))[1] in leaf_types:
                result.append(descendant_id)
        if context:
            context.playable_descendants[entity_id] = result
        return result

    def _state_rows(self, user_id: str) -> dict[str, tuple]:
        context = self._context(user_id)
        if context and context.direct_states is not None:
            return context.direct_states
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return {}
        placeholders = ",".join("?" for _ in allowed)
        def load_states():
            rows = self.db.execute(
                f"SELECT s.entity_id,s.favorite,s.played,s.play_count,s.position_seconds,s.duration_seconds,s.last_played_at "
                f"FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id "
                f"WHERE s.user_id=? AND e.library_id IN ({placeholders})",
                [user_id, *allowed],
            )
            return {row[0]: row[1:] for row in rows}
        values = context.measure("state_preload", load_states) if context else load_states()
        if context:
            context.direct_states = values
        return values

    def _state_row(self, user_id: str, entity_id: str, cursor=None):
        query = "SELECT favorite,played,play_count,position_seconds,duration_seconds,last_played_at FROM user_item_state WHERE user_id=? AND entity_id=?"
        rows = cursor.execute(query, (user_id, entity_id)).fetchall() if cursor else self.db.execute(query, (user_id, entity_id))
        return rows[0] if rows else None

    @staticmethod
    def _direct_state(row) -> dict:
        if not row:
            return {
                "favorite": False,
                "played": False,
                "playCount": 0,
                "positionSeconds": 0,
                "durationSeconds": 0,
                "playedPercentage": None,
            }
        position = max(0.0, float(row[3] or 0))
        duration = max(0.0, float(row[4] or 0))
        percentage = None if position <= 0 or duration <= 0 else min(100.0, position / duration * 100)
        return {
            "favorite": bool(row[0]),
            "played": bool(row[1]),
            "playCount": int(row[2] or 0),
            "positionSeconds": position,
            "durationSeconds": duration,
            "playedPercentage": percentage,
            "lastPlayedAt": row[5],
        }

    def _state(self, user_id: str, entity_id: str) -> dict:
        entities, children, _ = self._relationship_graph(user_id)
        context = self._context(user_id)
        row = self._state_rows(user_id).get(entity_id) if context else self._state_row(user_id, entity_id)
        direct = self._direct_state(row)
        leaves = self._playable_descendants(entity_id, entities, children)
        if not leaves or (len(leaves) == 1 and leaves[0] == entity_id):
            if entities.get(entity_id, (None, ""))[1] in {"series", "season", "collection", "artist", "release"}:
                direct["played"] = False
                direct["playedPercentage"] = None
                direct["unplayedItemCount"] = 0
            return direct
        states = self._state_rows(user_id) if context else None
        leaf_states = [
            self._direct_state(states.get(leaf_id) if states is not None else self._state_row(user_id, leaf_id))
            for leaf_id in leaves
        ]
        direct["played"] = bool(leaf_states) and all(state["played"] for state in leaf_states)
        direct["unplayedItemCount"] = sum(not state["played"] for state in leaf_states)
        direct["playedPercentage"] = None
        return direct

    def _serialize(
        self,
        user_id: str,
        row,
        metadata: dict,
        children: list[str] | None = None,
        dates: dict | None = None,
        series_name: str | None = None,
        language: str | None = None,
    ) -> dict:
        season_id = row[2] if row[3] == "episode" else None
        series_id = row[2] if row[3] == "season" else None
        if season_id:
            parent = self._entity_row(season_id)
            series_id = parent[2] if parent else None
        series_primary_image = (
            self._series_primary_image(user_id, series_id, language)
            if row[3] == "episode" and series_id and language
            else None
        )
        return {
            "id": row[0],
            "libraryId": row[1],
            "parentId": row[2],
            "type": row[3],
            "seriesId": series_id,
            "seriesName": series_name,
            "seriesPrimaryImage": series_primary_image,
            "seasonId": season_id,
            "name": metadata.get("title") or Path(row[4] or "").stem or row[3].title(),
            "seasonNumber": row[5],
            "episodeNumber": row[6],
            "episodeEndNumber": row[7],
            "dateAdded": (dates or {}).get("addedAt", row[8]),
            "addedAt": (dates or {}).get("addedAt", row[8]),
            "lastAddedAt": (dates or {}).get("lastAddedAt", row[8]),
            "updatedAt": row[9],
            "metadata": metadata,
            "userState": self._state(user_id, row[0]),
            "childIds": children or [],
        }

    def _series_primary_image(
        self, user_id: str, series_id: str, language: str
    ) -> dict | None:
        try:
            series = self.metadata(user_id, series_id, language)["metadata"]
        except HTTPException as error:
            if error.status_code != 404:
                raise
            return None
        images = series.get("images")
        primary = images.get("Primary") if isinstance(images, dict) else None
        return primary if isinstance(primary, dict) else None

    def _date_values(
        self,
        library_id: str,
        allowed_library_ids: set[str] | None = None,
        root_ids: set[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Return filesystem-derived Added and Last added values per entity."""
        scope = allowed_library_ids or {library_id}
        placeholders = ",".join("?" for _ in scope)
        root_filter = ""
        root_params: list[str] = []
        if root_ids is not None:
            if not root_ids:
                return {}
            root_filter = f" AND id IN ({','.join('?' for _ in root_ids)})"
            root_params = list(root_ids)
        rows = self.db.execute(
            f"SELECT id,parent_id,entity_type,created_at FROM library_entities WHERE library_id IN ({placeholders}){root_filter}",
            [*scope, *root_params],
        )
        entities = {row[0]: row for row in rows}
        if not entities:
            return {}
        if not self._has_table("media_files"):
            return {
                entity_id: {"addedAt": row[3] or "", "lastAddedAt": row[3] or ""}
                for entity_id, row in entities.items()
            }
        collection_step = ""
        collection_params: list[str] = []
        if self._has_table("collection_members"):
            collection_step = f"""
                UNION
                SELECT member.collection_entity_id, member.source_entity_id
                FROM collection_members member
                JOIN library_entities collection
                  ON collection.id = member.collection_entity_id
                JOIN library_entities source ON source.id = member.source_entity_id
                WHERE collection.library_id IN ({placeholders})
                  AND source.library_id IN ({placeholders})
            """
            collection_params = [*scope, *scope]
        date_rows = self.db.execute(
            f"""
            WITH RECURSIVE edges(parent_id, child_id) AS (
                SELECT parent_id, id
                FROM library_entities
                WHERE parent_id IS NOT NULL AND library_id IN ({placeholders})
                {collection_step}
            ), entity_tree(root_id, entity_id) AS (
                SELECT id, id FROM library_entities
                WHERE library_id IN ({placeholders}){root_filter}
                UNION
                SELECT entity_tree.root_id, edges.child_id
                FROM entity_tree
                JOIN edges ON edges.parent_id = entity_tree.entity_id
            )
            SELECT entity_tree.root_id, MIN(media_files.modified_ns), MAX(media_files.modified_ns)
            FROM entity_tree
            LEFT JOIN media_files
              ON media_files.entity_id = entity_tree.entity_id
             AND media_files.role = 'media'
             AND media_files.modified_ns IS NOT NULL
            GROUP BY entity_tree.root_id
            """,
            [*scope, *collection_params, *scope, *root_params],
        )
        values = {}
        for entity_id, added_ns, last_added_ns in date_rows:
            fallback = entities[entity_id][3] or ""
            added = (
                datetime.fromtimestamp(added_ns / 1_000_000_000, tz=timezone.utc).isoformat()
                if added_ns is not None else fallback
            )
            last_added = (
                datetime.fromtimestamp(last_added_ns / 1_000_000_000, tz=timezone.utc).isoformat()
                if last_added_ns is not None else fallback
            )
            values[entity_id] = {"addedAt": added, "lastAddedAt": last_added}
        return values

    @_catalog_read
    def list_items(
        self,
        user_id: str,
        library_id: str,
        language: str,
        *,
        parent_id: str | None = None,
        page: int = 1,
        page_size: int = 40,
        sort_by: str | None = None,
        sort_order: str = "ascending",
    ) -> dict:
        library = self.require_library(user_id, library_id)
        if parent_id:
            parent = self.require_entity(user_id, parent_id)
            if parent[1] != library_id:
                raise HTTPException(404, "Item not found.")
        rows = self.db.execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE library_id=? AND parent_id IS ?",
            (library_id, parent_id),
        )
        hierarchy_parent = None
        if parent_id:
            hierarchy_parent = self.require_entity(user_id, parent_id)[3]
        total = len(rows)
        display_rows = rows
        if sort_by is None and hierarchy_parent in {"series", "season"}:
            def row_hierarchy_key(row):
                season = row[5]
                episode = row[6]
                if hierarchy_parent == "season":
                    return (
                        episode is None,
                        episode if episode is not None else 0,
                        row[0],
                    )
                return (
                    season is None,
                    season if season is not None else 0,
                    episode is None,
                    episode if episode is not None else 0,
                    row[0],
                )

            ordered_rows = sorted(rows, key=row_hierarchy_key)
            start = (page - 1) * page_size
            display_rows = ordered_rows[start : start + page_size]
        context = self._context(user_id)
        date_scope = self.allowed_libraries(user_id) if library["type"] == "collection" else {library_id}
        roots_for_dates = None if sort_by in {"added", "lastAdded"} else {row[0] for row in display_rows}
        date_values = lambda: self._date_values(library_id, date_scope, roots_for_dates)
        dates = context.measure("date_sort", date_values) if context else date_values()
        if sort_by in {"added", "lastAdded"} and display_rows is rows:
            date_field = "addedAt" if sort_by == "added" else "lastAddedAt"
            grouped_rows: dict[str, list] = {}
            for row in rows:
                date_value = (dates.get(row[0]) or {}).get(date_field, "")
                grouped_rows.setdefault(date_value, []).append(row)
            ordered_dates = sorted(
                grouped_rows, reverse=sort_order.lower() == "descending"
            )
            candidate_rows = []
            end = (page - 1) * page_size + page_size
            for date_value in ordered_dates:
                candidate_rows.extend(grouped_rows[date_value])
                if len(candidate_rows) >= end:
                    break
            display_rows = candidate_rows
        def serialize_values():
            values = []
            for row in display_rows:
                metadata = self.metadata(user_id, row[0], language)["metadata"]
                values.append(
                    self._serialize(
                        user_id, row, metadata, dates=dates.get(row[0]), language=language
                    )
                )
            return values
        values = context.measure("serialization", serialize_values) if context else serialize_values()
        if sort_by is None and hierarchy_parent in {"series", "season"}:
            def hierarchy_key(value):
                season = value.get("seasonNumber")
                episode = value.get("episodeNumber")
                if hierarchy_parent == "season":
                    return (
                        episode is None,
                        episode if episode is not None else 0,
                        value["id"],
                    )
                return (
                    season is None,
                    season if season is not None else 0,
                    episode is None,
                    episode if episode is not None else 0,
                    value["id"],
                )

            values.sort(key=hierarchy_key)
        else:
            reverse = sort_order.lower() == "descending"
            selected_sort = sort_by if sort_by in {"rating", "title", "added", "lastAdded", "release", "runtime"} else "title"
            key = {
                "added": lambda value: value.get("addedAt") or "",
                "lastAdded": lambda value: value.get("lastAddedAt") or "",
                "release": lambda value: value["metadata"].get("date") or "",
                "rating": lambda value: value["metadata"].get("communityRating") or 0,
                "runtime": lambda value: value["metadata"].get("runtimeMinutes") or 0,
            }.get(selected_sort, lambda value: str(value.get("name") or "").casefold())
            values.sort(key=lambda value: (key(value), str(value.get("name") or "").casefold(), value["id"]), reverse=reverse)
        if sort_by is None and hierarchy_parent in {"series", "season"}:
            return {
                "items": values,
                "page": page,
                "pageSize": page_size,
                "total": total,
            }
        start = (page - 1) * page_size
        return {
            "items": values[start : start + page_size],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    @staticmethod
    def _search_text(value: str) -> str:
        return " ".join(
            "".join(
                character if character.isalnum() else " "
                for character in unicodedata.normalize("NFKC", value).casefold()
            ).split()
        )

    @_catalog_read
    def search(
        self, user_id: str, query: str, language: str, page: int, page_size: int
    ) -> dict:
        wanted = self._search_text(query)
        if not wanted:
            return {"items": [], "page": page, "pageSize": page_size, "total": 0}
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return {"items": [], "page": page, "pageSize": page_size, "total": 0}
        configured_language = normalize_metadata_locale(language)
        if configured_language not in MetadataLanguageSettings().get():
            raise HTTPException(400, "Metadata language is not configured.")
        placeholders = ",".join("?" for _ in allowed)
        locale_order = [configured_language]
        if configured_language != "en" and "en" in MetadataLanguageSettings().get():
            locale_order.append("en")
        locale_order.append("original")
        locale_placeholders = ",".join("?" for _ in locale_order)
        if len(wanted) >= 3:
            indexed = self.db.execute(
                f"SELECT entity_id,locale,title,bm25(catalog_search) FROM catalog_search WHERE catalog_search MATCH ? AND library_id IN ({placeholders}) AND locale IN ({locale_placeholders})",
                [f'"{wanted.replace(chr(34), chr(34) * 2)}"', *allowed, *locale_order],
            )
        else:
            indexed = self.db.execute(
                f"SELECT entity_id,locale,title,0 FROM catalog_search WHERE title LIKE ? ESCAPE '\\' AND library_id IN ({placeholders}) AND locale IN ({locale_placeholders})",
                [
                    f"%{wanted.replace('%', r'\%').replace('_', r'\_')}%",
                    *allowed,
                    *locale_order,
                ],
            )
        indexed_by_entity: dict[str, list[tuple]] = {}
        for indexed_row in indexed:
            indexed_by_entity.setdefault(indexed_row[0], []).append(indexed_row)
        if not indexed_by_entity:
            return {"items": [], "page": page, "pageSize": page_size, "total": 0}
        entity_placeholders = ",".join("?" for _ in indexed_by_entity)
        rows = self.db.execute(
            f"SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE id IN ({entity_placeholders}) AND library_id IN ({placeholders}) AND entity_type IN ('movie','series','collection')",
            [*indexed_by_entity, *allowed],
        )
        ranked = []
        for row in rows:
            metadata = self.metadata(user_id, row[0], language)["metadata"]
            candidates = indexed_by_entity[row[0]]
            best = None
            for _, locale, raw_title, fts_score in candidates:
                title = self._search_text(str(raw_title or ""))
                match_rank = (
                    0
                    if title == wanted
                    else 1
                    if title.startswith(wanted)
                    else 2
                    if wanted in title
                    else 3
                )
                language_rank = locale_order.index(locale)
                candidate = (match_rank, language_rank, float(fts_score or 0), title)
                best = min(best, candidate) if best is not None else candidate
            ranked.append(
                (*best, row[0], self._serialize(user_id, row, metadata, language=language))
            )
        ranked.sort(key=lambda value: value[:3])
        values = [value[5] for value in ranked]
        start = (page - 1) * page_size
        return {
            "items": values[start : start + page_size],
            "page": page,
            "pageSize": page_size,
            "total": len(values),
        }

    def update_state(self, user_id: str, entity_id: str, changes: dict) -> dict:
        self.require_entity(user_id, entity_id)
        if not isinstance(changes, dict):
            raise HTTPException(400, "Invalid item state.")
        entities, children, parents = self._relationship_graph(user_id)
        current_row = self._state_row(user_id, entity_id)
        current = self._direct_state(current_row)
        try:
            position = float(changes.get("positionSeconds", current["positionSeconds"]))
            duration = float(changes.get("durationSeconds", current["durationSeconds"]))
        except (TypeError, ValueError) as error:
            raise HTTPException(400, "Invalid playback position.") from error
        if not math.isfinite(position) or not math.isfinite(duration):
            raise HTTPException(400, "Invalid playback position.")
        position = max(0.0, position)
        duration = max(0.0, duration)
        explicit_played = changes.get("played")
        played = (
            bool(explicit_played)
            if explicit_played is not None
            else bool(duration and position / duration >= 0.9)
        )
        favorite = bool(changes.get("favorite", current["favorite"]))
        descendants = self._walk_children(entity_id, children) if explicit_played is not None else []
        affected = [entity_id, *descendants]
        ancestor_ids = self._walk_parents(entity_id, parents)
        now = _now()
        with self.db.transaction() as cursor:
            states = {
                affected_id: self._direct_state(self._state_row(user_id, affected_id, cursor))
                for affected_id in affected
            }
            original_played = {
                affected_id: state["played"] for affected_id, state in states.items()
            }
            states[entity_id].update(
                favorite=favorite,
                played=played,
                positionSeconds=0 if played or explicit_played is False else position,
                durationSeconds=duration,
            )
            if explicit_played is False:
                for descendant_id in descendants:
                    states[descendant_id].update(played=False, positionSeconds=0)
            elif explicit_played is True:
                for descendant_id in descendants:
                    states[descendant_id].update(played=True, positionSeconds=0)
            for ancestor_id in ancestor_ids:
                leaves = self._playable_descendants(ancestor_id, entities, children)
                if not leaves:
                    continue
                leaf_states = [states.get(leaf_id) or self._direct_state(self._state_row(user_id, leaf_id, cursor)) for leaf_id in leaves]
                ancestor = self._direct_state(self._state_row(user_id, ancestor_id, cursor))
                ancestor.update(played=all(state["played"] for state in leaf_states), positionSeconds=0)
                states[ancestor_id] = ancestor
                affected.append(ancestor_id)
            for affected_id in dict.fromkeys(affected):
                state = states[affected_id]
                was_played = original_played.get(affected_id, False)
                next_played = bool(state["played"])
                play_count = state["playCount"] + int(next_played and not was_played)
                cursor.execute(
                    "INSERT INTO user_item_state(user_id,entity_id,favorite,played,play_count,position_seconds,duration_seconds,last_played_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id,entity_id) DO UPDATE SET favorite=excluded.favorite,played=excluded.played,play_count=excluded.play_count,position_seconds=excluded.position_seconds,duration_seconds=excluded.duration_seconds,last_played_at=excluded.last_played_at,updated_at=excluded.updated_at",
                    (user_id, affected_id, int(state["favorite"]), int(next_played), play_count, state["positionSeconds"], state["durationSeconds"], now if state["positionSeconds"] or next_played else state.get("lastPlayedAt"), now),
                )
        return self._state(user_id, entity_id)

    @_catalog_read
    def favorites(
        self,
        user_id: str,
        language: str,
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
    ) -> dict:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return {"items": [], "page": page, "pageSize": page_size, "total": 0}
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id WHERE s.user_id=? AND s.favorite=1 AND e.library_id IN ({placeholders})",
            [user_id, *allowed],
        )
        values = [
            self._serialize(
                user_id,
                row,
                self.metadata(user_id, row[0], language)["metadata"],
                language=language,
            )
            for row in rows
        ]
        key = (
            (lambda value: value.get("dateAdded") or "")
            if sort_by.lower() in {"datecreated", "dateadded"}
            else (lambda value: str(value.get("name") or "").casefold())
        )
        values.sort(
            key=lambda value: (key(value), value["id"]),
            reverse=sort_order.lower() == "descending",
        )
        start = (page - 1) * page_size
        return {
            "items": values[start : start + page_size],
            "page": page,
            "pageSize": page_size,
            "total": len(values),
        }

    @_catalog_read
    def similar(
        self, user_id: str, entity_id: str, language: str, limit: int = 8
    ) -> dict:
        source_row = self.require_entity(user_id, entity_id)
        source = self.metadata(user_id, entity_id, language)["metadata"]
        source_terms = {
            str(value).casefold()
            for value in (source.get("genres") or source.get("tags") or [])
        }
        allowed = self.allowed_libraries(user_id)
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            f"SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE id<>? AND library_id IN ({placeholders}) AND entity_type=?",
            [entity_id, *allowed, source_row[3]],
        )
        ranked = []
        for row in rows:
            metadata = self.metadata(user_id, row[0], language)["metadata"]
            terms = {
                str(value).casefold()
                for value in (metadata.get("genres") or metadata.get("tags") or [])
            }
            score = len(source_terms & terms)
            if score:
                ranked.append(
                    (
                        -score,
                        str(metadata.get("title") or "").casefold(),
                        row[0],
                        self._serialize(user_id, row, metadata, language=language),
                    )
                )
        ranked.sort(key=lambda value: value[:3])
        return {"items": [value[3] for value in ranked[:limit]]}

    def _home_series_name(
        self,
        user_id: str,
        language: str,
        series_id: str | None,
        names: dict[str, str],
    ) -> str | None:
        if not series_id:
            return None
        if series_id not in names:
            series_row = self._entity_row(series_id)
            series_metadata = self.metadata(user_id, series_id, language)["metadata"]
            names[series_id] = str(
                series_metadata.get("title")
                or Path(series_row[4] if series_row else "").stem
                or "Series"
            )
        return names[series_id]

    def _home_discovery_items(
        self, user_id: str, language: str, allowed: set[str]
    ) -> list[dict]:
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            f"SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE library_id IN ({placeholders}) AND entity_type IN ('movie','series','collection') ORDER BY created_at DESC LIMIT 100",
            list(allowed),
        )
        dates = self._date_values("", allowed)
        return [
            self._serialize(
                user_id,
                row,
                self.metadata(user_id, row[0], language)["metadata"],
                dates=dates.get(row[0]),
                language=language,
            )
            for row in rows
        ]

    @_catalog_read
    def home_featured(self, user_id: str, language: str) -> list[dict]:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return []
        return self._home_discovery_items(user_id, language, allowed)[:25]

    @_catalog_read
    def home_continue_watching(self, user_id: str, language: str) -> list[dict]:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,series.id "
            f"FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id "
            f"LEFT JOIN library_entities season ON e.entity_type='episode' AND season.id=e.parent_id "
            f"LEFT JOIN library_entities series ON series.id=season.parent_id "
            f"WHERE s.user_id=? AND e.library_id IN ({placeholders}) AND s.duration_seconds>0 AND s.position_seconds/s.duration_seconds>=0.02 AND s.position_seconds/s.duration_seconds<0.9 ORDER BY s.last_played_at DESC LIMIT 18",
            [user_id, *allowed],
        )
        series_names: dict[str, str] = {}
        return [
            self._serialize(
                user_id,
                row[:10],
                self.metadata(user_id, row[0], language)["metadata"],
                series_name=self._home_series_name(user_id, language, row[10], series_names),
                language=language,
            )
            for row in rows
        ]

    @_catalog_read
    def home_next_up(self, user_id: str, language: str) -> list[dict]:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,series.id "
            f"FROM library_entities e JOIN library_entities season ON season.id=e.parent_id JOIN library_entities series ON series.id=season.parent_id "
            f"WHERE e.entity_type='episode' AND e.library_id IN ({placeholders}) ORDER BY series.id,e.season_number,e.episode_number,e.relative_path COLLATE NOCASE",
            list(allowed),
        )
        by_series: dict[str, list[tuple]] = {}
        for row in rows:
            by_series.setdefault(row[10], []).append(row[:10])
        series_names: dict[str, str] = {}
        next_up = []
        for series_id, episodes in by_series.items():
            states = [self._state(user_id, episode[0]) for episode in episodes]
            if not any(state["played"] or state["positionSeconds"] > 0 for state in states):
                continue
            candidate = next(
                (episode for episode, state in zip(episodes, states) if not state["played"]),
                None,
            )
            if candidate:
                next_up.append(
                    self._serialize(
                        user_id,
                        candidate,
                        self.metadata(user_id, candidate[0], language)["metadata"],
                        series_name=self._home_series_name(user_id, language, series_id, series_names),
                        language=language,
                    )
                )
        next_up.sort(key=lambda value: value["userState"].get("lastPlayedAt") or "", reverse=True)
        return next_up[:18]

    @_catalog_read
    def home_derived(
        self, user_id: str, language: str, discovery_items: list[dict] | None = None
    ) -> dict:
        allowed = self.allowed_libraries(user_id)
        empty = {"myList": [], "recentlyPlayed": [], "genreRows": []}
        if not allowed:
            return empty
        placeholders = ",".join("?" for _ in allowed)
        favorite_rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id WHERE s.user_id=? AND s.favorite=1 AND e.library_id IN ({placeholders})",
            [user_id, *allowed],
        )
        my_list = [
            self._serialize(
                user_id,
                row,
                self.metadata(user_id, row[0], language)["metadata"],
                language=language,
            )
            for row in favorite_rows
        ]
        my_list.sort(
            key=lambda value: (str(value.get("name") or "").casefold(), value["id"])
        )

        recent_rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,series.id "
            f"FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id "
            f"LEFT JOIN library_entities season ON e.entity_type='episode' AND season.id=e.parent_id "
            f"LEFT JOIN library_entities series ON series.id=season.parent_id "
            f"WHERE s.user_id=? AND e.library_id IN ({placeholders}) AND e.entity_type IN ('movie','episode') "
            f"AND s.last_played_at IS NOT NULL AND NOT (s.duration_seconds>0 AND s.position_seconds/s.duration_seconds>=0.02 AND s.position_seconds/s.duration_seconds<0.9) "
            f"ORDER BY s.last_played_at DESC,e.id LIMIT 18",
            [user_id, *allowed],
        )
        series_names: dict[str, str] = {}
        recently_played = [
            self._serialize(
                user_id,
                row[:10],
                self.metadata(user_id, row[0], language)["metadata"],
                series_name=self._home_series_name(user_id, language, row[10], series_names),
                language=language,
            )
            for row in recent_rows
        ]

        candidates = discovery_items or self._home_discovery_items(
            user_id, language, allowed
        )
        genre_candidates = [
            item for item in candidates if item.get("type") in {"movie", "series"}
        ]
        genre_names: dict[str, str] = {}
        genre_counts: dict[str, int] = {}
        for item in genre_candidates:
            metadata = item.get("metadata") or {}
            tags = metadata.get("genres") or metadata.get("tags") or []
            item_genres = {
                tag.strip().casefold(): tag.strip()
                for tag in tags
                if isinstance(tag, str) and tag.strip()
            }
            for key, genre in item_genres.items():
                genre_names.setdefault(key, genre)
                genre_counts[key] = genre_counts.get(key, 0) + 1
        genre_rows = []
        for key in sorted(
            genre_counts, key=lambda value: (-genre_counts[value], genre_names[value].casefold())
        )[:3]:
            items = [
                item
                for item in genre_candidates
                if any(
                    isinstance(tag, str) and tag.strip().casefold() == key
                    for tag in ((item.get("metadata") or {}).get("genres") or (item.get("metadata") or {}).get("tags") or [])
                )
            ][:18]
            if items:
                genre_rows.append({"genre": genre_names[key], "items": items})
        return {
            "myList": my_list[:18],
            "recentlyPlayed": recently_played,
            "genreRows": genre_rows,
        }

    @_catalog_read
    def home(self, user_id: str, language: str) -> dict:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return {
                "latestItems": [],
                "continueWatching": [],
                "nextUp": [],
                "libraryRows": [],
                "myList": [],
                "recentlyPlayed": [],
                "genreRows": [],
            }
        placeholders = ",".join("?" for _ in allowed)
        dates = self._date_values("", allowed)
        items = self._home_discovery_items(user_id, language, allowed)
        resume_rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,series.id "
            f"FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id "
            f"LEFT JOIN library_entities season ON e.entity_type='episode' AND season.id=e.parent_id "
            f"LEFT JOIN library_entities series ON series.id=season.parent_id "
            f"WHERE s.user_id=? AND e.library_id IN ({placeholders}) AND s.duration_seconds>0 AND s.position_seconds/s.duration_seconds>=0.02 AND s.position_seconds/s.duration_seconds<0.9 ORDER BY s.last_played_at DESC LIMIT 18",
            [user_id, *allowed],
        )
        series_names: dict[str, str] = {}

        def series_name(series_id: str | None) -> str | None:
            if not series_id:
                return None
            if series_id not in series_names:
                series_row = self._entity_row(series_id)
                series_metadata = self.metadata(user_id, series_id, language)["metadata"]
                series_names[series_id] = str(
                    series_metadata.get("title")
                    or Path(series_row[4] if series_row else "").stem
                    or "Series"
                )
            return series_names[series_id]

        resume = [
            self._serialize(
                user_id,
                row[:10],
                self.metadata(user_id, row[0], language)["metadata"],
                series_name=series_name(row[10]),
                language=language,
            )
            for row in resume_rows
        ]
        next_rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,series.id "
            f"FROM library_entities e JOIN library_entities season ON season.id=e.parent_id JOIN library_entities series ON series.id=season.parent_id "
            f"WHERE e.entity_type='episode' AND e.library_id IN ({placeholders}) ORDER BY series.id,e.season_number,e.episode_number,e.relative_path COLLATE NOCASE",
            list(allowed),
        )
        next_up = []
        by_series = {}
        for row in next_rows:
            by_series.setdefault(row[10], []).append(row[:10])
        for series_id, episodes in by_series.items():
            states = [self._state(user_id, episode[0]) for episode in episodes]
            if not any(
                state["played"] or state["positionSeconds"] > 0 for state in states
            ):
                continue
            candidate = next(
                (
                    episode
                    for episode, state in zip(episodes, states)
                    if not state["played"]
                ),
                None,
            )
            if candidate:
                next_up.append(
                    self._serialize(
                        user_id,
                        candidate,
                        self.metadata(user_id, candidate[0], language)["metadata"],
                        series_name=series_name(series_id),
                        language=language,
                    )
                )
        next_up.sort(
            key=lambda value: value["userState"].get("lastPlayedAt") or "", reverse=True
        )
        library_rows = []
        by_library = {library["id"]: library for library in self.libraries(user_id)}
        for library_id, library in by_library.items():
            newly_added = []
            if self._has_table("media_files") and library["type"] in {"movies", "tv_series"}:
                entity_type = "movie" if library["type"] == "movies" else "episode"
                playable_rows = self.db.execute(
                    "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at "
                    "FROM library_entities e WHERE e.library_id=? AND e.entity_type=? "
                    "AND EXISTS (SELECT 1 FROM media_files f WHERE f.entity_id=e.id AND f.role='media')",
                    (library_id, entity_type),
                )
                ordered_playable_rows = sorted(
                    playable_rows,
                    key=lambda row: str(row[4] or "").casefold(),
                )
                ordered_playable_rows.sort(
                    key=lambda row: (dates.get(row[0]) or {}).get("lastAddedAt") or "",
                    reverse=True,
                )
                for playable_row in ordered_playable_rows[:18]:
                    episode_series_id = None
                    if entity_type == "episode":
                        season = self._entity_row(playable_row[2])
                        episode_series_id = season[2] if season else None
                    newly_added.append(
                        self._serialize(
                            user_id,
                            playable_row,
                            self.metadata(user_id, playable_row[0], language)["metadata"],
                            dates=dates.get(playable_row[0]),
                            series_name=series_name(episode_series_id),
                            language=language,
                        )
                    )
                newly_added.sort(
                    key=lambda value: (
                        str(value.get("name") or "").casefold(),
                        value["id"],
                    )
                )
                newly_added.sort(
                    key=lambda value: value.get("lastAddedAt") or "", reverse=True
                )
                newly_added = newly_added[:18]
            if newly_added:
                library_rows.append(
                    {
                        "libraryId": library_id,
                        "libraryName": library["name"],
                        "titleKey": "newlyAddedOn",
                        "stackEpisodes": library["type"] == "tv_series",
                        "items": newly_added,
                    }
                )
            library_items = [
                value for value in items if value["libraryId"] == library_id
            ]
            rated = sorted(
                library_items,
                key=lambda value: value["metadata"].get("communityRating") or 0,
                reverse=True,
            )[:18]
            released = sorted(
                library_items,
                key=lambda value: value["metadata"].get("date") or "",
                reverse=True,
            )[:18]
            if rated:
                library_rows.append(
                    {
                        "libraryId": library_id,
                        "libraryName": library["name"],
                        "titleKey": "topRated",
                        "items": rated,
                    }
                )
            if released:
                library_rows.append(
                    {
                        "libraryId": library_id,
                        "libraryName": library["name"],
                        "titleKey": "newReleases",
                        "items": released,
                    }
                )
        return {
            "latestItems": items[:25],
            "continueWatching": resume,
            "nextUp": next_up[:18],
            "libraryRows": library_rows,
            **self.home_derived(user_id, language, items),
        }
