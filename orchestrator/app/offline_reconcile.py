from typing import Literal

from app.account_sync import epochs, field_revision
from app.offline import OfflineService
from app.offline_assets import OfflineAssets
from app.offline_bundle import artwork_descriptors, revision
from app.playlists import PlaylistService
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator


class OfflineKnownObject(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    kind: Literal["catalog", "playlist"] = "catalog"
    presentationRevision: str | None = Field(default=None, max_length=200)
    libraryId: str | None = Field(default=None, max_length=200)
    sourceId: str | None = Field(default=None, max_length=200)
    sourceRevision: str | None = Field(default=None, max_length=200)
    offset: int = Field(default=0, ge=0)


class OfflineReconcileRequest(BaseModel):
    serverInstanceId: str = Field(min_length=1, max_length=200)
    language: str = Field(default="en", min_length=2, max_length=30)
    objects: list[OfflineKnownObject] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def bounded_playlist_page(self):
        if any(item.kind == "playlist" for item in self.objects) and (
            len(self.objects) != 1 or self.objects[0].offset % 100
        ):
            raise ValueError("Request one playlist page at a multiple-of-100 offset.")
        if len({(item.kind, item.id) for item in self.objects}) != len(self.objects):
            raise ValueError("Objects must have unique identities.")
        return self


class OfflineReconcileResponse(BaseModel):
    serverInstanceId: str
    serverTime: str
    historyEpoch: int
    privacyEpoch: int
    watchHistoryEnabled: bool
    libraryAccess: dict[str, bool]
    objects: list[dict]


class OfflineReconciler:
    """Read at most 100 known catalog objects or one 100-entry playlist page."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.db = catalog.db

    def reconcile(self, user, body):
        identity = self.db.read_execute(
            "SELECT instance_id FROM offline_server WHERE id=1"
        )
        if not identity or identity[0][0] != body.serverInstanceId:
            raise HTTPException(409, "Server identity changed.")
        allowed = self.catalog.allowed_libraries(user)
        hydrated = self.catalog.items_by_ids(
            user,
            [item.id for item in body.objects if item.kind == "catalog"],
            body.language,
        )
        by_id = {item["id"]: item for item in hydrated}
        results = []
        for known in body.objects:
            if known.kind == "playlist":
                results.append(self._playlist(user, known, body.language))
                continue
            item = by_id.get(known.id)
            if item is None:
                results.append({"id": known.id, "status": "unavailable"})
                continue
            presentation = {
                key: value for key, value in item.items() if key != "userState"
            }
            version = revision(presentation)
            result = {
                "id": known.id,
                "status": "unchanged"
                if known.presentationRevision == version
                else "updated",
                "presentationRevision": version,
                "state": item.get("userState", {}),
                "stateRevisions": {
                    field: field_revision(self.db, user, known.id, field)
                    for field in ("playback", "favorite", "following")
                },
            }
            if result["status"] == "updated":
                result.update(
                    item=item, artwork=artwork_descriptors([item], body.language)
                )
            if known.sourceId:
                try:
                    source = OfflineAssets(self.catalog).source(
                        user, known.id, known.sourceId
                    )
                    result.update(
                        sourceRevision=source["revision"],
                        sourceStatus="unchanged"
                        if source["revision"] == known.sourceRevision
                        else "changed",
                    )
                except (HTTPException, OSError):
                    result["sourceStatus"] = "unavailable"
            results.append(result)
        return {
            "serverInstanceId": identity[0][0],
            "serverTime": OfflineService.server_time(),
            **epochs(self.db, user),
            "watchHistoryEnabled": self.catalog._watch_history_enabled(user),
            "libraryAccess": {
                item.libraryId: item.libraryId in allowed
                for item in body.objects
                if item.libraryId
            },
            "objects": results,
        }

    def _playlist(self, user, known, language):
        try:
            payload = PlaylistService(self.catalog).get_playlist(
                user, known.id, language, page=known.offset // 100 + 1, page_size=100
            )
        except HTTPException as error:
            if error.status_code != 404:
                raise
            deleted = field_revision(self.db, user, known.id, "deleted")["revision"] > 0
            return {"id": known.id, "status": "deleted" if deleted else "unavailable"}
        payload.pop("shareToken", None)
        # One revision identifies all pages. A changed revision during enumeration
        # requires the client to restart its metadata snapshot, never merge pages.
        fields = {
            field: field_revision(self.db, user, known.id, field)
            for field in ("name", "description", "membership", "order", "deleted")
        }
        membership = self.db.read_execute(
            "SELECT COUNT(*),COALESCE(SUM(revision),0) FROM account_sync_fields WHERE user_id=? AND target_id=? AND field LIKE 'member:%'",
            (user, known.id),
        )
        version = revision(
            [payload["updatedAt"], payload["itemCount"], fields, membership]
        )
        return {
            "id": known.id,
            "status": "updated",
            "kind": "playlist",
            "presentationRevision": version,
            "playlist": payload,
            "stateRevisions": fields,
            "offset": known.offset,
            "nextOffset": known.offset + 100 if payload.get("hasMore") else None,
        }
