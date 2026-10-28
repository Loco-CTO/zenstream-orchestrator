import json
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from app.offline_asset_queue import OfflineAssetQueue
from app.playlists import PlaylistService
from fastapi import HTTPException
from pydantic import BaseModel, Field


class OfflineSelectionRequest(BaseModel):
    itemId: str = Field(min_length=1, max_length=200)
    kind: Literal["catalog", "playlist"] = "catalog"
    language: str = Field(default="en", min_length=2, max_length=30)


class OfflineSelectionResponse(BaseModel):
    id: str
    state: str
    itemCount: int
    items: list[dict] = Field(default_factory=list)
    metadata: list[dict] = Field(
        default_factory=list,
        description="Frozen parent metadata, including the selection root on the first page.",
    )
    nextOffset: int | None = None
    errorCode: str | None = None


class OfflineSelections:
    def __init__(self, catalog):
        self.catalog = catalog
        self.db = catalog.db

    def create(self, user_id, request):
        if request.kind == "playlist":
            PlaylistService(self.catalog)._owned(user_id, request.itemId)
            kind = "playlist"
        else:
            entity = self.catalog.require_entity(user_id, request.itemId)
            kind = entity[3]
            if kind not in {
                "movie",
                "series",
                "season",
                "episode",
                "track",
                "release",
                "artist",
                "collection",
            }:
                raise HTTPException(400, "This item cannot be downloaded.")
        pending = self.db.read_execute(
            "SELECT count(*) FROM offline_selections WHERE user_id=? AND state='enumerating'",
            (user_id,),
        )[0][0]
        if pending >= 20:
            raise HTTPException(429, "Too many selections are preparing.")
        identity = str(uuid4())
        root_snapshot = (
            self.catalog.items_by_ids(user_id, [request.itemId], request.language)
            if kind != "playlist"
            else []
        )
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO offline_selections(id,user_id,root_id,root_type,language,state,created_at) VALUES(?,?,?,?,?,'enumerating',?)",
                (
                    identity,
                    user_id,
                    request.itemId,
                    kind,
                    request.language,
                    datetime.now(UTC).isoformat(),
                ),
            )
            cursor.execute(
                "INSERT INTO offline_selection_frontier(selection_id,entity_id,kind) VALUES(?,?,?)",
                (identity, request.itemId, kind),
            )
            for item in root_snapshot:
                cursor.execute(
                    "INSERT INTO offline_selection_metadata(selection_id,entity_id,payload) VALUES(?,?,?)",
                    (identity, item["id"], json.dumps(item)),
                )
        return self.get(user_id, identity)

    def _owned(self, user_id, identity):
        rows = self.db.read_execute(
            "SELECT root_id,root_type,language,state,error_code FROM offline_selections WHERE id=? AND user_id=?",
            (identity, user_id),
        )
        if not rows:
            raise HTTPException(404, "Download selection not found.")
        root, kind, language, state, error = rows[0]
        if kind == "playlist":
            PlaylistService(self.catalog)._owned(user_id, root)
        else:
            self.catalog.require_entity(user_id, root)
        return root, kind, language, state, error

    def get(self, user_id, identity, offset=0):
        root, kind, language, state, error = self._owned(user_id, identity)
        total = self.db.read_execute(
            "SELECT count(*) FROM offline_selection_items i JOIN library_entities e ON e.id=i.entity_id JOIN user_library_access a ON a.library_id=e.library_id AND a.user_id=? WHERE i.selection_id=?",
            (user_id, identity),
        )[0][0]
        items = []
        metadata = []
        if state == "ready":
            rows = self.db.read_execute(
                "SELECT i.entity_id,i.payload FROM offline_selection_items i JOIN library_entities e ON e.id=i.entity_id JOIN user_library_access a ON a.library_id=e.library_id AND a.user_id=? WHERE i.selection_id=? ORDER BY i.ordinal LIMIT 100 OFFSET ?",
                (user_id, identity, offset),
            )
            items = [json.loads(row[1]) for row in rows]
            parent_ids = self.parents([row[0] for row in rows])
            if offset == 0 and kind != "playlist" and root not in parent_ids:
                parent_ids.append(root)
            for start in range(0, len(parent_ids), 100):
                batch = parent_ids[start : start + 100]
                marks = ",".join("?" for _ in batch)
                snapshots = self.db.read_execute(
                    f"SELECT payload FROM offline_selection_metadata WHERE selection_id=? AND entity_id IN ({marks})",
                    [identity, *batch],
                )
                accessible = {
                    item["id"]
                    for item in self.catalog.items_by_ids(user_id, batch, language)
                }
                metadata.extend(
                    item
                    for item in (json.loads(row[0]) for row in snapshots)
                    if item["id"] in accessible
                )
        return {
            "id": identity,
            "state": state,
            "itemCount": total,
            "items": items,
            "metadata": metadata,
            "nextOffset": offset + len(items)
            if state == "ready" and offset + len(items) < total
            else None,
            "errorCode": error,
        }

    def cancel(self, user_id, identity):
        self._owned(user_id, identity)
        self.db.execute(
            "UPDATE offline_selections SET state='cancelled' WHERE id=? AND user_id=?",
            (identity, user_id),
        )
        if self.catalog._has_table("offline_assets"):
            OfflineAssetQueue(self.db).release_selection(user_id, identity)
        return self.get(user_id, identity)

    def parents(self, ids):
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.db.read_execute(
            f"WITH RECURSIVE parents(id,parent_id,depth) AS (SELECT id,parent_id,0 FROM library_entities WHERE id IN ({marks}) UNION SELECT e.id,e.parent_id,p.depth+1 FROM library_entities e JOIN parents p ON e.id=p.parent_id WHERE p.depth<16) SELECT DISTINCT id FROM parents",
            ids,
        )
        return [row[0] for row in rows]

    def _children(self, user_id, node, kind, offset):
        params = [user_id, node]
        if kind == "playlist":
            source = "JOIN user_playlist_items p ON p.entity_id=e.id"
            predicate = "p.playlist_id=?"
            order = "p.position,p.id"
        else:
            source = ""
            predicate = "e.parent_id=?"
            order = "COALESCE(e.season_number,0),COALESCE(e.disc_number,0),COALESCE(e.track_number,0),COALESCE(e.episode_number,0),e.id"
            if kind == "collection":
                predicate = "(e.parent_id=? OR EXISTS (SELECT 1 FROM collection_members m WHERE m.collection_entity_id=? AND m.source_entity_id=e.id))"
                params.append(node)
            elif kind == "artist" and self.catalog._has_table("music_artist_credits"):
                predicate = "(e.parent_id=? OR EXISTS (SELECT 1 FROM music_artist_credits c WHERE c.artist_id=? AND c.track_id=e.id))"
                params.append(node)
        return self.db.read_execute(
            f"SELECT e.id,e.entity_type FROM library_entities e {source} JOIN user_library_access a ON a.library_id=e.library_id AND a.user_id=? WHERE {predicate} ORDER BY {order} LIMIT 100 OFFSET ?",
            [*params, offset],
        )

    def process_next(self):
        pending = self.db.read_execute(
            "SELECT id,user_id,language FROM offline_selections WHERE state='enumerating' ORDER BY created_at,id LIMIT 1"
        )
        if not pending:
            return False
        identity, user, language = pending[0]
        try:
            self._owned(user, identity)
            nodes = self.db.read_execute(
                "SELECT id,entity_id,kind,page_offset FROM offline_selection_frontier WHERE selection_id=? AND done=0 ORDER BY id LIMIT 100",
                (identity,),
            )
            if not nodes:
                self.db.execute(
                    "UPDATE offline_selections SET state='ready' WHERE id=? AND state='enumerating'",
                    (identity,),
                )
                return True
            if nodes[0][2] not in {"movie", "episode", "track"}:
                ordinal, node, kind, offset = nodes[0]
                children = self._children(user, node, kind, offset)
                with self.db.transaction() as cursor:
                    for child, child_kind in children:
                        cursor.execute(
                            "INSERT OR IGNORE INTO offline_selection_frontier(selection_id,entity_id,kind) VALUES(?,?,?)",
                            (identity, child, child_kind),
                        )
                    cursor.execute(
                        "UPDATE offline_selection_frontier SET page_offset=?,done=? WHERE id=?",
                        (offset + len(children), len(children) < 100, ordinal),
                    )
                return True
            leaves = []
            for node in nodes:
                if node[2] not in {"movie", "episode", "track"}:
                    break
                leaves.append(node)
            ids = [node[1] for node in leaves]
            items = self.catalog.items_by_ids(user, ids, language)
            by_id = {item["id"]: item for item in items}
            parents = self.parents(ids)
            metadata = []
            for start in range(0, len(parents), 100):
                metadata.extend(
                    self.catalog.items_by_ids(
                        user, parents[start : start + 100], language
                    )
                )
            with self.db.transaction() as cursor:
                for ordinal, node, _, _ in leaves:
                    if node in by_id and self.db.read_execute(
                        "SELECT 1 FROM media_files WHERE entity_id=? AND role='media' LIMIT 1",
                        (node,),
                    ):
                        cursor.execute(
                            "INSERT OR IGNORE INTO offline_selection_items(selection_id,entity_id,payload,ordinal) VALUES(?,?,?,?)",
                            (identity, node, json.dumps(by_id[node]), ordinal),
                        )
                    cursor.execute(
                        "UPDATE offline_selection_frontier SET done=1 WHERE id=?",
                        (ordinal,),
                    )
                for item in metadata:
                    cursor.execute(
                        "INSERT OR REPLACE INTO offline_selection_metadata(selection_id,entity_id,payload) VALUES(?,?,?)",
                        (identity, item["id"], json.dumps(item)),
                    )
        except HTTPException as error:
            if error.status_code not in {400, 403, 404}:
                raise
            self.db.execute(
                "UPDATE offline_selections SET state='failed',error_code='selection_unavailable' WHERE id=?",
                (identity,),
            )
        return True
