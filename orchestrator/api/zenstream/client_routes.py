from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.catalog import Catalog, LOCAL_ARTWORK_NAMES
from app.images import LocalArtworkCache
from app.models.account import Account
from app.models.account_preference import AccountPreference
from app.models.metadata import MetadataLanguageSettings
from app.client_auth import account_from_access, issue_ticket, require_account
from app.logging_config import get_logger
from app.playback import PlaybackManager, ffmpeg_path
from app.trickplay import TrickplayExtractor
from app.intro_outro import IntroOutroStore
from api.zenstream.library_routes import require_admin


router = APIRouter()
catalog = Catalog()
media = PlaybackManager()
trickplay = TrickplayExtractor()
intro_outro = IntroOutroStore()
logger = get_logger("playback_routes")


@router.post("/api/auth/login")
async def login(request: Request):
    data = await request.json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    account_model = Account()
    account = account_model.authenticate_password(username, password)
    if not account:
        raise HTTPException(401, "Invalid credentials.")
    session = account_model.create_session(account["id"])
    return {**session, "user": account}


@router.get("/api/auth/me")
async def me(request: Request):
    account, _ = require_account(request)
    return {"user": account}


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request):
    _, token = require_account(request)
    Account().revoke(token)


@router.get("/api/auth/resource-ticket")
async def resource_ticket(request: Request):
    account, _ = require_account(request)
    return {
        "ticket": issue_ticket(account["id"], "resource", 6 * 60 * 60),
        "expiresIn": 6 * 60 * 60,
    }


@router.post("/api/auth/socket-ticket")
async def socket_ticket(request: Request):
    account, _ = require_account(request)
    return {"ticket": issue_ticket(account["id"], "socket", 60), "expiresIn": 60}


@router.get("/api/metadata/languages")
async def metadata_languages(request: Request):
    require_account(request)
    return {"languages": MetadataLanguageSettings().get()}


@router.get("/api/preferences/locale")
async def get_locale(request: Request):
    account, _ = require_account(request)
    return {"locale": AccountPreference(account["id"]).locale()}


@router.patch("/api/preferences/locale")
async def set_locale(request: Request):
    account, _ = require_account(request)
    try:
        return {
            "locale": AccountPreference(account["id"]).set_locale(
                (await request.json()).get("locale")
            )
        }
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/preferences/metadata-language")
async def get_metadata_language(request: Request):
    account, _ = require_account(request)
    return AccountPreference(account["id"]).metadata_language()


@router.patch("/api/preferences/metadata-language")
async def set_metadata_language(request: Request):
    account, _ = require_account(request)
    try:
        return AccountPreference(account["id"]).set_metadata_language(
            (await request.json()).get("language")
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/preferences/subtitles")
async def get_subtitles(request: Request):
    account, _ = require_account(request)
    return AccountPreference(account["id"]).subtitle_style()


@router.patch("/api/preferences/subtitles")
async def set_subtitles(request: Request):
    account, _ = require_account(request)
    try:
        return AccountPreference(account["id"]).set_subtitle_style(await request.json())
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/catalog/libraries")
async def libraries(request: Request):
    account, _ = require_account(request)
    return {"libraries": await asyncio.to_thread(catalog.libraries, account["id"])}


@router.get("/api/catalog/home")
async def home(
    request: Request,
    language: str | None = Query(None),
    section: str | None = Query(None),
):
    account, _ = require_account(request)
    preferred = await asyncio.to_thread(_preferred, account, language)
    if section is None:
        return await asyncio.to_thread(
            catalog.home, account["id"], preferred
        )
    if section == "featured":
        return {
            "latestItems": await asyncio.to_thread(
                catalog.home_featured, account["id"], preferred
            )
        }
    if section == "continueWatching":
        return {
            "continueWatching": await asyncio.to_thread(
                catalog.home_continue_watching, account["id"], preferred
            )
        }
    if section == "nextUp":
        return {
            "nextUp": await asyncio.to_thread(
                catalog.home_next_up, account["id"], preferred
            )
        }
    if section == "derived":
        return await asyncio.to_thread(
            catalog.home_derived, account["id"], preferred
        )
    else:
        raise HTTPException(400, "Unsupported home section.")


def _preferred(account: dict, supplied: str | None) -> str:
    return supplied or AccountPreference(account["id"]).metadata_language()["language"]


@router.get("/api/catalog/items")
async def items(
    request: Request,
    libraryId: str = Query(...),
    language: str | None = Query(None),
    parentId: str | None = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(40, ge=1, le=100),
    sortBy: str | None = Query(None),
    sortOrder: str = Query("ascending"),
):
    account, _ = require_account(request)
    preferred = await asyncio.to_thread(_preferred, account, language)
    return await asyncio.to_thread(
        catalog.list_items,
        account["id"],
        libraryId,
        preferred,
        parent_id=parentId,
        page=page,
        page_size=pageSize,
        sort_by=sortBy,
        sort_order=sortOrder,
    )


@router.get("/api/catalog/search")
async def search(
    request: Request,
    query: str = Query(..., min_length=1, max_length=200),
    language: str | None = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(40, ge=1, le=100),
):
    account, _ = require_account(request)
    preferred = await asyncio.to_thread(_preferred, account, language)
    return await asyncio.to_thread(
        catalog.search,
        account["id"],
        query,
        preferred,
        page,
        pageSize,
    )


@router.get("/api/catalog/favorites")
async def favorites(
    request: Request,
    language: str | None = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(100, ge=1, le=100),
    sortBy: str = Query("title"),
    sortOrder: str = Query("ascending"),
):
    account, _ = require_account(request)
    preferred = await asyncio.to_thread(_preferred, account, language)
    return await asyncio.to_thread(
        catalog.favorites,
        account["id"],
        preferred,
        page,
        pageSize,
        sortBy,
        sortOrder,
    )


@router.get("/api/catalog/items/{entity_id}")
async def item(entity_id: str, request: Request, language: str | None = Query(None)):
    account, _ = require_account(request)
    preferred = await asyncio.to_thread(_preferred, account, language)
    return await asyncio.to_thread(
        catalog.item, account["id"], entity_id, preferred
    )


@router.get("/api/catalog/items/{entity_id}/similar")
async def similar(entity_id: str, request: Request, language: str | None = Query(None)):
    account, _ = require_account(request)
    preferred = await asyncio.to_thread(_preferred, account, language)
    return await asyncio.to_thread(
        catalog.similar, account["id"], entity_id, preferred
    )


@router.get("/api/catalog/items/{entity_id}/metadata")
async def item_metadata(entity_id: str, request: Request, language: str = Query(...)):
    account, _ = require_account(request)
    return await asyncio.to_thread(
        catalog.metadata, account["id"], entity_id, language, include_credits=True
    )


@router.get("/api/catalog/items/{entity_id}/images/{image_type}")
async def item_image(
    entity_id: str, image_type: str, request: Request, language: str = Query(...)
):
    account = account_from_access(request)
    def resolve_cached_image() -> Path | None:
        row = catalog.require_entity(account["id"], entity_id)
        library_rows = catalog.db.execute(
            "SELECT directory FROM libraries WHERE id=?", (row[1],)
        )
        directory = (
            Path(library_rows[0][0]) if library_rows and library_rows[0][0] else None
        )
        if directory:
            for relative_path, content_hash in catalog.db.execute(
                "SELECT relative_path,quick_fingerprint FROM media_files WHERE entity_id=? AND role='image'",
                (entity_id,),
            ):
                candidate = directory / relative_path
                if candidate.stem.lower() in LOCAL_ARTWORK_NAMES.get(image_type, set()) and candidate.is_file():
                    cached = LocalArtworkCache(catalog.db).path(content_hash)
                    if cached and cached.is_file():
                        return cached
                    raise HTTPException(404, "Image not found.")
        image = catalog.selected_image(account["id"], entity_id, language, image_type)
        if not image:
            raise HTTPException(404, "Image not found.")
        rows = catalog.db.execute(
            "SELECT local_path FROM metadata_images WHERE image_type=? AND image_url=? ORDER BY fetched_at DESC",
            (image_type, image.get("url")),
        )
        if rows and rows[0][0] and Path(rows[0][0]).is_file():
            return Path(rows[0][0])
        return None

    cached_image = await asyncio.to_thread(resolve_cached_image)
    if cached_image:
        return FileResponse(
            cached_image,
            media_type="image/webp",
            headers={"Cache-Control": "private, max-age=86400"},
        )
    return Response(
        status_code=202,
        headers={"Retry-After": "2", "X-ZenStream-Image-State": "pending"},
    )


@router.get("/api/catalog/items/{entity_id}/people/{person_id}/image")
async def person_image(entity_id: str, person_id: str, request: Request):
    account = account_from_access(request)
    image = await asyncio.to_thread(
        catalog.person_image, account["id"], entity_id, person_id
    )
    if image is None:
        raise HTTPException(404, "Person image not found.")
    return FileResponse(image, media_type="image/webp")


@router.patch("/api/catalog/items/{entity_id}/state")
async def update_item_state(entity_id: str, request: Request):
    account, _ = require_account(request)
    state = await request.json()
    return await asyncio.to_thread(
        catalog.update_state, account["id"], entity_id, state
    )


@router.post("/api/playback/items/{entity_id}/negotiate")
async def negotiate_playback(entity_id: str, request: Request):
    account, _ = require_account(request)
    return await asyncio.to_thread(
        media.negotiate, account["id"], entity_id, await request.json()
    )


@router.get("/api/playback/items/{entity_id}/source")
async def playback_source_metadata(entity_id: str, request: Request):
    account, _ = require_account(request)
    return await asyncio.to_thread(media.source_metadata, account["id"], entity_id)


@router.get("/api/playback/items/{entity_id}/trickplay")
async def trickplay_manifest(entity_id: str, request: Request, sourceId: str | None = Query(None)):
    account, _ = require_account(request)
    catalog.require_entity(account["id"], entity_id)
    payload = await asyncio.to_thread(trickplay.manifest, account["id"], entity_id, sourceId)
    if payload["state"] != "ready":
        return Response(
            content=json.dumps(payload),
            status_code=202,
            headers={"Retry-After": "5"},
            media_type="application/json",
        )
    return payload


@router.get("/api/playback/items/{entity_id}/segments")
async def playback_segments(entity_id: str, request: Request, sourceId: str | None = Query(None)):
    account, _ = require_account(request)
    catalog.require_entity(account["id"], entity_id)
    return await asyncio.to_thread(intro_outro.segments, entity_id, sourceId)


@router.get("/api/playback/items/{entity_id}/trickplay/{generation}/{sheet_index}.webp")
async def trickplay_sheet(entity_id: str, generation: str, sheet_index: int, request: Request):
    account = account_from_access(request)
    catalog.require_entity(account["id"], entity_id)
    path = await asyncio.to_thread(trickplay.sheet_path, entity_id, generation, sheet_index)
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})


@router.api_route("/api/playback/items/{entity_id}/stream", methods=["GET", "HEAD"])
async def direct_stream(entity_id: str, request: Request):
    account = account_from_access(request)
    media_source_id = request.query_params.get("sourceId")
    path = await asyncio.to_thread(
        media.direct_path, account["id"], entity_id, media_source_id
    )
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
    range_header = request.headers.get("range")
    if not range_header:
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers, media_type=media_type)
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    if not range_header.startswith("bytes=") or "," in range_header:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start_text, _, end_text = range_header[6:].partition("-")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix = int(end_text)
            start = max(0, size - suffix)
            end = size - 1
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if size == 0 or start < 0 or start >= size or end < start:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    length = end - start + 1
    headers.update(
        {
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
        }
    )
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers, media_type=media_type)

    def chunks():
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(chunks(), status_code=206, headers=headers, media_type=media_type)


@router.get("/api/playback/sessions/{session_id}/{filename}")
async def playback_output(session_id: str, filename: str, request: Request):
    account = account_from_access(request)
    logger.debug(
        "playback output request session_id=%s filename=%s user_id=%s",
        session_id,
        filename,
        account["id"],
    )
    path = await asyncio.to_thread(media.session_file, account["id"], session_id, filename)
    if path.suffix.lower() == ".m3u8":
        access = request.query_params.get("access") or ""
        lines = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            lines.append(
                f"{line}{'&' if '?' in line else '?'}access={access}"
                if line and not line.startswith("#")
                else line
            )
        return Response(
            "\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl"
        )
    logger.debug(
        "playback segment response session_id=%s filename=%s user_id=%s",
        session_id,
        filename,
        account["id"],
    )
    return FileResponse(
        path,
        media_type="video/mp2t",
    )


@router.get("/api/playback/sessions/{session_id}")
async def playback_session_status(session_id: str, request: Request):
    account = account_from_access(request)
    logger.debug(
        "playback status request session_id=%s user_id=%s", session_id, account["id"]
    )
    return await asyncio.to_thread(media.session_status, account["id"], session_id)


@router.delete("/api/playback/sessions/{session_id}")
async def cancel_playback_session(session_id: str, request: Request):
    account = account_from_access(request)
    await asyncio.to_thread(media.cancel_session, account["id"], session_id)
    return {"sessionId": session_id, "sessionState": "stopping"}


def _lyrics_to_vtt(source: Path) -> str:
    text = source.read_text(encoding="utf-8-sig", errors="replace")
    timed: list[tuple[float, str]] = []
    for line in text.splitlines():
        stamps = re.findall(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]", line)
        lyric = re.sub(r"\[[^\]]+\]", "", line).strip()
        if not lyric:
            continue
        for minutes, seconds, fraction in stamps:
            value = float(minutes) * 60 + float(seconds)
            if fraction:
                value += int(fraction.ljust(3, "0")) / 1000
            timed.append((value, lyric))
    if timed:
        timed.sort(key=lambda value: value[0])
        cues = []
        for index, (start, lyric) in enumerate(timed):
            end = timed[index + 1][0] if index + 1 < len(timed) else start + 8
            cues.append(f"{index + 1}\n{_vtt_time(start)} --> {_vtt_time(max(end, start + 0.5))}\n{lyric}\n")
        return "WEBVTT\n\n" + "\n".join(cues)
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("[")]
    return "WEBVTT\n\n1\n00:00:00.000 --> 99:59:59.000\n" + "\n".join(lines) + "\n"


def _vtt_time(seconds: float) -> str:
    hours, remainder = divmod(max(0.0, seconds), 3600)
    minutes, remainder = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{remainder:06.3f}"


@router.get("/api/playback/items/{entity_id}/subtitles/{media_file_id}.vtt")
async def subtitle(entity_id: str, media_file_id: str, request: Request):
    account = account_from_access(request)
    catalog.require_entity(account["id"], entity_id)
    rows = catalog.db.execute(
        "SELECT f.relative_path,l.directory,f.role FROM media_files f JOIN library_entities e ON e.id=f.entity_id JOIN libraries l ON l.id=e.library_id WHERE f.id=? AND f.entity_id=? AND f.role IN ('subtitle','lyrics')",
        (media_file_id, entity_id),
    )
    if not rows:
        raise HTTPException(404, "Subtitle track not found.")
    source = Path(rows[0][1]) / rows[0][0]
    role = rows[0][2]
    if role == "lyrics":
        target_root = Path(catalog.db.db_file).parent / "subtitle-cache"
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / f"{hashlib.sha256(str(source).encode()).hexdigest()}.vtt"
        if not target.is_file() or target.stat().st_mtime_ns < source.stat().st_mtime_ns:
            target.write_text(_lyrics_to_vtt(source), encoding="utf-8")
        return FileResponse(target, media_type="text/vtt")
    if source.suffix.lower() == ".vtt":
        return FileResponse(source, media_type="text/vtt")
    executable = ffmpeg_path()
    if not executable:
        raise HTTPException(503, "FFmpeg is not available.")
    target_root = Path(catalog.db.db_file).parent / "subtitle-cache"
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"{hashlib.sha256(str(source).encode()).hexdigest()}.vtt"
    if not target.is_file() or target.stat().st_mtime_ns < source.stat().st_mtime_ns:
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [
                    executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    str(target),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise HTTPException(
                422, "Subtitle track could not be converted."
            ) from error
        if completed.returncode != 0:
            raise HTTPException(422, "Subtitle track could not be converted.")
    return FileResponse(target, media_type="text/vtt")


@router.get("/api/admin/users")
async def admin_users(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    return {"users": Account().list()}


@router.post("/api/admin/users", status_code=201)
async def create_user(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        return Account().create(
            str(data.get("username") or ""), str(data.get("password") or "")
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.put("/api/admin/users/{user_id}/libraries")
async def set_user_libraries(
    user_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        return {
            "libraryIds": Account().set_library_ids(
                user_id, data.get("libraryIds") or []
            )
        }
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/admin/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    try:
        return Account().set_password(
            user_id, str((await request.json()).get("password") or "")
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.patch("/api/admin/users/{user_id}")
async def update_user(
    user_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    try:
        return Account().set_disabled(
            user_id, bool((await request.json()).get("disabled"))
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.delete("/api/admin/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    if not Account().delete(user_id):
        raise HTTPException(404, "User not found.")
