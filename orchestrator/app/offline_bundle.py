import hashlib
import json
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from app.account_sync import field_revision
from app.ffmpeg_supervisor import FFmpegCancelled, FFmpegError, run_ffmpeg
from app.offline_selections import OfflineSelections
from app.playback import ffmpeg_path
from fastapi import HTTPException
from pydantic import BaseModel, Field

TEXT_SUBTITLES = frozenset(
    {"subrip", "srt", "webvtt", "ass", "ssa", "mov_text", "text"}
)


def revision(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class OfflineBundleResponse(BaseModel):
    itemId: str
    language: str
    items: list[dict]
    artwork: list[dict]
    subtitles: list[dict]
    lyrics: dict | None = None
    chapters: list[dict] = Field(default_factory=list)
    segments: list[dict] = Field(default_factory=list)
    episodeNeighbors: dict[str, str | None] | None = None
    stateRevisions: dict = Field(default_factory=dict)
    presentationRevision: str
    supplementRevision: str
    warnings: list[str] = Field(default_factory=list)


def episode_neighbors(catalog, user, identity):
    entity = catalog.require_entity(user, identity)
    if entity[3] != "episode":
        return None
    season = "COALESCE(CASE WHEN p.entity_type='season' THEN p.season_number END,e.season_number,0)"
    series = "CASE WHEN p.entity_type='season' THEN p.parent_id ELSE e.parent_id END"
    current = catalog.db.read_execute(
        f"SELECT e.episode_number,COALESCE(e.episode_end_number,e.episode_number),{season},{series} "
        "FROM library_entities e LEFT JOIN library_entities p ON p.id=e.parent_id WHERE e.id=?",
        (identity,),
    )
    if not current or current[0][0] is None or not current[0][3]:
        return {"previous": None, "next": None}
    start, end, number, parent = current[0]
    result = {}
    for direction, operator, boundary in (("previous", "<", start), ("next", ">", end)):
        episode = (
            "COALESCE(e.episode_end_number,e.episode_number)"
            if direction == "previous"
            else "e.episode_number"
        )
        order = "DESC" if direction == "previous" else "ASC"
        rows = catalog.db.read_execute(
            f"SELECT e.id FROM library_entities e LEFT JOIN library_entities p ON p.id=e.parent_id "
            "JOIN user_library_access a ON a.library_id=e.library_id AND a.user_id=? "
            f"WHERE e.entity_type='episode' AND {series}=? AND "
            f"({season}{operator}? OR ({season}=? AND {episode}{operator}?)) AND "
            "EXISTS(SELECT 1 FROM media_files m WHERE m.entity_id=e.id AND m.role='media') "
            f"ORDER BY {season} {order},e.episode_number {order},e.id {order} LIMIT 1",
            (user, parent, number, number, boundary),
        )
        result[direction] = rows[0][0] if rows else None
    return result


def selected_subtitles(assets, user, body, *, include_all=False):
    if not include_all and not body.subtitleStreamIndices:
        return []
    sources = assets.media.sources(user, body.itemId)
    source = next((item for item in sources if item["id"] == body.sourceId), None)
    if not source:
        raise HTTPException(404, "Selected source is unavailable.")
    streams = {
        stream["index"]: stream
        for stream in source.get("streams", [])
        if stream.get("codec_type") == "subtitle"
    }
    result = []
    indices = sorted(streams) if include_all else sorted(set(body.subtitleStreamIndices))
    for index in indices:
        stream = streams.get(index)
        if not stream:
            raise HTTPException(400, "Selected subtitle is unavailable.")
        if (
            body.profile != "original"
            and stream.get("codec_name") not in TEXT_SUBTITLES
        ):
            raise HTTPException(409, "These subtitles require an original download.")
        result.append(
            {
                "index": index,
                "fileId": stream.get("fileId"),
                "codec": stream.get("codec_name"),
                "language": stream.get("tags", {}).get("language"),
                "name": stream.get("tags", {}).get("title"),
            }
        )
    return result


def prepare_subtitles(assets, identity, request, should_stop):
    result = []
    warnings = []
    for selection in request.get("subtitles", []):
        if selection.get("fileId"):
            continue
        if selection.get("codec") not in TEXT_SUBTITLES:
            result.append({**selection, "mode": "embedded"})
            continue
        if should_stop():
            raise InterruptedError("Subtitle preparation interrupted")
        executable = ffmpeg_path()
        if not executable:
            warnings.append("subtitle_conversion_unavailable")
            continue
        references = assets.db.read_execute(
            "SELECT user_id FROM offline_asset_reservations WHERE asset_id=? ORDER BY user_id LIMIT 1",
            (identity,),
        )
        if not references:
            raise InterruptedError("Subtitle reservation was cancelled")
        source = assets.source(
            references[0][0], request["entityId"], request["sourceId"]
        )
        cache = assets.queue.cache_directory
        cache.mkdir(parents=True, exist_ok=True)
        name = f"{identity}.subtitle-{selection['index']}.vtt"
        temporary = cache / (name + ".part")
        target = cache / name
        try:
            run_ffmpeg(
                [
                    executable,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(source["path"]),
                    "-map",
                    f"0:{selection['index']}",
                    "-c:s",
                    "webvtt",
                    "-f",
                    "webvtt",
                    "-fs",
                    "16777216",
                    str(temporary),
                ],
                should_terminate=should_stop,
                timeout_seconds=600,
            )
            if temporary.stat().st_size >= 16777216:
                warnings.append("subtitle_too_large")
                continue
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            temporary.replace(target)
            result.append(
                {**selection, "mode": "text", "storageName": name, "sha256": digest}
            )
        except FFmpegCancelled as error:
            raise InterruptedError("Subtitle preparation interrupted") from error
        except (OSError, FFmpegError, HTTPException):
            warnings.append("subtitle_preparation_failed")
        finally:
            temporary.unlink(missing_ok=True)
    return result, warnings


def artwork_descriptors(items, language):
    result = []
    for item in items:
        for kind, image in (item.get("metadata", {}).get("images") or {}).items():
            if kind not in {"Primary", "Backdrop"} or not isinstance(image, dict):
                continue
            url = urlsplit(str(image.get("url") or ""))
            path = (
                url.path
                if not url.netloc and url.path.startswith("/api/catalog/items/")
                else f"/api/catalog/items/{quote(item['id'], safe='')}/images/{kind}"
            )
            query = {
                key: value
                for key, value in parse_qsl(url.query)
                if key in {"language", "v"}
            }
            query.update(language=language, w="320")
            address = path + "?" + urlencode(query)
            result.append(
                {
                    "itemId": item["id"],
                    "type": kind,
                    "url": address,
                    "revision": revision({"url": address, "image": image}),
                    "blurHash": image.get("blurHash"),
                }
            )
    return result


def bundle(assets, user_id, identity, language):
    entity, raw, state, result, _ = assets._owned(user_id, identity)
    if state != "ready" or not result:
        raise HTTPException(409, "Download asset is not ready.")
    request = json.loads(raw)
    prepared = json.loads(result)
    ids = OfflineSelections(assets.catalog).parents([entity])
    items = assets.catalog.items_by_ids(user_id, ids, language)
    artwork = artwork_descriptors(items, language)
    subtitles = []
    warnings = list(prepared.get("warnings", []))
    for item in prepared.get("supplements", []):
        public = {key: value for key, value in item.items() if key != "storageName"}
        if item.get("storageName"):
            public["url"] = (
                f"/api/offline/assets/{identity}/supplements/{item['index']}.vtt"
            )
        subtitles.append(public)
    for selection in request.get("subtitles", []):
        if not selection.get("fileId"):
            continue
        try:
            _, path = assets.media._file_path(
                entity, selection["fileId"], role="subtitle"
            )
            subtitles.append(
                {
                    **selection,
                    "mode": "text",
                    "url": f"/api/playback/items/{quote(entity, safe='')}/subtitles/{quote(selection['fileId'], safe='')}.vtt?v={path.stat().st_mtime_ns}",
                }
            )
        except (HTTPException, OSError):
            warnings.append("selected_subtitle_unavailable")
    lyrics = None
    if assets.catalog.require_entity(user_id, entity)[3] == "track":
        try:
            lyrics = assets.media.lyrics(user_id, entity).get("lyrics")
        except (HTTPException, OSError):
            warnings.append("lyrics_unavailable")
    probe_rows = assets.db.read_execute(
        "SELECT probe_payload,media_file_id FROM media_sources WHERE id=? AND entity_id=?",
        (request["sourceId"], entity),
    )
    chapters = []
    segments = []
    if probe_rows:
        probe = json.loads(probe_rows[0][0] or "{}")
        chapters = [
            {
                "startSeconds": float(chapter.get("start_time") or 0),
                "endSeconds": float(chapter.get("end_time") or 0),
                "name": chapter.get("tags", {}).get("title"),
            }
            for chapter in probe.get("chapters", [])[:1000]
        ]
        if assets.catalog._has_table("intro_outro_segments"):
            segments = [
                {"type": kind, "startSeconds": start, "endSeconds": end}
                for kind, start, end in assets.db.read_execute(
                    "SELECT segment_type,start_seconds,end_seconds FROM intro_outro_segments WHERE media_file_id=? ORDER BY segment_type",
                    (probe_rows[0][1],),
                )
            ]
    states = {
        item["id"]: {
            field: field_revision(assets.db, user_id, item["id"], field)
            for field in ("playback", "favorite", "following")
        }
        for item in items
    }
    presentation = [
        {key: value for key, value in item.items() if key != "userState"}
        for item in items
    ]
    neighbors = episode_neighbors(assets.catalog, user_id, entity)
    return {
        "itemId": entity,
        "language": language,
        "items": items,
        "artwork": artwork,
        "subtitles": subtitles,
        "lyrics": lyrics,
        "chapters": chapters,
        "segments": segments,
        "episodeNeighbors": neighbors,
        "stateRevisions": states,
        "presentationRevision": revision(presentation),
        "supplementRevision": revision(
            [subtitles, lyrics, chapters, segments, neighbors]
        ),
        "warnings": warnings,
    }


def supplement_path(assets, user, identity, index):
    _, _, state, raw, _ = assets._owned(user, identity)
    if state != "ready" or not raw:
        raise HTTPException(404, "Download supplement is unavailable.")
    entry = next(
        (
            item
            for item in json.loads(raw).get("supplements", [])
            if item["index"] == index and item.get("storageName")
        ),
        None,
    )
    if not entry:
        raise HTTPException(404, "Download supplement is unavailable.")
    target = assets.queue.cache_directory / entry["storageName"]
    if (
        target.resolve().parent != assets.queue.cache_directory.resolve()
        or not target.is_file()
    ):
        raise HTTPException(404, "Download supplement is unavailable.")
    return target
