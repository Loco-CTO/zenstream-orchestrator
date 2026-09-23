from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class PlaylistService:
    """Account-owned playlists and the Follow-backed Watchlist read model."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.db = catalog.db

    @staticmethod
    def _name(value) -> str:
        if not isinstance(value, str):
            raise HTTPException(400, "Playlist name is required.")
        normalized = value.strip()
        if not normalized or len(normalized) > 100:
            raise HTTPException(400, "Playlist name must be 1 to 100 characters.")
        return normalized

    @staticmethod
    def _description(value) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 500:
            raise HTTPException(400, "Playlist description must be 500 characters or less.")
        normalized = value.strip()
        return normalized or None

    def _owned(self, user_id: str, playlist_id: str):
        rows = self.db.execute(
            "SELECT id,user_id,name,description,is_private,share_token,created_at,updated_at "
            "FROM user_playlists WHERE id=? AND user_id=?",
            (playlist_id, user_id),
        )
        if not rows:
            raise HTTPException(404, "Playlist not found.")
        return rows[0]

    def _entries(self, playlist_id: str, user_id: str, *, page=None, page_size=None):
        allowed = sorted(self.catalog.allowed_libraries(user_id))
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        limit = "" if page is None else " LIMIT ? OFFSET ?"
        params = [playlist_id, *allowed]
        if page is not None:
            params.extend([page_size, (page - 1) * page_size])
        return self.db.execute(
            "SELECT i.id,i.entity_id,i.position,i.added_at FROM user_playlist_items i "
            "JOIN library_entities e ON e.id=i.entity_id "
            "WHERE i.playlist_id=? AND e.entity_type='track' "
            f"AND e.library_id IN ({placeholders}) "
            "AND EXISTS (SELECT 1 FROM media_files m WHERE m.entity_id=e.id AND m.role='media') "
            f"ORDER BY i.position,i.id{limit}",
            params,
        )

    def _resolved_entries(self, user_id: str, entries, language: str):
        catalog_items = self.catalog.items_by_ids(
            user_id, [row[1] for row in entries], language
        )
        by_id = {item["id"]: item for item in catalog_items}
        return [
            {
                "entryId": row[0],
                "position": row[2],
                "addedAt": row[3],
                "item": by_id[row[1]],
            }
            for row in entries
            if row[1] in by_id
        ]

    def _visible_counts_and_artwork(self, user_id: str, playlist_ids: list[str], language: str):
        """Count grant-visible entries in SQL and hydrate at most four per card."""
        allowed = sorted(self.catalog.allowed_libraries(user_id))
        counts: dict[str, int] = {}
        artwork: dict[str, list[dict]] = {}
        if not allowed or not playlist_ids:
            return counts, artwork
        library_params = ",".join("?" for _ in allowed)
        for start in range(0, len(playlist_ids), 400):
            batch = playlist_ids[start : start + 400]
            playlist_params = ",".join("?" for _ in batch)
            visible = (
                " FROM user_playlist_items i JOIN library_entities e ON e.id=i.entity_id "
                f"WHERE i.playlist_id IN ({playlist_params}) AND e.library_id IN ({library_params}) "
                "AND e.entity_type='track' "
                "AND EXISTS (SELECT 1 FROM media_files m WHERE m.entity_id=e.id AND m.role='media')"
            )
            params = [*batch, *allowed]
            for playlist_id, count in self.db.execute(
                "SELECT i.playlist_id,COUNT(*)" + visible + " GROUP BY i.playlist_id", params
            ):
                counts[playlist_id] = count
            rows = self.db.execute(
                "SELECT playlist_id,entity_id FROM ("
                "SELECT i.playlist_id,i.entity_id,"
                "ROW_NUMBER() OVER (PARTITION BY i.playlist_id ORDER BY i.position,i.id) AS rn"
                + visible + ") WHERE rn<=4 ORDER BY playlist_id,rn",
                params,
            )
            hydrated = self.catalog.items_by_ids(user_id, [row[1] for row in rows], language)
            by_id = {item["id"]: item for item in hydrated}
            for playlist_id, entity_id in rows:
                if entity_id in by_id:
                    artwork.setdefault(playlist_id, []).append(by_id[entity_id])
        return counts, artwork

    def _playlist_payload(
        self,
        row,
        user_id: str,
        language: str,
        *,
        include_items: bool,
        owner: bool,
        page: int | None = None,
        page_size: int | None = None,
        count: int | None = None,
        artwork: list[dict] | None = None,
    ) -> dict:
        if count is None or artwork is None:
            counts, artwork_by_id = self._visible_counts_and_artwork(user_id, [row[0]], language)
            count = counts.get(row[0], 0)
            artwork = artwork_by_id.get(row[0], [])
        payload = {
            "id": row[0],
            "name": row[2],
            "description": row[3],
            "isPrivate": bool(row[4]),
            "itemCount": count,
            "artworkItems": artwork,
            "createdAt": row[6],
            "updatedAt": row[7],
            "isOwner": owner,
        }
        if owner:
            payload["shareToken"] = row[5]
        if include_items:
            rows = self._entries(row[0], user_id, page=page, page_size=page_size)
            payload["items"] = self._resolved_entries(user_id, rows, language)
            if page is not None:
                payload.update(page=page, pageSize=page_size, hasMore=page * page_size < count)
        return payload

    def list_playlists(self, user_id: str, language: str, membership_source_id: str | None = None) -> dict:
        rows = self.db.execute(
            "SELECT id,user_id,name,description,is_private,share_token,created_at,updated_at "
            "FROM user_playlists WHERE user_id=? ORDER BY updated_at DESC,name COLLATE NOCASE,id",
            (user_id,),
        )
        counts, artwork = self._visible_counts_and_artwork(
            user_id, [row[0] for row in rows], language
        )
        membership: dict[str, int] = {}
        source_ids = None
        if membership_source_id is not None:
            source_ids = self._expand(user_id, membership_source_id, language)
            for start in range(0, len(source_ids), 400):
                batch = source_ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                for playlist_id, matched in self.db.execute(
                    "SELECT i.playlist_id,COUNT(*) FROM user_playlist_items i "
                    "JOIN user_playlists p ON p.id=i.playlist_id "
                    f"WHERE p.user_id=? AND i.entity_id IN ({placeholders}) GROUP BY i.playlist_id",
                    [user_id, *batch],
                ):
                    membership[playlist_id] = membership.get(playlist_id, 0) + matched
        return {
            "items": [
                {
                    **self._playlist_payload(
                        row, user_id, language, include_items=False, owner=True,
                        count=counts.get(row[0], 0), artwork=artwork.get(row[0], []),
                    ),
                    **({"isMember": bool(source_ids) and membership.get(row[0], 0) == len(source_ids)}
                       if source_ids is not None else {}),
                }
                for row in rows
            ]
        }

    def get_playlist(self, user_id: str, playlist_id: str, language: str, *, page=None, page_size=None) -> dict:
        row = self._owned(user_id, playlist_id)
        return self._playlist_payload(
            row, user_id, language, include_items=True, owner=True,
            page=page, page_size=page_size,
        )

    def get_shared_playlist(self, user_id: str, share_token: str, language: str, *, page=None, page_size=None) -> dict:
        rows = self.db.execute(
            "SELECT id,user_id,name,description,is_private,share_token,created_at,updated_at "
            "FROM user_playlists WHERE share_token=? AND is_private=0",
            (share_token,),
        )
        if not rows:
            raise HTTPException(404, "Playlist not found.")
        return self._playlist_payload(
            rows[0], user_id, language, include_items=True, owner=False,
            page=page, page_size=page_size,
        )

    def get_summary(self, user_id: str, playlist_id: str, language: str) -> dict:
        return self._playlist_payload(
            self._owned(user_id, playlist_id), user_id, language,
            include_items=False, owner=True,
        )

    def _expand(self, user_id: str, entity_id: str, language: str) -> list[str]:
        expanded: list[str] = []
        visited: set[str] = set()

        def visit(candidate_id: str) -> None:
            if candidate_id in visited:
                return
            visited.add(candidate_id)
            row = self.catalog.require_entity(user_id, candidate_id)
            entity_type = row[3]
            if entity_type == "track":
                if self.db.execute(
                    "SELECT 1 FROM media_files WHERE entity_id=? AND role='media' LIMIT 1",
                    (candidate_id,),
                ):
                    expanded.append(candidate_id)
                return

            if entity_type == "artist":
                details = self.catalog.music_artist_tracks(
                    user_id, candidate_id, language
                )
                for track in details.get("tracks", []):
                    track_id = track.get("id")
                    if track_id:
                        visit(str(track_id))
                return

            if entity_type == "release":
                allowed = sorted(self.catalog.allowed_libraries(user_id))
                if not allowed:
                    return
                placeholders = ",".join("?" for _ in allowed)
                children = self.db.execute(
                    "SELECT id FROM library_entities WHERE parent_id=? "
                    f"AND entity_type='track' AND library_id IN ({placeholders}) "
                    "ORDER BY disc_number IS NULL,disc_number,"
                    "track_number IS NULL,track_number,relative_path COLLATE NOCASE,id",
                    [candidate_id, *allowed],
                )
                for child in children:
                    visit(str(child[0]))
                return

            raise HTTPException(400, "Playlists only support music tracks, albums, and artists.")

        visit(entity_id)
        return list(dict.fromkeys(expanded))

    def _validate_entity_ids(self, entity_ids) -> list[str]:
        if not isinstance(entity_ids, list) or not entity_ids or len(entity_ids) > 100:
            raise HTTPException(400, "Provide 1 to 100 catalog item IDs.")
        if any(not isinstance(value, str) or not value for value in entity_ids):
            raise HTTPException(400, "Catalog item IDs must be non-empty strings.")
        return list(dict.fromkeys(entity_ids))

    def create_playlist(
        self,
        user_id: str,
        language: str,
        *,
        name,
        description=None,
        is_private=True,
        entity_id: str | None = None,
        summary: bool = False,
    ) -> dict:
        playlist_name = self._name(name)
        playlist_description = self._description(description)
        if not isinstance(is_private, bool):
            raise HTTPException(400, "Playlist privacy must be a boolean.")
        item_ids = self._expand(user_id, entity_id, language) if entity_id else []
        if entity_id and not item_ids:
            raise HTTPException(400, "The selected item has no playable items.")
        playlist_id = _new_id()
        timestamp = _now()
        token = None if is_private else secrets.token_urlsafe(32)
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO user_playlists "
                "(id,user_id,name,description,is_private,share_token,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    playlist_id,
                    user_id,
                    playlist_name,
                    playlist_description,
                    int(is_private),
                    token,
                    timestamp,
                    timestamp,
                ),
            )
            for position, item_id in enumerate(item_ids):
                cursor.execute(
                    "INSERT INTO user_playlist_items "
                    "(id,playlist_id,entity_id,position,added_at) VALUES(?,?,?,?,?)",
                    (_new_id(), playlist_id, item_id, position, timestamp),
                )
        return (self.get_summary if summary else self.get_playlist)(user_id, playlist_id, language)

    def update_playlist(
        self, user_id: str, playlist_id: str, language: str, payload: dict, *, summary: bool = False
    ) -> dict:
        row = self._owned(user_id, playlist_id)
        name = self._name(payload["name"]) if "name" in payload else row[2]
        description = (
            self._description(payload["description"])
            if "description" in payload
            else row[3]
        )
        is_private = row[4]
        if "isPrivate" in payload:
            if not isinstance(payload["isPrivate"], bool):
                raise HTTPException(400, "Playlist privacy must be a boolean.")
            is_private = int(payload["isPrivate"])
        token = row[5]
        if is_private:
            token = None
        elif row[4] or not token:
            token = secrets.token_urlsafe(32)
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE user_playlists SET name=?,description=?,is_private=?,share_token=?,updated_at=? "
                "WHERE id=? AND user_id=?",
                (name, description, is_private, token, _now(), playlist_id, user_id),
            )
        return (self.get_summary if summary else self.get_playlist)(user_id, playlist_id, language)

    def delete_playlist(self, user_id: str, playlist_id: str) -> None:
        self._owned(user_id, playlist_id)
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM user_playlists WHERE id=? AND user_id=?",
                (playlist_id, user_id),
            )

    def add_entities(
        self,
        user_id: str,
        playlist_id: str,
        language: str,
        entity_ids,
        *,
        summary: bool = False,
    ) -> dict:
        self._owned(user_id, playlist_id)
        sources = self._validate_entity_ids(entity_ids)
        expanded: list[str] = []
        for source_id in sources:
            expanded.extend(self._expand(user_id, source_id, language))
        if not expanded:
            raise HTTPException(400, "The selected item has no playable items.")
        expanded = list(dict.fromkeys(expanded))
        existing = {
            row[0]
            for row in self.db.execute(
                "SELECT entity_id FROM user_playlist_items WHERE playlist_id=?",
                (playlist_id,),
            )
        }
        pending = [item_id for item_id in expanded if item_id not in existing]
        if pending:
            timestamp = _now()
            with self.db.transaction() as cursor:
                current = cursor.execute(
                    "SELECT COALESCE(MAX(position),-1)+1 FROM user_playlist_items WHERE playlist_id=?",
                    (playlist_id,),
                ).fetchone()[0]
                for offset, item_id in enumerate(pending):
                    cursor.execute(
                        "INSERT OR IGNORE INTO user_playlist_items "
                        "(id,playlist_id,entity_id,position,added_at) VALUES(?,?,?,?,?)",
                        (
                            _new_id(),
                            playlist_id,
                            item_id,
                            int(current) + offset,
                            timestamp,
                        ),
                    )
                cursor.execute(
                    "UPDATE user_playlists SET updated_at=? WHERE id=? AND user_id=?",
                    (timestamp, playlist_id, user_id),
                )
        return (self.get_summary if summary else self.get_playlist)(user_id, playlist_id, language)

    def remove_entry(
        self, user_id: str, playlist_id: str, entry_id: str, language: str, *, summary: bool = False
    ) -> dict:
        self._owned(user_id, playlist_id)
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM user_playlist_items WHERE id=? AND playlist_id=?",
                (entry_id, playlist_id),
            )
            cursor.execute(
                "UPDATE user_playlists SET updated_at=? WHERE id=? AND user_id=?",
                (_now(), playlist_id, user_id),
            )
        return (self.get_summary if summary else self.get_playlist)(user_id, playlist_id, language)

    def remove_source(self, user_id: str, playlist_id: str, source_id: str, language: str) -> dict:
        self._owned(user_id, playlist_id)
        entity_ids = self._expand(user_id, source_id, language)
        if not entity_ids:
            raise HTTPException(400, "The selected item has no playable items.")
        with self.db.transaction() as cursor:
            for start in range(0, len(entity_ids), 400):
                batch = entity_ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                cursor.execute(
                    f"DELETE FROM user_playlist_items WHERE playlist_id=? AND entity_id IN ({placeholders})",
                    [playlist_id, *batch],
                )
            cursor.execute(
                "UPDATE user_playlists SET updated_at=? WHERE id=? AND user_id=?",
                (_now(), playlist_id, user_id),
            )
        return self.get_summary(user_id, playlist_id, language)

    def move_entry(self, user_id: str, playlist_id: str, entry_id: str,
                   language: str, *, before_entry_id=None, after_entry_id=None) -> dict:
        self._owned(user_id, playlist_id)
        if (before_entry_id is None) == (after_entry_id is None):
            raise HTTPException(400, "Provide exactly one beforeEntryId or afterEntryId.")
        anchor = before_entry_id if before_entry_id is not None else after_entry_id
        current = [row[0] for row in self.db.execute(
            "SELECT id FROM user_playlist_items WHERE playlist_id=? ORDER BY position,id",
            (playlist_id,),
        )]
        if entry_id not in current or anchor not in current:
            raise HTTPException(404, "Playlist entry not found.")
        if entry_id == anchor:
            return self.get_summary(user_id, playlist_id, language)
        current.remove(entry_id)
        offset = 0 if before_entry_id is not None else 1
        current.insert(current.index(anchor) + offset, entry_id)
        with self.db.transaction() as cursor:
            for position, item_id in enumerate(current):
                cursor.execute(
                    "UPDATE user_playlist_items SET position=? WHERE id=? AND playlist_id=?",
                    (position, item_id, playlist_id),
                )
            cursor.execute(
                "UPDATE user_playlists SET updated_at=? WHERE id=? AND user_id=?",
                (_now(), playlist_id, user_id),
            )
        return self.get_summary(user_id, playlist_id, language)

    def reorder_entries(
        self, user_id: str, playlist_id: str, language: str, entry_ids
    ) -> dict:
        self._owned(user_id, playlist_id)
        if not isinstance(entry_ids, list) or any(
            not isinstance(value, str) for value in entry_ids
        ):
            raise HTTPException(400, "Playlist order must be a list of entry IDs.")
        current = [row[0] for row in self.db.execute(
            "SELECT id FROM user_playlist_items WHERE playlist_id=? ORDER BY position,id",
            (playlist_id,),
        )]
        if len(entry_ids) != len(current) or set(entry_ids) != set(current):
            raise HTTPException(400, "Playlist order must include every entry exactly once.")
        with self.db.transaction() as cursor:
            for position, entry_id in enumerate(entry_ids):
                cursor.execute(
                    "UPDATE user_playlist_items SET position=? WHERE id=? AND playlist_id=?",
                    (position, entry_id, playlist_id),
                )
            cursor.execute(
                "UPDATE user_playlists SET updated_at=? WHERE id=? AND user_id=?",
                (_now(), playlist_id, user_id),
            )
        return self.get_playlist(user_id, playlist_id, language)

    def watchlist(self, user_id: str, language: str) -> dict:
        allowed = sorted(self.catalog.allowed_libraries(user_id))
        if not allowed:
            return {"items": []}
        placeholders = ",".join("?" for _ in allowed)
        rows = self.db.execute(
            "SELECT e.id,MAX(f.created_at) FROM user_follow_targets f "
            "JOIN library_entities e ON e.id=f.entity_id "
            f"WHERE f.user_id=? AND e.library_id IN ({placeholders}) "
            "AND e.entity_type IN ('movie','series','artist') "
            "GROUP BY e.id ORDER BY MAX(f.created_at) DESC,e.id",
            [user_id, *allowed],
        )
        ids = [row[0] for row in rows]
        items = self.catalog.items_by_ids(user_id, ids, language)
        status = self._watchlist_status(user_id, items)
        return {
            "items": [
                {**item, "watchlistStatus": status.get(item["id"])}
                for item in items
            ]
        }

    def _watchlist_status(self, user_id: str, items: list[dict]) -> dict[str, dict]:
        statuses: dict[str, dict] = {}
        series_ids = [item["id"] for item in items if item.get("type") == "series"]
        for item in items:
            if item.get("type") != "movie":
                continue
            state = item.get("userState") or {}
            position = float(state.get("positionSeconds") or 0)
            duration = float(state.get("durationSeconds") or 0)
            if position > 0 and (not duration or position < duration * 0.95):
                statuses[item["id"]] = {"kind": "continue"}
        if not series_ids:
            return statuses
        placeholders = ",".join("?" for _ in series_ids)
        episodes = self.db.execute(
            "SELECT episode.id,COALESCE(season.parent_id,episode.parent_id) AS series_id,"
            "COALESCE(episode.season_number,season.season_number,0),episode.episode_number,"
            "COALESCE(state.position_seconds,0),COALESCE(state.duration_seconds,0),"
            "COALESCE(state.played,0),COALESCE(state.last_played_at,'') "
            "FROM library_entities episode "
            "LEFT JOIN library_entities season ON season.id=episode.parent_id AND season.entity_type='season' "
            "LEFT JOIN user_item_state state ON state.entity_id=episode.id AND state.user_id=? "
            f"WHERE episode.entity_type='episode' AND COALESCE(season.parent_id,episode.parent_id) IN ({placeholders}) "
            "ORDER BY COALESCE(episode.season_number,season.season_number,0),episode.episode_number,episode.id",
            [user_id, *series_ids],
        )
        by_series: dict[str, list[tuple]] = {}
        for row in episodes:
            by_series.setdefault(str(row[1]), []).append(row)
        for series_id, values in by_series.items():
            in_progress = [
                value
                for value in values
                if value[4] > 0 and value[5] > 0 and value[4] < value[5] * 0.95
            ]
            if in_progress:
                selected = max(in_progress, key=lambda value: str(value[7]))
                kind = "continue"
            else:
                next_items = [value for value in values if not bool(value[6])]
                if not next_items:
                    continue
                selected = next_items[0]
                kind = "upNext"
            statuses[series_id] = {
                "kind": kind,
                "seasonNumber": selected[2],
                "episodeNumber": selected[3],
            }
        return statuses
