import json
from datetime import UTC, datetime
from uuid import UUID

from app.account_sync import field_revision
from app.playlists import PlaylistService
from fastapi import HTTPException


def playlist_field(operation):
    if operation.kind in {"playlist_add", "playlist_remove"}:
        return "member:" + str(operation.payload.get("trackId", ""))
    return operation.kind.removeprefix("playlist_")


def _layout(db, user_id, playlist_id):
    rows = db.read_execute(
        "SELECT revision,activity_ms,event_id,value_json FROM account_sync_fields WHERE user_id=? AND target_id=? AND field='order'",
        (user_id, playlist_id),
    )
    if not rows or not rows[0][3]:
        return
    revision, activity, event_id, raw = rows[0]
    order = {track: index for index, track in enumerate(json.loads(raw))}
    entries = db.read_execute(
        "SELECT id,entity_id,added_at FROM user_playlist_items WHERE playlist_id=? ORDER BY added_at,id",
        (playlist_id,),
    )
    entries.sort(key=lambda row: (order.get(row[1], len(order)), row[2], row[0]))
    for index, entry in enumerate(entries):
        db.execute(
            "UPDATE user_playlist_items SET position=? WHERE id=? AND playlist_id=?",
            (index, entry[0], playlist_id),
        )
    db.execute(
        "UPDATE account_sync_fields SET revision=?,activity_ms=?,event_id=?,value_json=? WHERE user_id=? AND target_id=? AND field='order'",
        (revision, activity, event_id, raw, user_id, playlist_id),
    )


def apply_playlist(sync, user_id, operation):
    service = PlaylistService(sync.catalog)
    db = sync.db
    target = operation.targetId
    field = playlist_field(operation)
    if field_revision(db, user_id, target, "deleted")["revision"]:
        return "superseded"
    at = datetime.fromtimestamp(sync._activity(operation)[0] / 1000, UTC).isoformat()
    if operation.kind == "playlist_create":
        try:
            UUID(target)
        except ValueError as error:
            raise HTTPException(400, "Playlist identity must be a UUID.") from error
        existing = db.read_execute(
            "SELECT user_id FROM user_playlists WHERE id=?", (target,)
        )
        if existing:
            if existing[0][0] != user_id:
                raise HTTPException(404, "Playlist not found.")
            return "superseded"
        name = service._name(operation.payload.get("name"))
        description = service._description(operation.payload.get("description"))
        db.execute(
            "INSERT INTO user_playlists(id,user_id,name,description,is_private,share_token,created_at,updated_at) VALUES(?,?,?,?,1,NULL,?,?)",
            (target, user_id, name, description, at, at),
        )
        for key in ("name", "description", "create"):
            sync._stamp(user_id, operation, key)
        return "applied"
    service._owned(user_id, target)
    if not sync._wins(user_id, operation, field):
        return "superseded"
    if operation.kind == "playlist_delete":
        service.delete_playlist(user_id, target)
        sync._stamp(user_id, operation, "deleted")
        return "applied"
    if operation.kind in {"playlist_name", "playlist_description"}:
        service.update_playlist(
            user_id, target, "en", {field: operation.payload.get("value")}, summary=True
        )
    elif operation.kind in {"playlist_add", "playlist_remove"}:
        track = operation.payload.get("trackId")
        if not isinstance(track, str) or not track or len(track) > 200:
            raise HTTPException(400, "A track ID is required.")
        if operation.kind == "playlist_remove":
            db.execute(
                "DELETE FROM user_playlist_items WHERE playlist_id=? AND entity_id=?",
                (target, track),
            )
        else:
            entity = sync.catalog.require_entity(user_id, track)
            if entity[3] != "track" or not db.read_execute(
                "SELECT 1 FROM media_files WHERE entity_id=? AND role='media' LIMIT 1",
                (track,),
            ):
                raise HTTPException(404, "Playable track not found.")
            entry = str(operation.id)
            if db.read_execute(
                "SELECT 1 FROM user_playlist_items WHERE id=? AND playlist_id<>?",
                (entry, target),
            ):
                raise HTTPException(409, "Entry identity collision.")
            db.execute(
                "INSERT INTO user_playlist_items(id,playlist_id,entity_id,position,added_at) SELECT ?,?,?,COALESCE(MAX(position),-1)+1,? FROM user_playlist_items WHERE playlist_id=? ON CONFLICT(playlist_id,entity_id) DO NOTHING",
                (entry, target, track, at, target),
            )
    elif operation.kind == "playlist_order":
        tracks = operation.payload.get("trackIds")
        if (
            not isinstance(tracks, list)
            or len(tracks) > 10000
            or any(not isinstance(track, str) or len(track) > 200 for track in tracks)
            or len(tracks) != len(set(tracks))
        ):
            raise HTTPException(400, "An ordered list of unique track IDs is required.")
        sync._stamp(user_id, operation, "order")
        db.execute(
            "UPDATE account_sync_fields SET value_json=? WHERE user_id=? AND target_id=? AND field='order'",
            (json.dumps(tracks), user_id, target),
        )
    else:
        raise HTTPException(400, "Unsupported playlist operation.")
    _layout(db, user_id, target)
    sync._stamp(user_id, operation, field)
    db.execute(
        "UPDATE user_playlists SET updated_at=? WHERE id=? AND user_id=?",
        (at, target, user_id),
    )
    return "applied"
