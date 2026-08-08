from __future__ import annotations

import unicodedata
import math
import contextvars
import time
import json
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


def _date_from_ns(value: int | None) -> str:
    return (
        datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat()
        if value is not None
        else ""
    )


class _CatalogDatabase:
    def __init__(self, database):
        self._database = database

    def execute(self, query, params=None):
        context = Catalog._read_context.get() if "Catalog" in globals() else None
        if context is not None:
            context.query_count += 1
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
        self.projected_states: dict[str, tuple] = {}
        self.empty_state_counts: dict[str, int] = {}
        self.projected_metadata: dict[tuple[str, str], dict] = {}
        self.playable_descendants: dict[str, list[str]] = {}
        self.resolved_states: dict[str, dict] = {}
        self.series_primary_images: dict[tuple[str, str], dict | None] = {}
        self.metadata_service = MetadataReadService(catalog.db)
        self.configured_languages: list[str] | None = None
        self.allowed_library_ids: set[str] | None = None
        self.table_presence: dict[str, bool] = {}
        self.timings: dict[str, float] = {}
        self.date_values: dict[
            tuple[frozenset[str], frozenset[str] | None], dict[str, dict[str, str]]
        ] = {}
        self.date_root_values: dict[
            tuple[frozenset[str], str], dict[str, str]
        ] = {}
        self.date_requested_roots = 0
        self.date_rollup_hits = 0
        self.date_fallback_roots = 0
        self.date_scan_states: set[str] = set()
        self.selected_rows = 0
        self.query_count = 0

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
            if context.date_requested_roots:
                details = " ".join(
                    part
                    for part in (
                        details,
                        f"date_requested_roots={context.date_requested_roots}",
                        f"date_rollup_hits={context.date_rollup_hits}",
                        f"date_fallback_roots={context.date_fallback_roots}",
                        "date_scan_states="
                        + ",".join(sorted(context.date_scan_states)),
                    )
                    if part
                )
            if context is not None:
                details = " ".join(
                    part for part in (
                        details,
                        f"selected_rows={context.selected_rows}",
                        f"query_count={context.query_count}",
                    ) if part
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
        context = self._context(user_id)
        if context and context.allowed_library_ids is not None:
            return set(context.allowed_library_ids)
        values = {
            row[0]
            for row in self.db.execute(
                "SELECT library_id FROM user_library_access WHERE user_id=?",
                (user_id,),
            )
        }
        if context:
            context.allowed_library_ids = set(values)
        return values

    def _table_exists(self, name: str) -> bool:
        context = self._read_context.get()
        if context and name in context.table_presence:
            return context.table_presence[name]
        value = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
        )
        if context:
            context.table_presence[name] = value
        return value

    def _has_table(self, name: str) -> bool:
        return self._table_exists(name)

    def _read_model_ready(self) -> bool:
        if not all(
            self._has_table(name)
            for name in (
                "catalog_entity_summary",
                "catalog_item_projection",
                "catalog_read_model_status",
            )
        ):
            return False
        rows = self.db.execute("SELECT state FROM catalog_read_model_status WHERE id=1")
        return bool(rows and rows[0][0] == "ready")

    def _library_supports_last_added(self, library_ids: set[str]) -> set[str]:
        if not library_ids:
            return set()
        placeholders = ",".join("?" for _ in library_ids)
        supported = {
            row[0]
            for row in self.db.execute(
                "SELECT l.id FROM libraries l "
                f"WHERE l.id IN ({placeholders}) AND EXISTS ("
                "SELECT 1 FROM library_entities child "
                "WHERE child.library_id=l.id AND child.parent_id IS NOT NULL LIMIT 1)",
                sorted(library_ids),
            )
        }
        if self._has_table("collection_members"):
            supported.update(
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT parent.library_id "
                    "FROM collection_members member "
                    "JOIN library_entities parent ON parent.id=member.collection_entity_id "
                    f"WHERE parent.library_id IN ({placeholders})",
                    sorted(library_ids),
                )
            )
        return supported

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
        generation = (
            self.db.execute(
                "SELECT generation FROM catalog_projection_status WHERE library_id=?",
                (library_id,),
            )
            if self._has_table("catalog_projection_status")
            else []
        )
        context = self._context(user_id)
        supported = (
            context.measure("library_capabilities", lambda: self._library_supports_last_added({library_id}))
            if context
            else self._library_supports_last_added({library_id})
        )
        return {
            "id": row[0],
            "name": row[1],
            "type": row[2],
            "scanState": row[3],
            "lastScanFinishedAt": row[4],
            "supportsLastAdded": library_id in supported,
            "catalogGeneration": int(generation[0][0]) if generation else 0,
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
        context = self._context(user_id)
        supported = (
            context.measure(
                "library_capabilities",
                lambda: self._library_supports_last_added({row[0] for row in rows}),
            )
            if context
            else self._library_supports_last_added({row[0] for row in rows})
        )
        generations = {}
        if rows and self._has_table("catalog_projection_status"):
            generations = {
                row[0]: int(row[1])
                for row in self.db.execute(
                    f"SELECT library_id,generation FROM catalog_projection_status WHERE library_id IN ({','.join('?' for _ in rows)})",
                    [row[0] for row in rows],
                )
            }
        return [
            {
                "id": row[0],
                "name": row[1],
                "type": row[2],
                "scanState": row[3],
                "lastScanFinishedAt": row[4],
                "supportsLastAdded": row[0] in supported,
                "catalogGeneration": generations.get(row[0], 0),
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
        projected = context.projected_metadata.get((entity_id, language)) if context else None
        if (
            isinstance(projected, dict)
            and not include_credits
            and isinstance(projected.get("images"), dict)
        ):
            return {"metadata": projected}
        projection_table = (
            "catalog_item_projection"
            if self._read_model_ready() and self._has_table("catalog_item_projection")
            else "catalog_metadata_projection"
            if self._has_table("catalog_metadata_projection")
            else None
        )
        if not include_credits and projection_table:
            rows = self.db.execute(
                f"SELECT payload FROM {projection_table} WHERE entity_id=? AND locale=?",
                (entity_id, language),
            )
            if rows:
                try:
                    value = json.loads(rows[0][0])
                    if isinstance(value, dict) and isinstance(value.get("images"), dict):
                        if context:
                            context.projected_metadata[(entity_id, language)] = value
                        return {"metadata": value}
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
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
        context = self._context(user_id)
        if context and entity_id in context.resolved_states:
            return dict(context.resolved_states[entity_id])
        if context and entity_id in context.projected_states:
            row = context.projected_states[entity_id]
            direct = self._direct_state(
                (row[0], row[1], row[2], row[5], row[6], row[7])
            )
            direct["played"] = bool(row[3]) and not bool(row[4])
            direct["playedPercentage"] = None
            direct["unplayedItemCount"] = int(row[4])
            context.resolved_states[entity_id] = dict(direct)
            return direct
        if context and entity_id in context.empty_state_counts:
            direct = self._direct_state(None)
            direct["played"] = False
            direct["playedPercentage"] = None
            direct["unplayedItemCount"] = context.empty_state_counts[entity_id]
            context.resolved_states[entity_id] = dict(direct)
            return direct
        entities, children, _ = self._relationship_graph(user_id)
        row = self._state_rows(user_id).get(entity_id) if context else self._state_row(user_id, entity_id)
        direct = self._direct_state(row)
        leaves = self._playable_descendants(entity_id, entities, children)
        if not leaves or (len(leaves) == 1 and leaves[0] == entity_id):
            if entities.get(entity_id, (None, ""))[1] in {"series", "season", "collection", "artist", "release"}:
                direct["played"] = False
                direct["playedPercentage"] = None
                direct["unplayedItemCount"] = 0
            if context:
                context.resolved_states[entity_id] = dict(direct)
            return direct
        states = self._state_rows(user_id) if context else None
        leaf_states = [
            self._direct_state(states.get(leaf_id) if states is not None else self._state_row(user_id, leaf_id))
            for leaf_id in leaves
        ]
        direct["played"] = bool(leaf_states) and all(state["played"] for state in leaf_states)
        direct["unplayedItemCount"] = sum(not state["played"] for state in leaf_states)
        direct["playedPercentage"] = None
        if context:
            context.resolved_states[entity_id] = dict(direct)
        return direct

    def _leaf_state(self, user_id: str, entity_id: str) -> dict:
        context = self._context(user_id)
        if context:
            cached = context.resolved_states.get(entity_id)
            if cached is not None:
                return dict(cached)
            projected = context.projected_states.get(entity_id)
            if projected is not None:
                value = self._direct_state(
                    (projected[0], projected[1], projected[2], projected[5], projected[6], projected[7])
                )
                context.resolved_states[entity_id] = dict(value)
                return value
            value = self._direct_state(self._state_rows(user_id).get(entity_id))
            context.resolved_states[entity_id] = dict(value)
            return value
        return self._direct_state(self._state_row(user_id, entity_id))

    def _preload_projected_states(self, user_id: str, entity_ids: list[str]) -> None:
        context = self._context(user_id)
        if context is None or not entity_ids:
            return
        missing = [entity_id for entity_id in entity_ids if entity_id not in context.projected_states]
        if not missing:
            return
        placeholders = ",".join("?" for _ in missing)
        if self._read_model_ready() and self._has_table("catalog_user_summary"):
            rows = self.db.execute(
                f"SELECT e.id,COALESCE(s.favorite,0),COALESCE(s.played,0),COALESCE(s.play_count,0),"
                f"COALESCE(u.played_leaf_count,0),COALESCE(x.playable_leaf_count,0),"
                f"COALESCE(s.position_seconds,0),COALESCE(s.duration_seconds,0),s.last_played_at "
                f"FROM library_entities e JOIN catalog_entity_summary x ON x.entity_id=e.id "
                f"LEFT JOIN user_item_state s ON s.user_id=? AND s.entity_id=e.id "
                f"LEFT JOIN catalog_user_summary u ON u.user_id=? AND u.entity_id=e.id "
                f"WHERE e.id IN ({placeholders})",
                [user_id, user_id, *missing],
            )
            context.projected_states.update(
                {
                    row[0]: (
                        row[1],
                        bool(row[2]) or (int(row[4]) == int(row[5]) and int(row[5]) > 0),
                        row[3],
                        row[4],
                        max(0, int(row[5]) - int(row[4])),
                        row[6],
                        row[7],
                        row[8],
                    )
                    for row in rows
                }
            )
            missing = [entity_id for entity_id in missing if entity_id not in context.projected_states]
            if not missing:
                return
        if not self._has_table("catalog_user_rollups"):
            return
        rows = self.db.execute(
            f"SELECT entity_id,favorite,played,play_count,played_leaf_count,unplayed_leaf_count,position_seconds,duration_seconds,last_played_at FROM catalog_user_rollups WHERE user_id=? AND entity_id IN ({placeholders})",
            [user_id, *missing],
        )
        context.projected_states.update({row[0]: row[1:] for row in rows})
        if context.direct_states is None:
            direct_states = self._state_rows(user_id)
            if not direct_states:
                missing = [
                    entity_id
                    for entity_id in entity_ids
                    if entity_id not in context.projected_states
                ]
                if missing:
                    placeholders = ",".join("?" for _ in missing)
                    allowed = self.allowed_libraries(user_id)
                    if not allowed:
                        return
                    library_placeholders = ",".join("?" for _ in allowed)
                    collection_union = ""
                    collection_params: list[str] = []
                    if self._has_table("collection_members"):
                        collection_union = (
                            " UNION SELECT tree.root_id, member.source_entity_id "
                            "FROM entity_tree tree CROSS JOIN collection_members member "
                            "JOIN library_entities source ON source.id=member.source_entity_id "
                            "WHERE member.collection_entity_id=tree.entity_id "
                            f"AND source.library_id IN ({library_placeholders})"
                        )
                        collection_params = sorted(allowed)
                    rows = self.db.execute(
                        "WITH RECURSIVE entity_tree(root_id,entity_id) AS ("
                        f"SELECT id,id FROM library_entities WHERE id IN ({placeholders}) "
                        f"AND library_id IN ({library_placeholders}) "
                        "UNION SELECT tree.root_id,child.id FROM entity_tree tree "
                        "CROSS JOIN library_entities child "
                        "WHERE child.parent_id=tree.entity_id "
                        f"AND child.library_id IN ({library_placeholders})"
                        f"{collection_union}) "
                        "SELECT tree.root_id,COUNT(*) FROM entity_tree tree "
                        "JOIN library_entities entity ON entity.id=tree.entity_id "
                        "WHERE entity.entity_type IN ('movie','episode','track','release') "
                        "GROUP BY tree.root_id",
                        [
                            *missing,
                            *sorted(allowed),
                            *sorted(allowed),
                            *collection_params,
                        ],
                    )
                    context.empty_state_counts.update(
                        {row[0]: int(row[1] or 0) for row in rows}
                    )
                    context.empty_state_counts.update(
                        {entity_id: 0 for entity_id in missing if entity_id not in context.empty_state_counts}
                    )

    def _preload_projected_metadata(
        self, user_id: str, entity_ids: list[str], language: str
    ) -> None:
        context = self._context(user_id)
        if context is None or not entity_ids:
            return
        placeholders = ",".join("?" for _ in entity_ids)
        table = (
            "catalog_item_projection"
            if self._read_model_ready() and self._has_table("catalog_item_projection")
            else "catalog_metadata_projection"
            if self._has_table("catalog_metadata_projection")
            else None
        )
        if table is None:
            return
        rows = self.db.execute(
            f"SELECT entity_id,payload FROM {table} WHERE locale=? AND entity_id IN ({placeholders})",
            [language, *entity_ids],
        )
        for entity_id, payload in rows:
            try:
                value = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                context.projected_metadata[(entity_id, language)] = value

    def _seed_hydration_rows(self, user_id: str, rows: list[tuple], language: str) -> None:
        context = self._context(user_id)
        if context is None or not rows:
            return
        context.selected_rows += len(rows)
        context.entity_rows.update({row[0]: row for row in rows})
        parent_ids = {row[2] for row in rows if row[2]}
        for _ in range(2):
            missing = [entity_id for entity_id in parent_ids if entity_id not in context.entity_rows]
            if not missing:
                break
            placeholders = ",".join("?" for _ in missing)
            parent_rows = self.db.execute(
                f"SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at "
                f"FROM library_entities WHERE id IN ({placeholders})",
                missing,
            )
            context.entity_rows.update({row[0]: row for row in parent_rows})
            parent_ids.update(row[2] for row in parent_rows if row[2])
        self._preload_projected_states(user_id, list(context.entity_rows))
        self._preload_projected_metadata(user_id, list(context.entity_rows), language)

    def _hydrate_rows(
        self, user_id: str, rows: list[tuple], language: str, dates: dict[str, dict] | None = None
    ) -> list[dict]:
        self._seed_hydration_rows(user_id, rows, language)
        return [
            self._serialize(
                user_id,
                row,
                self.metadata(user_id, row[0], language)["metadata"],
                dates=(dates or {}).get(row[0]),
                language=language,
            )
            for row in rows
        ]

    def _projected_page_rows(
        self,
        library_id: str,
        parent_id: str | None,
        language: str,
        sort_by: str | None,
        sort_order: str,
        page: int,
        page_size: int,
        total: int,
    ) -> list[tuple] | None:
        if not self._has_table("catalog_metadata_projection") or total == 0:
            return None
        complete = self.db.execute(
            "SELECT COUNT(*) FROM library_entities e "
            "JOIN catalog_metadata_projection p ON p.entity_id=e.id AND p.locale=? "
            "WHERE e.library_id=? AND e.parent_id IS ?",
            (language, library_id, parent_id),
        )
        if not complete or int(complete[0][0] or 0) != total:
            return None
        sort_key = {
            "rating": "CAST(json_extract(p.payload, '$.communityRating') AS REAL)",
            "release": "json_extract(p.payload, '$.date')",
            "runtime": "CAST(json_extract(p.payload, '$.runtimeMinutes') AS REAL)",
        }.get(sort_by, "json_extract(p.payload, '$.title')")
        direction = "DESC" if sort_order.lower() == "descending" else "ASC"
        offset = max(0, page - 1) * page_size
        return self.db.execute(
            "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,"
            "e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at "
            "FROM library_entities e "
            "JOIN catalog_metadata_projection p ON p.entity_id=e.id AND p.locale=? "
            "WHERE e.library_id=? AND e.parent_id IS ? "
            f"ORDER BY COALESCE({sort_key}, '') {direction}, "
            f"COALESCE(json_extract(p.payload, '$.title'), '') {direction}, e.id {direction} "
            "LIMIT ? OFFSET ?",
            (language, library_id, parent_id, page_size, offset),
        )

    def _list_items_read_model(
        self,
        user_id: str,
        library: dict,
        language: str,
        *,
        parent_id: str | None,
        page: int,
        page_size: int,
        sort_by: str | None,
        sort_order: str,
    ) -> dict:
        library_id = library["id"]
        offset = max(0, page - 1) * page_size
        count_rows = self.db.execute(
            "SELECT COUNT(*) FROM library_entities WHERE library_id=? AND parent_id IS ?",
            (library_id, parent_id),
        )
        total = int(count_rows[0][0] or 0) if count_rows else 0
        direction = "DESC" if sort_order.lower() == "descending" else "ASC"
        params: list[object] = [language, library_id, parent_id]
        if sort_by in {"added", "lastAdded"}:
            order_column = "s.added_sort_ns" if sort_by == "added" else "s.last_added_sort_ns"
            select_dates = "s.added_sort_ns,s.last_added_sort_ns"
            date_params: list[object] = []
            if library["type"] == "collection" and self._has_table("catalog_collection_summary"):
                scope = sorted(self.allowed_libraries(user_id))
                scope_placeholders = ",".join("?" for _ in scope)
                select_dates = (
                    f"COALESCE((SELECT MIN(c.added_sort_ns) FROM catalog_collection_summary c WHERE c.collection_entity_id=e.id AND c.source_library_id IN ({scope_placeholders})),s.added_sort_ns),"
                    f"COALESCE((SELECT MAX(c.last_added_sort_ns) FROM catalog_collection_summary c WHERE c.collection_entity_id=e.id AND c.source_library_id IN ({scope_placeholders})),s.last_added_sort_ns)"
                )
                order_column = "sort_added" if sort_by == "added" else "sort_last"
                date_params = [*scope, *scope, *scope]
            order = f"{order_column} {direction},e.id {direction}"
            query = (
                "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,"
                "e.episode_number,e.episode_end_number,e.created_at,e.updated_at," + select_dates + " "
                "FROM library_entities e JOIN catalog_entity_summary s ON s.entity_id=e.id "
                "WHERE e.library_id=? AND e.parent_id IS ? ORDER BY " + order + " LIMIT ? OFFSET ?"
            )
            if date_params:
                # The computed date aliases are used only for ordering; SQLite
                # permits the equivalent expressions in the SELECT and ORDER BY.
                order = ("(SELECT MIN(c.added_sort_ns) FROM catalog_collection_summary c WHERE c.collection_entity_id=e.id AND c.source_library_id IN (" + scope_placeholders + "))" if sort_by == "added" else "(SELECT MAX(c.last_added_sort_ns) FROM catalog_collection_summary c WHERE c.collection_entity_id=e.id AND c.source_library_id IN (" + scope_placeholders + "))")
                query = query.replace("ORDER BY sort_added", "ORDER BY " + order).replace("ORDER BY sort_last", "ORDER BY " + order)
                params = [*date_params, library_id, parent_id, page_size, offset]
            else:
                params = [library_id, parent_id, page_size, offset]
        elif sort_by in {"rating", "title", "release", "runtime"} or sort_by is None:
            projection_order = {
                "rating": "p.rating_sort",
                "title": "p.title_sort",
                "release": "p.release_sort",
                "runtime": "p.runtime_sort",
            }.get(sort_by, "p.title_sort")
            if parent_id:
                parent_row = self._entity_row(parent_id)
                if parent_row and parent_row[3] in {"series", "season"} and sort_by is None:
                    projection_order = (
                        "e.season_number IS NOT NULL,e.season_number,e.episode_number IS NOT NULL,"
                        "e.episode_number,e.relative_path COLLATE NOCASE,e.id"
                    )
            query = (
                "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,"
                "e.episode_number,e.episode_end_number,e.created_at,e.updated_at "
                "FROM library_entities e JOIN catalog_item_projection p ON p.entity_id=e.id AND p.locale=? "
                "WHERE e.library_id=? AND e.parent_id IS ? ORDER BY " + projection_order + " " + direction + ",e.id " + direction + " LIMIT ? OFFSET ?"
            )
            params.extend([page_size, offset])
        else:
            query = (
                "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,"
                "e.episode_number,e.episode_end_number,e.created_at,e.updated_at "
                "FROM library_entities e WHERE e.library_id=? AND e.parent_id IS ? "
                "ORDER BY e.relative_path COLLATE NOCASE,e.id LIMIT ? OFFSET ?"
            )
            params = [library_id, parent_id, page_size, offset]
        rows = self.db.execute(query, params)
        dates: dict[str, dict] = {}
        ids = [row[0] for row in rows]
        if ids:
            date_placeholders = ",".join("?" for _ in ids)
            date_rows = self.db.execute(
                f"SELECT e.id,e.created_at,s.added_sort_ns,s.last_added_sort_ns FROM library_entities e JOIN catalog_entity_summary s ON s.entity_id=e.id WHERE e.id IN ({date_placeholders})",
                ids,
            )
            dates = {
                row[0]: {
                    "addedAt": _date_from_ns(row[2]) or row[1],
                    "lastAddedAt": _date_from_ns(row[3]) or row[1],
                }
                for row in date_rows
            }
        values = self._hydrate_rows(user_id, [row[:10] for row in rows], language, dates)
        if self._context(user_id):
            self._context(user_id).timings.setdefault("candidate_selection", 0.0)
        return {"items": values, "page": page, "pageSize": page_size, "total": total}

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
            "userState": (
                self._leaf_state(user_id, row[0])
                if row[3] in {"movie", "episode", "track"}
                else self._state(user_id, row[0])
            ),
            "childIds": children or [],
        }

    def _series_primary_image(
        self, user_id: str, series_id: str, language: str
    ) -> dict | None:
        context = self._context(user_id)
        key = (series_id, language)
        if context and key in context.series_primary_images:
            return context.series_primary_images[key]
        try:
            series = self.metadata(user_id, series_id, language)["metadata"]
        except HTTPException as error:
            if error.status_code != 404:
                raise
            value = None
            if context:
                context.series_primary_images[key] = value
            return value
        images = series.get("images")
        primary = images.get("Primary") if isinstance(images, dict) else None
        value = primary if isinstance(primary, dict) else None
        if context:
            context.series_primary_images[key] = value
        return value

    def _date_values(
        self,
        library_id: str,
        allowed_library_ids: set[str] | None = None,
        root_ids: set[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Return filesystem-derived Added and Last added values per entity."""
        scope = allowed_library_ids or {library_id}
        context = self._read_context.get()
        scope_key = frozenset(scope)
        requested_root_ids = None if root_ids is None else set(root_ids)
        cache_key = (
            scope_key,
            None if requested_root_ids is None else frozenset(requested_root_ids),
        )
        if context and cache_key in context.date_values:
            return context.date_values[cache_key]

        cached_roots: dict[str, dict[str, str]] = {}
        if context and requested_root_ids is not None:
            cached_roots = {
                root_id: context.date_root_values[(scope_key, root_id)]
                for root_id in requested_root_ids
                if (scope_key, root_id) in context.date_root_values
            }
            requested_root_ids -= set(cached_roots)
            if not requested_root_ids:
                context.date_values[cache_key] = cached_roots
                return cached_roots

        if self._read_model_ready():
            def resolve_indexed() -> dict[str, dict[str, str]]:
                placeholders = ",".join("?" for _ in scope)
                params: list[object] = []
                collection_dates = "s.added_sort_ns,s.last_added_sort_ns"
                if self._has_table("catalog_collection_summary"):
                    collection_dates = (
                        "CASE WHEN e.entity_type='collection' THEN COALESCE("
                        f"(SELECT MIN(c.added_sort_ns) FROM catalog_collection_summary c WHERE c.collection_entity_id=e.id AND c.source_library_id IN ({placeholders})),"
                        "s.added_sort_ns) ELSE s.added_sort_ns END,"
                        "CASE WHEN e.entity_type='collection' THEN COALESCE("
                        f"(SELECT MAX(c.last_added_sort_ns) FROM catalog_collection_summary c WHERE c.collection_entity_id=e.id AND c.source_library_id IN ({placeholders})),"
                        "s.last_added_sort_ns) ELSE s.last_added_sort_ns END"
                    )
                    params.extend(scope)
                    params.extend(scope)
                params.extend(scope)
                root_filter = ""
                if requested_root_ids is not None:
                    if not requested_root_ids:
                        return {}
                    root_params = sorted(requested_root_ids)
                    root_filter = f" AND e.id IN ({','.join('?' for _ in root_params)})"
                    params.extend(root_params)
                rows = self.db.execute(
                    "SELECT e.id,e.created_at,e.library_id," + collection_dates + " "
                    "FROM library_entities e JOIN catalog_entity_summary s ON s.entity_id=e.id "
                    f"WHERE e.library_id IN ({placeholders}){root_filter}",
                    params,
                )
                scan_rows = self.db.execute(
                    f"SELECT id,scan_state FROM libraries WHERE id IN ({placeholders})",
                    list(scope),
                )
                if context:
                    context.date_requested_roots += len(rows)
                    context.date_rollup_hits += len(rows)
                    context.date_scan_states.update(row[1] for row in scan_rows)
                values = {
                    row[0]: {
                        "addedAt": _date_from_ns(row[3]) or row[1] or "",
                        "lastAddedAt": _date_from_ns(row[4]) or row[1] or "",
                    }
                    for row in rows
                }
                if context:
                    context.date_values[cache_key] = values
                    if requested_root_ids is not None:
                        context.date_root_values.update(
                            {(scope_key, entity_id): value for entity_id, value in values.items()}
                        )
                return values

            values = context.measure("date_values", resolve_indexed) if context else resolve_indexed()
            return values

        def resolve() -> dict[str, dict[str, str]]:
            placeholders = ",".join("?" for _ in scope)
            if requested_root_ids is not None and not requested_root_ids:
                return {}
            requested_params = sorted(requested_root_ids or ())
            root_filter = (
                f" AND id IN ({','.join('?' for _ in requested_params)})"
                if requested_root_ids is not None
                else ""
            )
            rows = self.db.execute(
                f"SELECT id,library_id,parent_id,entity_type,created_at "
                f"FROM library_entities WHERE library_id IN ({placeholders})"
                f"{root_filter}",
                [*scope, *requested_params],
            )
            entities = {row[0]: row for row in rows}
            if not entities:
                return {}

            scan_states = {
                row[0]: row[1]
                for row in self.db.execute(
                    f"SELECT id,scan_state FROM libraries WHERE id IN ({placeholders})",
                    list(scope),
                )
            }
            projection_states: dict[str, str | None] = {}
            if self._has_table("catalog_projection_status"):
                projection_states = {
                    row[0]: row[1]
                    for row in self.db.execute(
                        f"SELECT library_id,state FROM catalog_projection_status "
                        f"WHERE library_id IN ({placeholders})",
                        list(scope),
                    )
                }
            if context:
                context.date_requested_roots += len(entities)
                context.date_scan_states.update(
                    scan_states.get(row[1], "unknown") for row in entities.values()
                )

            requested_ids = set(entities)
            rollup_values: dict[str, tuple[int | None, int | None]] = {}
            if self._has_table("catalog_entity_rollups"):
                rollup_query = (
                    f"SELECT entity_id,added_ns,last_added_ns,library_id "
                    f"FROM catalog_entity_rollups WHERE library_id IN ({placeholders})"
                )
                rollup_params: list[str] = list(scope)
                if requested_root_ids is not None:
                    rollup_query += (
                        f" AND entity_id IN ({','.join('?' for _ in requested_params)})"
                    )
                    rollup_params.extend(requested_params)
                for entity_id, added_ns, last_added_ns, rollup_library_id in self.db.execute(
                    rollup_query, rollup_params
                ):
                    projection_ready = projection_states.get(rollup_library_id)
                    if (
                        scan_states.get(rollup_library_id) == "scanning"
                        or projection_ready not in (None, "ready")
                    ):
                        continue
                    if entity_id in requested_ids:
                        rollup_values[entity_id] = (added_ns, last_added_ns)

            if requested_root_ids is None:
                if len(rollup_values) == len(requested_ids):
                    compute_ids: set[str] = set()
                else:
                    rollup_values = {}
                    compute_ids = requested_ids
            else:
                compute_ids = requested_ids - set(rollup_values)

            values = {
                entity_id: {
                    "addedAt": _date_from_ns(date_values[0])
                    or entities[entity_id][4]
                    or "",
                    "lastAddedAt": _date_from_ns(date_values[1])
                    or entities[entity_id][4]
                    or "",
                }
                for entity_id, date_values in rollup_values.items()
            }
            if context:
                context.date_rollup_hits += len(rollup_values)
                context.date_fallback_roots += len(compute_ids)

            if not compute_ids:
                return values
            if not self._has_table("media_files"):
                for entity_id in compute_ids:
                    fallback = entities[entity_id][4] or ""
                    values[entity_id] = {"addedAt": fallback, "lastAddedAt": fallback}
                return values

            direct_ids = {
                entity_id
                for entity_id in compute_ids
                if entities[entity_id][3] in {"movie", "episode", "track", "release"}
            }
            if direct_ids:
                direct_placeholders = ",".join("?" for _ in direct_ids)
                direct_rows = self.db.execute(
                    "SELECT entity_id,MIN(modified_ns),MAX(modified_ns) "
                    "FROM media_files WHERE role='media' "
                    f"AND entity_id IN ({direct_placeholders}) GROUP BY entity_id",
                    sorted(direct_ids),
                )
                for entity_id, added_ns, last_added_ns in direct_rows:
                    fallback = entities[entity_id][4] or ""
                    values[entity_id] = {
                        "addedAt": _date_from_ns(added_ns) or fallback,
                        "lastAddedAt": _date_from_ns(last_added_ns) or fallback,
                    }
                for entity_id in direct_ids:
                    values.setdefault(
                        entity_id,
                        {
                            "addedAt": entities[entity_id][4] or "",
                            "lastAddedAt": entities[entity_id][4] or "",
                        },
                    )
                compute_ids -= direct_ids
            if not compute_ids:
                return values

            collection_recursive = ""
            collection_params: list[str] = []
            if self._has_table("collection_members"):
                collection_placeholders = ",".join("?" for _ in scope)
                collection_recursive = f"""
                    UNION
                    SELECT entity_tree.root_id, member.source_entity_id
                    FROM entity_tree
                    CROSS JOIN collection_members member
                    JOIN library_entities source
                      ON source.id = member.source_entity_id
                    WHERE member.collection_entity_id = entity_tree.entity_id
                      AND source.library_id IN ({collection_placeholders})
                """
                collection_params = list(scope)
            compute_filter = ""
            compute_params: list[str] = []
            if requested_root_ids is not None:
                compute_params = sorted(compute_ids)
                compute_filter = (
                    f" AND id IN ({','.join('?' for _ in compute_params)})"
                )
            date_rows = self.db.execute(
                f"""
                WITH RECURSIVE entity_tree(root_id, entity_id) AS (
                    SELECT id, id
                    FROM library_entities
                    WHERE library_id IN ({placeholders}){compute_filter}
                    UNION
                    SELECT entity_tree.root_id, child.id
                    FROM entity_tree
                    CROSS JOIN library_entities child
                    WHERE child.parent_id = entity_tree.entity_id
                      AND child.library_id IN ({placeholders})
                    {collection_recursive}
                )
                SELECT entity_tree.root_id,
                       MIN(media_files.modified_ns),
                       MAX(media_files.modified_ns)
                FROM entity_tree
                LEFT JOIN media_files
                  ON media_files.entity_id = entity_tree.entity_id
                 AND media_files.role = 'media'
                 AND media_files.modified_ns IS NOT NULL
                GROUP BY entity_tree.root_id
                """,
                [*scope, *compute_params, *scope, *collection_params],
            )
            for entity_id, added_ns, last_added_ns in date_rows:
                fallback = entities[entity_id][4] or ""
                values[entity_id] = {
                    "addedAt": _date_from_ns(added_ns) or fallback,
                    "lastAddedAt": _date_from_ns(last_added_ns) or fallback,
                }
            for entity_id in compute_ids:
                values.setdefault(
                    entity_id,
                    {
                        "addedAt": entities[entity_id][4] or "",
                        "lastAddedAt": entities[entity_id][4] or "",
                    },
                )
            return values

        values = context.measure("date_values", resolve) if context else resolve()
        values = {**cached_roots, **values}
        if context:
            context.date_values[cache_key] = values
            if requested_root_ids is not None:
                context.date_root_values.update(
                    {
                        (scope_key, root_id): value
                        for root_id, value in values.items()
                    }
                )
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
        if self._read_model_ready():
            return self._list_items_read_model(
                user_id,
                library,
                language,
                parent_id=parent_id,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        rows = self.db.execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE library_id=? AND parent_id IS ?",
            (library_id, parent_id),
        )
        hierarchy_parent = None
        if parent_id:
            hierarchy_parent = self.require_entity(user_id, parent_id)[3]
        total = len(rows)
        display_rows = rows
        projected_page = False
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
        elif sort_by not in {"added", "lastAdded"}:
            projected_rows = self._projected_page_rows(
                library_id,
                parent_id,
                language,
                sort_by,
                sort_order,
                page,
                page_size,
                total,
            )
            if projected_rows is not None:
                display_rows = projected_rows
                projected_page = True
        context = self._context(user_id)
        date_scope = self.allowed_libraries(user_id) if library["type"] == "collection" else {library_id}
        roots_for_dates = {row[0] for row in (rows if sort_by in {"added", "lastAdded"} else display_rows)}
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
            self._preload_projected_states(user_id, [row[0] for row in display_rows])
            self._preload_projected_metadata(
                user_id, [row[0] for row in display_rows], language
            )
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
        elif display_rows is rows:
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
            "items": values if projected_page else values[start : start + page_size],
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
        if self._read_model_ready():
            locale_rank = "CASE " + " ".join(
                f"WHEN p.locale=? THEN {index}" for index, _ in enumerate(locale_order)
            ) + " ELSE 99 END"
            title_rank = (
                "CASE WHEN p.title_sort=? THEN 0 WHEN p.title_sort LIKE ? || '%' THEN 1 "
                "WHEN p.title_sort LIKE '%' || ? || '%' THEN 2 ELSE 3 END"
            )
            if len(wanted) >= 3 and self._has_table("catalog_search"):
                source = (
                    "SELECT p.entity_id,MIN(" + title_rank + ") AS match_rank,"
                    "MIN(" + locale_rank + ") AS locale_rank,0.0 AS score "
                    "FROM catalog_search JOIN catalog_item_projection p ON p.entity_id=catalog_search.entity_id AND p.locale=catalog_search.locale "
                    "JOIN library_entities e ON e.id=p.entity_id AND e.entity_type IN ('movie','series','collection') "
                    f"WHERE catalog_search MATCH ? AND p.library_id IN ({placeholders}) AND p.locale IN ({locale_placeholders}) "
                    "GROUP BY p.entity_id"
                )
                match_params = [wanted, wanted, wanted, *locale_order, f'"{wanted.replace(chr(34), chr(34) * 2)}"', *allowed, *locale_order]
            else:
                source = (
                    "SELECT p.entity_id,CASE WHEN p.title_sort=? THEN 0 ELSE 2 END AS match_rank,"
                    "MIN(" + locale_rank + ") AS locale_rank,0.0 AS score "
                    "FROM catalog_search_grams g JOIN catalog_item_projection p ON p.entity_id=g.entity_id AND p.locale=g.locale "
                    "JOIN library_entities e ON e.id=p.entity_id AND e.entity_type IN ('movie','series','collection') "
                    f"WHERE g.gram=? AND p.library_id IN ({placeholders}) AND p.locale IN ({locale_placeholders}) GROUP BY p.entity_id"
                )
                match_params = [wanted, *locale_order, wanted, *allowed, *locale_order]
            total_rows = self.db.execute(f"SELECT COUNT(*) FROM ({source}) matches", match_params)
            total = int(total_rows[0][0] or 0) if total_rows else 0
            if not total:
                return {"items": [], "page": page, "pageSize": page_size, "total": 0}
            page_rows = self.db.execute(
                "WITH matches AS (" + source + ") "
                "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at "
                "FROM matches JOIN library_entities e ON e.id=matches.entity_id "
                "WHERE e.entity_type IN ('movie','series','collection') "
                "ORDER BY matches.match_rank,matches.locale_rank,matches.score,e.id LIMIT ? OFFSET ?",
                [*match_params, page_size, max(0, page - 1) * page_size],
            )
            values = self._hydrate_rows(user_id, list(page_rows), language)
            return {"items": values, "page": page, "pageSize": page_size, "total": total}
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
        if self._has_table("catalog_user_summary"):
            from app.catalog_read_model import CatalogReadModel

            CatalogReadModel(self.db).refresh_user_entities(user_id, affected)
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
        if self._read_model_ready():
            total_rows = self.db.execute(
                f"SELECT COUNT(*) FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id WHERE s.user_id=? AND s.favorite=1 AND e.library_id IN ({placeholders})",
                [user_id, *allowed],
            )
            total = int(total_rows[0][0] or 0) if total_rows else 0
            direction = "DESC" if sort_order.lower() == "descending" else "ASC"
            order = "p.title_sort" if sort_by.lower() not in {"datecreated", "dateadded"} else "x.added_sort_ns"
            rows = self.db.execute(
                f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,x.added_sort_ns,x.last_added_sort_ns "
                f"FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id JOIN catalog_entity_summary x ON x.entity_id=e.id "
                f"JOIN catalog_item_projection p ON p.entity_id=e.id AND p.locale=? "
                f"WHERE s.user_id=? AND s.favorite=1 AND e.library_id IN ({placeholders}) "
                f"ORDER BY {order} {direction},e.id {direction} LIMIT ? OFFSET ?",
                [language, user_id, *allowed, page_size, max(0, page - 1) * page_size],
            )
            dates = {
                row[0]: {
                    "addedAt": _date_from_ns(row[10]) or row[8],
                    "lastAddedAt": _date_from_ns(row[11]) or row[8],
                }
                for row in rows
            }
            values = self._hydrate_rows(user_id, [row[:10] for row in rows], language, dates)
            return {"items": values, "page": page, "pageSize": page_size, "total": total}
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
        if self._read_model_ready() and self._has_table("catalog_item_genres") and allowed:
            genre_rows = self.db.execute(
                "SELECT genre_key FROM catalog_item_genres WHERE entity_id=? AND locale=?",
                (entity_id, normalize_metadata_locale(language)),
            )
            keys = [row[0] for row in genre_rows]
            if keys:
                allowed_placeholders = ",".join("?" for _ in allowed)
                genre_placeholders = ",".join("?" for _ in keys)
                rows = self.db.execute(
                    "SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,COUNT(DISTINCT g.genre_key) AS score,p.title_sort "
                    "FROM catalog_item_genres g JOIN catalog_item_projection p ON p.entity_id=g.entity_id AND p.locale=g.locale "
                    "JOIN library_entities e ON e.id=g.entity_id "
                    f"WHERE g.locale=? AND g.genre_key IN ({genre_placeholders}) AND e.id<>? AND e.library_id IN ({allowed_placeholders}) AND e.entity_type=? "
                    "GROUP BY e.id ORDER BY score DESC,p.title_sort,e.id LIMIT ?",
                    [normalize_metadata_locale(language), *keys, entity_id, *allowed, source_row[3], limit],
                )
                values = self._hydrate_rows(user_id, [row[:10] for row in rows], language)
                return {"items": values}
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
        context = self._context(user_id)
        if self._read_model_ready():
            select_rows = lambda: self.db.execute(
                f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,s.added_sort_ns,s.last_added_sort_ns "
                f"FROM library_entities e JOIN catalog_entity_summary s ON s.entity_id=e.id "
                f"WHERE e.library_id IN ({placeholders}) AND e.entity_type IN ('movie','series','collection') "
                "ORDER BY s.last_added_sort_ns DESC,e.id LIMIT 36",
                list(allowed),
            )
            rows = context.measure("home_discovery_sql", select_rows) if context else select_rows()
            dates = {
                row[0]: {
                    "addedAt": _date_from_ns(row[10]) or row[8],
                    "lastAddedAt": _date_from_ns(row[11]) or row[8],
                }
                for row in rows
            }
            return self._hydrate_rows(user_id, [row[:10] for row in rows], language, dates)
        select_rows = lambda: self.db.execute(
            f"SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE library_id IN ({placeholders}) AND entity_type IN ('movie','series','collection') ORDER BY created_at DESC LIMIT 36",
            list(allowed),
        )
        rows = (
            context.measure("home_discovery_sql", select_rows)
            if context
            else select_rows()
        )
        self._preload_projected_states(user_id, [row[0] for row in rows])
        self._preload_projected_metadata(user_id, [row[0] for row in rows], language)
        dates = self._date_values("", allowed, {row[0] for row in rows})
        def serialize_rows():
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
        return (
            context.measure("serialization", serialize_rows)
            if context
            else serialize_rows()
        )

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
        select_rows = lambda: self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,series.id "
            f"FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id "
            f"LEFT JOIN library_entities season ON e.entity_type='episode' AND season.id=e.parent_id "
            f"LEFT JOIN library_entities series ON series.id=season.parent_id "
            f"WHERE s.user_id=? AND e.library_id IN ({placeholders}) AND s.duration_seconds>0 AND s.position_seconds/s.duration_seconds>=0.02 AND s.position_seconds/s.duration_seconds<0.9 ORDER BY s.last_played_at DESC LIMIT 18",
            [user_id, *allowed],
        )
        context = self._context(user_id)
        rows = (
            context.measure("home_continue_watching_sql", select_rows)
            if context
            else select_rows()
        )
        ids = [row[0] for row in rows] + [row[10] for row in rows if row[10]]
        self._preload_projected_states(user_id, ids)
        self._preload_projected_metadata(user_id, ids, language)
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
        def select_rows():
            started_series = self.db.execute(
                f"SELECT DISTINCT series.id "
                f"FROM user_item_state s "
                f"JOIN library_entities e ON e.id=s.entity_id AND e.entity_type='episode' "
                f"JOIN library_entities season ON season.id=e.parent_id "
                f"JOIN library_entities series ON series.id=season.parent_id "
                f"WHERE s.user_id=? AND e.library_id IN ({placeholders}) "
                "AND (s.played=1 OR s.position_seconds>0)",
                [user_id, *allowed],
            )
            if not started_series:
                return []
            series_placeholders = ",".join("?" for _ in started_series)
            return self.db.execute(
                f"WITH ranked AS ("
                f" SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at,series.id AS series_id,COALESCE(s.played,0) AS played,COALESCE(s.last_played_at,'') AS last_played_at,"
                f" ROW_NUMBER() OVER (PARTITION BY series.id ORDER BY e.season_number,e.episode_number,e.relative_path COLLATE NOCASE) AS item_rank"
                f" FROM library_entities e "
                f"JOIN library_entities season ON season.id=e.parent_id "
                f"JOIN library_entities series ON series.id=season.parent_id "
                f"LEFT JOIN user_item_state s ON s.entity_id=e.id AND s.user_id=? "
                f"WHERE e.entity_type='episode' AND e.library_id IN ({placeholders}) "
                f"AND series.id IN ({series_placeholders}) AND COALESCE(s.played,0)=0"
                f" ) SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at,series_id,last_played_at "
                f"FROM ranked WHERE item_rank=1 ORDER BY last_played_at DESC,id LIMIT 18",
                [user_id, *allowed, *(row[0] for row in started_series)],
            )
        context = self._context(user_id)
        rows = (
            context.measure("home_next_up_sql", select_rows)
            if context
            else select_rows()
        )
        ids = [row[0] for row in rows] + [row[10] for row in rows if row[10]]
        self._preload_projected_states(user_id, ids)
        self._preload_projected_metadata(user_id, ids, language)
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
    def home_derived(
        self, user_id: str, language: str, discovery_items: list[dict] | None = None
    ) -> dict:
        allowed = self.allowed_libraries(user_id)
        empty = {"myList": [], "recentlyPlayed": [], "genreRows": []}
        if not allowed:
            return empty
        placeholders = ",".join("?" for _ in allowed)
        favorite_query = (
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at "
            f"FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id WHERE s.user_id=? AND s.favorite=1 AND e.library_id IN ({placeholders})"
        )
        favorite_params = [user_id, *allowed]
        if self._read_model_ready():
            favorite_query += " ORDER BY e.relative_path COLLATE NOCASE,e.id LIMIT 18"
        favorite_rows = self.db.execute(favorite_query, favorite_params)
        self._preload_projected_states(user_id, [row[0] for row in favorite_rows])
        self._preload_projected_metadata(user_id, [row[0] for row in favorite_rows], language)
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
        recent_ids = [row[0] for row in recent_rows] + [row[10] for row in recent_rows if row[10]]
        self._preload_projected_states(user_id, recent_ids)
        self._preload_projected_metadata(user_id, recent_ids, language)
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

    def _newly_added_rows(self, library_id: str, entity_type: str) -> list[tuple]:
        columns = (
            "e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,"
            "e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at"
        )
        rows_by_entity: dict[str, tuple] = {}
        last_modified: int | None = None
        last_entity: str | None = None
        last_file: str | None = None
        boundary: int | None = None
        while True:
            params: list[object] = [library_id, entity_type]
            cursor_filter = ""
            if last_modified is not None:
                cursor_filter = (
                    " AND (f.modified_ns < ? OR (f.modified_ns = ? AND "
                    "(f.entity_id > ? OR (f.entity_id = ? AND f.id > ?))))"
                )
                params.extend(
                    [last_modified, last_modified, last_entity, last_entity, last_file]
                )
            batch = self.db.execute(
                f"SELECT {columns},f.modified_ns,f.id "
                "FROM media_files f "
                "JOIN library_entities e ON e.id=f.entity_id "
                "WHERE f.role='media' AND e.library_id=? AND e.entity_type=?"
                f"{cursor_filter} ORDER BY f.modified_ns DESC,f.entity_id,f.id LIMIT 256",
                params,
            )
            if not batch:
                break
            for row in batch:
                entity_id = row[0]
                rows_by_entity.setdefault(entity_id, row[:10] + (row[10],))
            last_modified = batch[-1][10]
            last_entity = batch[-1][0]
            last_file = batch[-1][11]
            if len(rows_by_entity) >= 18:
                ordered = sorted(
                    rows_by_entity.values(),
                    key=lambda row: (-int(row[10] or 0), row[0]),
                )
                boundary = int(ordered[17][10] or 0)
                if last_modified < boundary:
                    break
        return [
            row[:10]
            for row in sorted(
                rows_by_entity.values(),
                key=lambda row: (-int(row[10] or 0), row[0]),
            )[:18]
        ]

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
        items = self._home_discovery_items(user_id, language, allowed)
        resume = self.home_continue_watching(user_id, language)
        next_up = self.home_next_up(user_id, language)
        series_names: dict[str, str] = {}
        library_rows = []
        context = self._context(user_id)
        by_library = {
            library["id"]: library
            for library in (
                context.measure("home_libraries", lambda: self.libraries(user_id))
                if context
                else self.libraries(user_id)
            )
        }
        for library_id, library in by_library.items():
            newly_added = []
            if self._has_table("media_files") and library["type"] in {"movies", "tv_series"}:
                entity_type = "movie" if library["type"] == "movies" else "episode"
                select_rows = lambda: self._newly_added_rows(library_id, entity_type)
                playable_rows = (
                    context.measure("home_newly_added_sql", select_rows)
                    if context
                    else select_rows()
                )
                dates = self._date_values(
                    "", {library_id}, {row[0] for row in playable_rows}
                )
                for playable_row in playable_rows:
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
                            series_name=self._home_series_name(
                                user_id, language, episode_series_id, series_names
                            ),
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
        context = self._context(user_id)
        derived = lambda: self.home_derived(user_id, language, items)
        derived_rows = (
            context.measure("home_derived", derived)
            if context
            else derived()
        )
        return {
            "latestItems": items[:25],
            "continueWatching": resume,
            "nextUp": next_up[:18],
            "libraryRows": library_rows,
            **derived_rows,
        }
