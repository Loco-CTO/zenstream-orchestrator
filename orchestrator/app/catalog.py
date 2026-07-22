"""Permission-aware catalog and deterministic metadata fallback."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app.config import Config
from app.models.metadata import MetadataLanguageSettings, normalize_metadata_locale
from app.metadata_domain import choose_artwork
from app.metadata_services import MetadataReadService
from app.providers import IMAGE_TYPES, PRIMARY_PROVIDER_BY_ENTITY


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self):
        self.db = Config().database

    def allowed_libraries(self, user_id: str) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT library_id FROM user_library_access WHERE user_id=?",
                (user_id,),
            )
        }

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
        }

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
            }
            for row in rows
        ]

    def _entity_row(self, entity_id: str):
        rows = self.db.execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE id=?",
            (entity_id,),
        )
        return rows[0] if rows else None

    def require_entity(self, user_id: str, entity_id: str):
        row = self._entity_row(entity_id)
        if not row or row[1] not in self.allowed_libraries(user_id):
            raise HTTPException(404, "Item not found.")
        return row

    def _provider_ids(self, entity_id: str, entity_type: str) -> list[dict]:
        primary = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
        rows = self.db.execute(
            "SELECT provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id=? ORDER BY CASE WHEN provider=? THEN 0 ELSE 1 END,provider",
            (entity_id, primary),
        )
        return [{"provider": row[0], "type": row[1], "id": row[2]} for row in rows]

    def _read_service(self) -> MetadataReadService:
        return MetadataReadService(self.db)

    def metadata(self, user_id: str, entity_id: str, language: str) -> dict:
        row = self.require_entity(user_id, entity_id)
        configured = MetadataLanguageSettings().get()
        language = normalize_metadata_locale(language)
        if language not in configured:
            raise HTTPException(400, "Metadata language is not configured.")
        return self._read_service().resolve_public(
            entity_id, row[3], self._provider_ids(entity_id, row[3]), language
        )

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

    def item(self, user_id: str, entity_id: str, language: str) -> dict:
        row = self.require_entity(user_id, entity_id)
        metadata = self.metadata(user_id, entity_id, language)["metadata"]
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
        return self._serialize(user_id, row, metadata, [child[0] for child in children])

    def _state(self, user_id: str, entity_id: str) -> dict:
        rows = self.db.execute(
            "SELECT favorite,played,play_count,position_seconds,duration_seconds,last_played_at FROM user_item_state WHERE user_id=? AND entity_id=?",
            (user_id, entity_id),
        )
        if not rows:
            return {
                "favorite": False,
                "played": False,
                "playCount": 0,
                "positionSeconds": 0,
                "durationSeconds": 0,
                "playedPercentage": 0,
            }
        row = rows[0]
        percentage = (row[3] / row[4] * 100) if row[4] else 0
        return {
            "favorite": bool(row[0]),
            "played": bool(row[1]),
            "playCount": row[2],
            "positionSeconds": row[3],
            "durationSeconds": row[4],
            "playedPercentage": percentage,
            "lastPlayedAt": row[5],
        }

    def _serialize(
        self, user_id: str, row, metadata: dict, children: list[str] | None = None
    ) -> dict:
        season_id = row[2] if row[3] == "episode" else None
        series_id = row[2] if row[3] == "season" else None
        if season_id:
            parent = self._entity_row(season_id)
            series_id = parent[2] if parent else None
        return {
            "id": row[0],
            "libraryId": row[1],
            "parentId": row[2],
            "type": row[3],
            "seriesId": series_id,
            "seasonId": season_id,
            "name": metadata.get("title") or Path(row[4] or "").stem or row[3].title(),
            "seasonNumber": row[5],
            "episodeNumber": row[6],
            "episodeEndNumber": row[7],
            "dateAdded": row[8],
            "updatedAt": row[9],
            "metadata": metadata,
            "userState": self._state(user_id, row[0]),
            "childIds": children or [],
        }

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
        self.require_library(user_id, library_id)
        if parent_id:
            parent = self.require_entity(user_id, parent_id)
            if parent[1] != library_id:
                raise HTTPException(404, "Item not found.")
        rows = self.db.execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE library_id=? AND parent_id IS ?",
            (library_id, parent_id),
        )
        values = []
        for row in rows:
            metadata = self.metadata(user_id, row[0], language)["metadata"]
            values.append(self._serialize(user_id, row, metadata))
        hierarchy_parent = None
        if parent_id:
            hierarchy_parent = self.require_entity(user_id, parent_id)[3]
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
            selected_sort = sort_by or "title"
            key = {
                "dateAdded": lambda value: value.get("dateAdded") or "",
                "releaseDate": lambda value: value["metadata"].get("date") or "",
                "rating": lambda value: value["metadata"].get("communityRating") or 0,
                "runtime": lambda value: value["metadata"].get("runtimeMinutes") or 0,
            }.get(selected_sort, lambda value: str(value.get("name") or "").casefold())
            values.sort(key=lambda value: (key(value), value["id"]), reverse=reverse)
        total = len(values)
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
            ranked.append((*best, row[0], self._serialize(user_id, row, metadata)))
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
        current = self._state(user_id, entity_id)
        favorite = bool(changes.get("favorite", current["favorite"]))
        position = max(
            0.0, float(changes.get("positionSeconds", current["positionSeconds"]))
        )
        duration = max(
            0.0, float(changes.get("durationSeconds", current["durationSeconds"]))
        )
        explicit_played = changes.get("played")
        played = (
            bool(explicit_played)
            if explicit_played is not None
            else bool(duration and position / duration >= 0.9)
        )
        play_count = int(current.get("playCount") or 0)
        if played and not current.get("played"):
            play_count += 1
        self.db.execute(
            "INSERT INTO user_item_state(user_id,entity_id,favorite,played,play_count,position_seconds,duration_seconds,last_played_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,entity_id) DO UPDATE SET favorite=excluded.favorite,played=excluded.played,play_count=excluded.play_count,position_seconds=excluded.position_seconds,duration_seconds=excluded.duration_seconds,last_played_at=excluded.last_played_at,updated_at=excluded.updated_at",
            (
                user_id,
                entity_id,
                int(favorite),
                int(played),
                play_count,
                0 if played else position,
                duration,
                _now() if position or played else current.get("lastPlayedAt"),
                _now(),
            ),
        )
        return self._state(user_id, entity_id)

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
                user_id, row, self.metadata(user_id, row[0], language)["metadata"]
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
                        self._serialize(user_id, row, metadata),
                    )
                )
        ranked.sort(key=lambda value: value[:3])
        return {"items": [value[3] for value in ranked[:limit]]}

    def home(self, user_id: str, language: str) -> dict:
        allowed = self.allowed_libraries(user_id)
        if not allowed:
            return {
                "latestItems": [],
                "continueWatching": [],
                "nextUp": [],
                "libraryRows": [],
            }
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            f"SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,created_at,updated_at FROM library_entities WHERE library_id IN ({placeholders}) AND entity_type IN ('movie','series','collection') ORDER BY created_at DESC LIMIT 100",
            list(allowed),
        )
        items = [
            self._serialize(
                user_id, row, self.metadata(user_id, row[0], language)["metadata"]
            )
            for row in rows
        ]
        resume_rows = self.db.execute(
            f"SELECT e.id,e.library_id,e.parent_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,e.episode_end_number,e.created_at,e.updated_at FROM user_item_state s JOIN library_entities e ON e.id=s.entity_id WHERE s.user_id=? AND e.library_id IN ({placeholders}) AND s.duration_seconds>0 AND s.position_seconds/s.duration_seconds>=0.02 AND s.position_seconds/s.duration_seconds<0.9 ORDER BY s.last_played_at DESC LIMIT 18",
            [user_id, *allowed],
        )
        resume = [
            self._serialize(
                user_id, row, self.metadata(user_id, row[0], language)["metadata"]
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
        for episodes in by_series.values():
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
                    )
                )
        next_up.sort(
            key=lambda value: value["userState"].get("lastPlayedAt") or "", reverse=True
        )
        library_rows = []
        by_library = {library["id"]: library for library in self.libraries(user_id)}
        for library_id, library in by_library.items():
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
        }
