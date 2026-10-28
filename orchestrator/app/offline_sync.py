import math
import time
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from app.account_sync import epochs, field_revision
from app.notifications import FollowService
from app.offline_playlists import apply_playlist, playlist_field
from app.playlists import PlaylistService
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class SyncOperation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    sequence: int = Field(ge=1, le=9223372036854775807)
    kind: Literal[
        "checkpoint",
        "complete",
        "watched",
        "unwatched",
        "music_play",
        "favorite",
        "unfavorite",
        "follow",
        "unfollow",
        "playlist_create",
        "playlist_name",
        "playlist_description",
        "playlist_delete",
        "playlist_add",
        "playlist_remove",
        "playlist_order",
    ]
    targetId: str = Field(min_length=1, max_length=200)
    activityMs: int = Field(ge=0)
    clockTrusted: bool = True
    baseRevision: int = Field(default=0, ge=0)
    historyEpoch: int = Field(default=0, ge=0)
    privacyEpoch: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class AccountSyncRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    serverInstanceId: str = Field(min_length=1, max_length=200)
    deviceId: str = Field(min_length=1, max_length=200)
    operations: list[SyncOperation] = Field(max_length=100)


class SyncAcknowledgement(BaseModel):
    id: str
    sequence: int
    outcome: Literal[
        "applied", "superseded", "duplicate", "privacy_blocked", "rejected"
    ]
    targetId: str
    state: dict[str, Any] | None = None
    revision: dict[str, Any] | None = None


class AccountSyncResponse(BaseModel):
    serverTime: str
    historyEpoch: int
    privacyEpoch: int
    acknowledgements: list[SyncAcknowledgement]


class AccountSyncService:
    def __init__(self, catalog):
        self.catalog = catalog
        self.db = catalog.db

    def synchronize(self, user_id, batch):
        identity = self.db.read_execute(
            "SELECT instance_id FROM offline_server WHERE id=1"
        )
        if not identity or batch.serverInstanceId != identity[0][0]:
            raise HTTPException(409, "Server identity changed.")
        acknowledgements = []
        for operation in batch.operations:
            with self.db.transaction() as cursor:
                rows = self.db.read_execute(
                    "SELECT sequence FROM account_sync_devices WHERE user_id=? AND device_id=?",
                    (user_id, batch.deviceId),
                )
                previous = rows[0][0] if rows else 0
                if operation.sequence <= previous:
                    outcome = "duplicate"
                elif operation.sequence != previous + 1:
                    raise HTTPException(
                        409, {"code": "sequence_gap", "expectedSequence": previous + 1}
                    )
                else:
                    cursor.execute("SAVEPOINT offline_operation")
                    try:
                        outcome = self._apply(user_id, operation)
                    except HTTPException as error:
                        if error.status_code not in {400, 403, 404, 409, 422}:
                            raise
                        cursor.execute("ROLLBACK TO offline_operation")
                        outcome = "rejected"
                    finally:
                        cursor.execute("RELEASE offline_operation")
                    cursor.execute(
                        "INSERT INTO account_sync_devices(user_id,device_id,sequence) VALUES(?,?,?) ON CONFLICT(user_id,device_id) DO UPDATE SET sequence=excluded.sequence",
                        (user_id, batch.deviceId, operation.sequence),
                    )
                state = self._canonical(user_id, operation)
                acknowledgements.append(
                    {
                        "id": str(operation.id),
                        "sequence": operation.sequence,
                        "targetId": operation.targetId,
                        "outcome": outcome,
                        "state": state,
                        "revision": field_revision(
                            self.db, user_id, operation.targetId, self._field(operation)
                        )
                        if state is not None
                        else None,
                    }
                )
        return {
            "serverTime": datetime.now(UTC).isoformat(),
            **epochs(self.db, user_id),
            "acknowledgements": acknowledgements,
        }

    def _canonical(self, user_id, operation):
        target_id = operation.targetId
        try:
            if operation.kind.startswith("playlist_"):
                state = PlaylistService(self.catalog).get_summary(
                    user_id, target_id, "en"
                )
                if operation.kind == "playlist_add":
                    rows = self.db.read_execute(
                        "SELECT id FROM user_playlist_items WHERE playlist_id=? AND entity_id=?",
                        (target_id, operation.payload.get("trackId")),
                    )
                    state["entryId"] = rows[0][0] if rows else None
                return state
            self.catalog.require_entity(user_id, target_id)
            return self.catalog._state(user_id, target_id)
        except HTTPException as error:
            if error.status_code not in {403, 404}:
                raise
            return None

    @staticmethod
    def _field(operation):
        if operation.kind.startswith("playlist_"):
            return playlist_field(operation)
        if operation.kind in {"favorite", "unfavorite"}:
            return "favorite"
        if operation.kind in {"follow", "unfollow"}:
            return "following"
        return "playback"

    @staticmethod
    def _activity(operation):
        now = int(time.time() * 1000)
        trusted = (
            operation.clockTrusted
            and 946684800000 <= operation.activityMs <= now + 300000
        )
        return (operation.activityMs if trusted else now), trusted

    def _wins(self, user_id, operation, field):
        current = field_revision(self.db, user_id, operation.targetId, field)
        activity, trusted = self._activity(operation)
        if not trusted and current["revision"] != operation.baseRevision:
            return False
        return (activity, str(operation.id)) > (
            current["activityMs"],
            current["eventId"],
        )

    def _stamp(self, user_id, operation, field):
        activity, _ = self._activity(operation)
        self.db.execute(
            "INSERT INTO account_sync_fields(user_id,target_id,field,revision,activity_ms,event_id) VALUES(?,?,?,1,?,?) ON CONFLICT(user_id,target_id,field) DO UPDATE SET revision=revision+1,activity_ms=excluded.activity_ms,event_id=excluded.event_id",
            (user_id, operation.targetId, field, activity, str(operation.id)),
        )

    def _apply_flag(self, user_id, operation, entity, field):
        if field == "following" and entity[3] not in {"movie", "series", "artist"}:
            raise HTTPException(
                400, "Follow operations require the movie, series, or artist identity."
            )
        if not self._wins(user_id, operation, field):
            return "superseded"
        if field == "favorite":
            self.catalog.update_state(
                user_id, operation.targetId, {"favorite": operation.kind == "favorite"}
            )
        else:
            FollowService(self.db).set_for_entity(
                user_id, operation.targetId, operation.kind == "follow"
            )
        self._stamp(user_id, operation, field)
        self.catalog._invalidate_home_cache(user_id)
        return "applied"

    def _apply(self, user_id, operation):
        if operation.kind.startswith("playlist_"):
            return apply_playlist(self, user_id, operation)
        entity = self.catalog.require_entity(user_id, operation.targetId)
        field = self._field(operation)
        if field != "playback":
            return self._apply_flag(user_id, operation, entity, field)
        if entity[3] not in {"movie", "episode", "track"}:
            raise HTTPException(400, "Offline playback state requires a playable item.")
        generations = epochs(self.db, user_id)
        automatic = operation.kind in {"checkpoint", "complete", "music_play"}
        if operation.historyEpoch != generations["historyEpoch"]:
            return "privacy_blocked"
        if automatic and (
            operation.privacyEpoch != generations["privacyEpoch"]
            or not self.catalog._watch_history_enabled(user_id)
        ):
            return "privacy_blocked"
        winning = self._wins(user_id, operation, "playback")
        activity, _ = self._activity(operation)
        at = datetime.fromtimestamp(activity / 1000, UTC).isoformat()
        if operation.kind == "music_play":
            if entity[3] != "track":
                raise HTTPException(400, "Music history requires a track.")
            if winning:
                self.catalog.record_play_start(
                    user_id,
                    operation.targetId,
                    {"playbackInstanceId": str(operation.id)},
                )
            else:
                self.db.execute(
                    "INSERT OR IGNORE INTO user_play_events(user_id,entity_id,playback_instance_id,started_at) VALUES(?,?,?,?)",
                    (user_id, operation.targetId, str(operation.id), at),
                )
                inserted = self.db.read_execute("SELECT changes()")[0][0]
                if inserted:
                    self.db.execute(
                        "UPDATE user_item_state SET play_count=play_count+1 WHERE user_id=? AND entity_id=?",
                        (user_id, operation.targetId),
                    )
            self.db.execute(
                "UPDATE user_play_events SET started_at=? WHERE user_id=? AND playback_instance_id=?",
                (at, user_id, str(operation.id)),
            )
        elif winning:
            if operation.kind in {"watched", "unwatched", "complete"}:
                changes = {"played": operation.kind != "unwatched"}
            else:
                try:
                    position = float(operation.payload["positionSeconds"])
                    duration = float(operation.payload["durationSeconds"])
                except (KeyError, TypeError, ValueError) as error:
                    raise HTTPException(400, "Invalid playback checkpoint.") from error
                if (
                    not math.isfinite(position)
                    or not math.isfinite(duration)
                    or position < 0
                    or duration < 0
                ):
                    raise HTTPException(400, "Invalid playback checkpoint.")
                changes = {
                    "positionSeconds": min(position, duration)
                    if duration
                    else position,
                    "durationSeconds": duration,
                }
            self.catalog.update_state(user_id, operation.targetId, changes)
        if winning:
            self.db.execute(
                "UPDATE user_item_state SET last_played_at=? WHERE user_id=? AND entity_id=?",
                (at, user_id, operation.targetId),
            )
            self._stamp(user_id, operation, "playback")
        self.catalog._invalidate_home_cache(user_id)
        return "applied" if winning or operation.kind == "music_play" else "superseded"
