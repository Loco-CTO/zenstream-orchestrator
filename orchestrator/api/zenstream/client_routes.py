"""Public ZenStream account, catalog, preference, and state APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.catalog import Catalog
from app.models.account import Account
from app.models.account_preference import AccountPreference
from app.models.metadata import MetadataLanguageSettings
from app.client_auth import account_from_access, issue_ticket, require_account
from app.playback import PlaybackManager, ffmpeg_path
from api.zenstream.library_routes import require_admin


router = APIRouter()
catalog = Catalog()
media = PlaybackManager()


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
    return {"libraries": catalog.libraries(account["id"])}


@router.get("/api/catalog/home")
async def home(request: Request, language: str | None = Query(None)):
    account, _ = require_account(request)
    return catalog.home(account["id"], _preferred(account, language))


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
    sortBy: str = Query("title"),
    sortOrder: str = Query("ascending"),
):
    account, _ = require_account(request)
    return catalog.list_items(
        account["id"],
        libraryId,
        _preferred(account, language),
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
    return catalog.search(
        account["id"], query, _preferred(account, language), page, pageSize
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
    return catalog.favorites(
        account["id"], _preferred(account, language), page, pageSize, sortBy, sortOrder
    )


@router.get("/api/catalog/items/{entity_id}")
async def item(entity_id: str, request: Request, language: str | None = Query(None)):
    account, _ = require_account(request)
    return catalog.item(account["id"], entity_id, _preferred(account, language))


@router.get("/api/catalog/items/{entity_id}/similar")
async def similar(entity_id: str, request: Request, language: str | None = Query(None)):
    account, _ = require_account(request)
    return catalog.similar(account["id"], entity_id, _preferred(account, language))


@router.get("/api/catalog/items/{entity_id}/metadata")
async def item_metadata(entity_id: str, request: Request, language: str = Query(...)):
    account, _ = require_account(request)
    return catalog.metadata(account["id"], entity_id, language)


@router.get("/api/catalog/items/{entity_id}/images/{image_type}")
async def item_image(
    entity_id: str, image_type: str, request: Request, language: str = Query(...)
):
    account = account_from_access(request)
    row = catalog.require_entity(account["id"], entity_id)
    library_rows = catalog.db.execute(
        "SELECT directory FROM libraries WHERE id=?", (row[1],)
    )
    directory = (
        Path(library_rows[0][0]) if library_rows and library_rows[0][0] else None
    )
    local_names = {
        "Primary": {
            "poster",
            "folder",
            "cover",
            "primary",
            "tvshow",
            "movie",
            "season",
        },
        "Backdrop": {"backdrop", "fanart", "background"},
        "Logo": {"logo", "clearlogo", "clear-logo"},
        "Banner": {"banner"},
    }
    if directory:
        for (relative_path,) in catalog.db.execute(
            "SELECT relative_path FROM media_files WHERE entity_id=? AND role='image'",
            (entity_id,),
        ):
            candidate = directory / relative_path
            if (
                candidate.stem.lower() in local_names.get(image_type, set())
                and candidate.is_file()
            ):
                return FileResponse(candidate)
    image = catalog.selected_image(account["id"], entity_id, language, image_type)
    if not image:
        raise HTTPException(404, "Image not found.")
    rows = catalog.db.execute(
        "SELECT local_path FROM metadata_images WHERE image_type=? AND image_url=? ORDER BY fetched_at DESC",
        (image_type, image.get("url")),
    )
    if rows and rows[0][0] and Path(rows[0][0]).is_file():
        return FileResponse(rows[0][0])
    return Response(
        status_code=202,
        headers={"Retry-After": "2", "X-ZenStream-Image-State": "pending"},
    )


@router.patch("/api/catalog/items/{entity_id}/state")
async def update_item_state(entity_id: str, request: Request):
    account, _ = require_account(request)
    return catalog.update_state(account["id"], entity_id, await request.json())


@router.post("/api/playback/items/{entity_id}/negotiate")
async def negotiate_playback(entity_id: str, request: Request):
    account, _ = require_account(request)
    return await asyncio.to_thread(
        media.negotiate, account["id"], entity_id, await request.json()
    )


@router.api_route("/api/playback/items/{entity_id}/stream", methods=["GET", "HEAD"])
async def direct_stream(entity_id: str, request: Request):
    account = account_from_access(request)
    return FileResponse(
        await asyncio.to_thread(media.direct_path, account["id"], entity_id)
    )


@router.get("/api/playback/sessions/{session_id}/{filename}")
async def playback_output(session_id: str, filename: str, request: Request):
    account = account_from_access(request)
    path = await asyncio.to_thread(
        media.session_file, account["id"], session_id, filename
    )
    if path.suffix.lower() == ".m3u8":
        access = request.query_params.get("access") or ""
        lines = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            lines.append(
                f"{line}?access={access}" if line and not line.startswith("#") else line
            )
        return Response(
            "\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl"
        )
    return FileResponse(path, media_type="video/mp2t")


@router.get("/api/playback/items/{entity_id}/subtitles/{media_file_id}.vtt")
async def subtitle(entity_id: str, media_file_id: str, request: Request):
    account = account_from_access(request)
    catalog.require_entity(account["id"], entity_id)
    rows = catalog.db.execute(
        "SELECT f.relative_path,l.directory FROM media_files f JOIN library_entities e ON e.id=f.entity_id JOIN libraries l ON l.id=e.library_id WHERE f.id=? AND f.entity_id=? AND f.role='subtitle'",
        (media_file_id, entity_id),
    )
    if not rows:
        raise HTTPException(404, "Subtitle track not found.")
    source = Path(rows[0][1]) / rows[0][0]
    if source.suffix.lower() == ".vtt":
        return FileResponse(source, media_type="text/vtt")
    executable = ffmpeg_path()
    if not executable:
        raise HTTPException(503, "FFmpeg is not available.")
    target_root = Path(catalog.db.db_file).parent / "subtitle-cache"
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"{hashlib.sha256(str(source).encode()).hexdigest()}.vtt"
    if not target.is_file() or target.stat().st_mtime_ns < source.stat().st_mtime_ns:
        completed = await asyncio.create_subprocess_exec(
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await completed.wait() != 0:
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
