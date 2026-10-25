import hashlib
import json
import os
from typing import Literal

from app.offline_asset_queue import OfflineAssetQueue, now_ms
from app.offline_bundle import prepare_subtitles, selected_subtitles
from app.offline_conversion import (
    audio_index,
    conversion_capabilities,
    is_hdr,
    prepare_audio,
    prepare_video,
)
from app.playback import PlaybackManager
from fastapi import HTTPException
from pydantic import BaseModel, Field


class OfflineAssetRequest(BaseModel):
    selectionId: str = Field(min_length=1, max_length=200)
    itemId: str = Field(min_length=1, max_length=200)
    sourceId: str | None = Field(default=None, max_length=200)
    profile: Literal[
        "original", "video-720p", "video-1080p", "audio-128k", "audio-256k"
    ] = "original"
    audioStreamId: int | None = Field(default=None, ge=0)
    subtitleStreamIndices: list[int] = Field(default_factory=list, max_length=8)
    retry: bool = False


class OfflineAssetResponse(BaseModel):
    id: str
    itemId: str
    sourceId: str
    sourceRevision: str
    profile: str
    state: str
    contentUrl: str | None = None
    bundleUrl: str | None = None
    sizeBytes: int | None = None
    sha256: str | None = None
    mimeType: str | None = None
    durationSeconds: float | None = None
    trackManifest: list[dict] = Field(default_factory=list)
    replayGain: dict[str, str] = Field(default_factory=dict)
    errorCode: str | None = None


def source_signature(stat):
    return hashlib.sha256(
        json.dumps((stat.st_size, stat.st_mtime_ns, stat.st_ino)).encode()
    ).hexdigest()


class OfflineAssets:
    def __init__(self, catalog, media=None, queue=None):
        self.catalog = catalog
        self.db = catalog.db
        self.media = media or PlaybackManager(self.db, catalog)
        self.queue = queue or OfflineAssetQueue(self.db)

    def source(self, user_id, item_id, source_id=None):
        self.catalog.require_entity(user_id, item_id)
        rows = self.db.read_execute(
            "SELECT id,container,duration_seconds,bitrate,width,height,video_codec,audio_codec,probe_payload FROM media_sources WHERE entity_id=? AND (? IS NULL OR id=?) ORDER BY id LIMIT 1",
            (item_id, source_id, source_id),
        )
        if not rows:
            raise HTTPException(409, "Media source is not ready.")
        row = rows[0]
        path, size, mime = self.media.direct_path_and_metadata(user_id, item_id, row[0])
        return {
            "id": row[0],
            "container": row[1],
            "durationSeconds": row[2],
            "bitrate": row[3],
            "width": row[4],
            "height": row[5],
            "videoCodec": row[6],
            "audioCodec": row[7],
            "probe": json.loads(row[8] or "{}"),
            "path": path,
            "size": size,
            "mimeType": mime,
            "revision": source_signature(path.stat()),
        }

    def request(self, user_id, body):
        source = self.source(user_id, body.itemId, body.sourceId)
        if body.profile.startswith("audio-"):
            entity = self.catalog.require_entity(user_id, body.itemId)
            if entity[3] != "track" or not conversion_capabilities()["audio"]:
                raise HTTPException(
                    409, "The requested compact audio profile is unavailable."
                )
        if body.profile.startswith("video-"):
            capabilities = conversion_capabilities()
            if (
                not source.get("videoCodec")
                or not capabilities["video"]
                or (is_hdr(source) and not capabilities["toneMapping"])
            ):
                raise HTTPException(
                    409, "The requested portable video profile is unavailable."
                )
        normalized = {
            "entityId": body.itemId,
            "sourceId": source["id"],
            "sourceRevision": source["revision"],
            "profile": body.profile,
            "profileVersion": 1,
        }
        if body.profile != "original":
            normalized["audioStreamId"] = audio_index(source, body.audioStreamId)
        if body.subtitleStreamIndices:
            normalized["subtitles"] = selected_subtitles(
                self, user_id, body.model_copy(update={"sourceId": source["id"]})
            )
        identity = self.queue.enqueue(
            user_id, body.selectionId, normalized, retry=body.retry
        )
        return self.get(user_id, identity)

    def _owned(self, user_id, identity):
        rows = self.db.read_execute(
            "SELECT a.entity_id,a.request_json,a.state,a.result_json,a.error_code FROM offline_assets a WHERE a.id=? AND EXISTS(SELECT 1 FROM offline_asset_reservations r JOIN offline_selections s ON s.id=r.selection_id WHERE r.asset_id=a.id AND r.user_id=? AND r.expires_ms>? AND s.state='ready')",
            (identity, user_id, now_ms()),
        )
        if not rows:
            raise HTTPException(404, "Download asset not found.")
        self.catalog.require_entity(user_id, rows[0][0])
        return rows[0]

    def get(self, user_id, identity):
        entity, raw, state, result, error = self._owned(user_id, identity)
        request = json.loads(raw)
        public = json.loads(result) if result else {}
        public.pop("storageName", None)
        public.pop("supplements", None)
        public.pop("warnings", None)
        return {
            "id": identity,
            "itemId": entity,
            "sourceId": request["sourceId"],
            "sourceRevision": request["sourceRevision"],
            "profile": request["profile"],
            "state": state,
            "errorCode": error,
            "contentUrl": f"/api/offline/assets/{identity}/content"
            if state == "ready"
            else None,
            "bundleUrl": f"/api/offline/assets/{identity}/bundle"
            if state == "ready"
            else None,
            **public,
        }

    @staticmethod
    def tracks(source):
        return [
            {
                "index": stream.get("index", index),
                "type": stream.get("codec_type"),
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "sampleRate": stream.get("sample_rate"),
                "bitrate": stream.get("bit_rate"),
                "language": stream.get("tags", {}).get("language"),
                "displayTitle": stream.get("tags", {}).get("title"),
                "isDefault": bool(stream.get("disposition", {}).get("default")),
            }
            for index, stream in enumerate(source["probe"].get("streams", []))
            if stream.get("codec_type") in {"audio", "video", "subtitle"}
        ]

    def produce(self, identity, request, should_stop):
        result = self._produce_media(identity, request, should_stop)
        try:
            supplements, warnings = prepare_subtitles(
                self, identity, request, should_stop
            )
            result.update(supplements=supplements, warnings=warnings)
            if should_stop():
                raise InterruptedError("Download preparation interrupted")
            return result
        except BaseException:
            names = [
                result.get("storageName"),
                *[item.get("storageName") for item in result.get("supplements", [])],
            ]
            for name in names:
                if name:
                    target = self.queue.cache_directory / name
                    if target.resolve().parent == self.queue.cache_directory.resolve():
                        target.unlink(missing_ok=True)
            raise

    def _produce_media(self, identity, request, should_stop):
        references = self.db.read_execute(
            "SELECT r.user_id FROM offline_asset_reservations r JOIN offline_selections s ON s.id=r.selection_id WHERE r.asset_id=? AND r.expires_ms>? AND s.state='ready' ORDER BY r.user_id",
            (identity, now_ms()),
        )
        source = None
        for (user,) in references:
            try:
                source = self.source(user, request["entityId"], request["sourceId"])
                break
            except HTTPException as error:
                if error.status_code not in {403, 404}:
                    raise
        if source is None:
            raise InterruptedError("No authorized reservation remains")
        if source["revision"] != request["sourceRevision"]:
            raise HTTPException(409, "Source changed.")
        if request["profile"].startswith("video-"):
            return prepare_video(self, identity, source, request, should_stop)
        if request["profile"].startswith("audio-"):
            return prepare_audio(self, identity, source, request, should_stop)
        if request["profile"] != "original":
            raise HTTPException(409, "Download profile unavailable.")
        digest = hashlib.sha256()
        with source["path"].open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if should_stop():
                    raise InterruptedError("Hashing interrupted")
                digest.update(chunk)
            if source_signature(os.fstat(stream.fileno())) != source["revision"]:
                raise HTTPException(409, "Source changed while hashing.")
        if source_signature(source["path"].stat()) != source["revision"]:
            raise HTTPException(409, "Source changed while hashing.")
        return {
            "sizeBytes": source["size"],
            "sha256": digest.hexdigest(),
            "mimeType": source["mimeType"],
            "durationSeconds": source["durationSeconds"],
            "trackManifest": self.tracks(source),
        }

    def open_delivery(self, user_id, identity):
        _, raw, state, result, _ = self._owned(user_id, identity)
        if state != "ready" or not result:
            raise HTTPException(409, "Download asset is not ready.")
        request = json.loads(raw)
        descriptor = json.loads(result)
        if descriptor.get("storageName"):
            path = self.queue.cache_directory / descriptor["storageName"]
            if (
                path.resolve().parent != self.queue.cache_directory.resolve()
                or not path.is_file()
            ):
                raise HTTPException(404, "Prepared media file is unavailable.")
            stream = path.open("rb")
            if os.fstat(stream.fileno()).st_size != descriptor["sizeBytes"]:
                stream.close()
                raise HTTPException(409, "Prepared media file changed.")
            return stream, descriptor
        source = self.source(user_id, request["entityId"], request["sourceId"])
        if source["revision"] != request["sourceRevision"]:
            raise HTTPException(409, "Download source changed. Prepare it again.")
        stream = source["path"].open("rb")
        if source_signature(os.fstat(stream.fileno())) != request["sourceRevision"]:
            stream.close()
            raise HTTPException(409, "Download source changed. Prepare it again.")
        return stream, descriptor


def byte_range(value, size):
    if not value or not value.startswith("bytes=") or "," in value:
        raise ValueError("Invalid byte range")
    start, separator, end = value[6:].partition("-")
    if not separator or not (start or end):
        raise ValueError("Invalid byte range")
    if start:
        first = int(start)
        last = int(end) if end else size - 1
    else:
        suffix = int(end)
        if suffix <= 0:
            raise ValueError("Invalid suffix range")
        first = max(0, size - suffix)
        last = size - 1
    if first < 0 or first >= size or last < first:
        raise ValueError("Unsatisfiable range")
    return first, min(last, size - 1)
