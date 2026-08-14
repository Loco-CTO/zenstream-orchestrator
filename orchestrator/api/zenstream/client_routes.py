from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.catalog import LOCAL_ARTWORK_NAMES, Catalog
from app.catalog_read_model import CatalogReadModel
from app.client_auth import (
    CLIENT_SESSION_COOKIE,
    DEV_CLIENT_SESSION_COOKIE,
    account_from_access,
    cookie_secure,
    issue_ticket,
    require_account,
    session_cookie_name,
    session_id_for_token,
    websocket_account,
)
from app.foreground import run_foreground
from app.images import LocalArtworkCache
from app.intro_outro import IntroOutroStore
from app.language_registry import language_options
from app.logging_config import get_logger
from app.models.account import Account
from app.models.account_preference import AccountPreference
from app.models.metadata import MetadataLanguageSettings
from app.playback import PlaybackManager, ffmpeg_path
from app.trickplay import TrickplayExtractor
from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from api.zenstream.library_routes import authenticate_admin_request

router = APIRouter()
catalog = Catalog()
media = PlaybackManager()
trickplay = TrickplayExtractor()
intro_outro = IntroOutroStore()
logger = get_logger("playback_routes")
AUTH_BODY_LIMIT_BYTES = 16 * 1024
RESOURCE_TICKET_TTL_SECONDS = 15 * 60
_RATE_LIMIT_EVENTS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_ARTWORK_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, min(32, int(os.getenv("ARTWORK_RESOLVE_WORKERS", "8")))),
    thread_name_prefix="zenstream-artwork",
)
CARD_METADATA_FIELDS = {
    "title",
    "date",
    "year",
    "runtimeMinutes",
    "communityRating",
    "officialRating",
    "tags",
    "genres",
    "images",
}


async def _require_account(request: Request) -> tuple[dict, str]:
    return await run_foreground(require_account, request)


def _catalog_item(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("metadata"), dict)
        and "userState" in value
    )


def _card_item(value: dict) -> dict:
    result = dict(value)
    source_metadata = value.get("metadata")
    metadata = {
        key: field
        for key, field in source_metadata.items()
        if key in CARD_METADATA_FIELDS
    }
    images = metadata.get("images")
    if isinstance(images, dict):
        metadata["images"] = {
            image_type: image
            for image_type in ("Primary", "Backdrop")
            if isinstance((image := images.get(image_type)), dict)
        }
    result["metadata"] = metadata
    return result


def _catalog_response(value, view: str | None, limit: int | None = None):
    if view not in {None, "full", "card"}:
        raise HTTPException(400, "Unsupported catalog view.")
    if view != "card" and limit is None:
        return value

    def transform(current):
        if _catalog_item(current):
            return _card_item(current) if view == "card" else current
        if isinstance(current, list):
            items = [transform(item) for item in current]
            if (
                limit is not None
                and items
                and all(_catalog_item(item) for item in items)
            ):
                return items[:limit]
            return items
        if isinstance(current, dict):
            return {key: transform(item) for key, item in current.items()}
        return current

    return transform(value)


def _client_address(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    trusted = {
        value.strip()
        for value in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
        if value.strip()
    }
    if peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return peer


def _enforce_rate_limit(
    request: Request, bucket: str, limit: int, window: int = 60
) -> None:
    now = time.monotonic()
    key = (bucket, _client_address(request))
    events = _RATE_LIMIT_EVENTS[key]
    while events and now - events[0] >= window:
        events.popleft()
    if len(events) >= limit:
        retry_after = max(1, int(window - (now - events[0])))
        raise HTTPException(
            429,
            "Too many requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    events.append(now)


async def _bounded_json_object(
    request: Request, limit: int = AUTH_BODY_LIMIT_BYTES
) -> dict:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
            if length < 0:
                raise ValueError
            if length > limit:
                raise HTTPException(413, "Request body is too large.")
        except ValueError as error:
            raise HTTPException(400, "Invalid Content-Length header.") from error
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, "Request body is too large.")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(400, "A JSON object is required.") from error
    if not isinstance(value, dict):
        raise HTTPException(400, "A JSON object is required.")
    return value


def _catalog_status_payload(user_id: str) -> dict:
    status = CatalogReadModel(catalog.db).status()
    allowed = catalog.allowed_libraries(user_id)
    has_library_summary = bool(
        catalog.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_library_summary'"
        )
    )
    libraries = (
        catalog.db.execute(
            (
                f"SELECT l.id,l.scan_state,l.last_scan_finished_at,COALESCE(p.generation,0),p.last_root_entity_id "
                f"FROM libraries l LEFT JOIN catalog_library_summary p ON p.library_id=l.id "
                f"WHERE l.id IN ({','.join('?' for _ in allowed)}) ORDER BY l.id"
                if has_library_summary
                else f"SELECT id,scan_state,last_scan_finished_at,0,NULL FROM libraries WHERE id IN ({','.join('?' for _ in allowed)}) ORDER BY id"
            ),
            sorted(allowed),
        )
        if allowed
        else []
    )
    return {
        "generation": int(status[1] or 0) if status else 0,
        "state": status[0] if status else "unavailable",
        "updatedAt": status[2] if status else None,
        "libraries": [
            {
                "id": row[0],
                "scanState": row[1],
                "lastScanFinishedAt": row[2],
                "catalogGeneration": int(row[3]),
                "lastRootEntityId": row[4],
            }
            for row in libraries
        ],
    }


def _catalog_status_fingerprint(payload: dict) -> tuple:
    return (
        payload["generation"],
        payload["state"],
        tuple(
            (
                value["id"],
                value["scanState"],
                value["lastScanFinishedAt"],
                value["catalogGeneration"],
                value["lastRootEntityId"],
            )
            for value in payload["libraries"]
        ),
    )


@router.post("/api/auth/login")
async def login(request: Request):
    _enforce_rate_limit(request, "login", 10)
    data = await _bounded_json_object(request)
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
    account, _ = await _require_account(request)
    return {"user": account}


@router.get("/api/auth/bootstrap")
async def auth_bootstrap(request: Request):
    """Return the authenticated session's small, parallel-startup payload.

    This is additive to ``/auth/me`` and deliberately keeps the ticket optional:
    catalog JSON can render before artwork authorization is available.
    """
    account, token = await _require_account(request)
    session_id = await run_foreground(session_id_for_token, token)
    if not session_id:
        raise HTTPException(401, "Authentication required.")
    preference = AccountPreference(account["id"])
    locale, metadata_language, subtitles, languages = await asyncio.gather(
        run_foreground(preference.locale),
        run_foreground(preference.metadata_language),
        run_foreground(preference.subtitle_style),
        run_foreground(MetadataLanguageSettings().get),
    )
    return {
        "user": account,
        "resourceTicket": issue_ticket(
            account["id"],
            "resource",
            RESOURCE_TICKET_TTL_SECONDS,
            sessionId=session_id,
        ),
        "resourceTicketExpiresIn": RESOURCE_TICKET_TTL_SECONDS,
        "locale": locale,
        "metadataLanguage": metadata_language,
        "subtitleStyle": subtitles,
        "languages": languages,
        "languageOptions": language_options(),
    }


@router.post("/api/auth/browser-login")
async def browser_login(request: Request):
    """Same-site browser login with an HttpOnly bearer cookie."""
    _enforce_rate_limit(request, "login", 10)
    data = await _bounded_json_object(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    account = Account().authenticate_password(username, password)
    if not account:
        raise HTTPException(401, "Invalid credentials.")
    session = Account().create_session(account["id"])
    response = JSONResponse({"user": account}, status_code=200)
    response.set_cookie(
        session_cookie_name(request),
        session["token"],
        max_age=7 * 24 * 60 * 60,
        secure=cookie_secure(request),
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(
        DEV_CLIENT_SESSION_COOKIE,
        secure=False,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request):
    _, token = await _require_account(request)
    Account().revoke(token)
    response = Response(status_code=204)
    primary_cookie = session_cookie_name(request)
    response.delete_cookie(
        primary_cookie,
        secure=cookie_secure(request),
        httponly=True,
        samesite="strict",
        path="/",
    )
    if primary_cookie != CLIENT_SESSION_COOKIE:
        response.delete_cookie(
            CLIENT_SESSION_COOKIE,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
    if primary_cookie != DEV_CLIENT_SESSION_COOKIE:
        response.delete_cookie(
            DEV_CLIENT_SESSION_COOKIE,
            secure=False,
            httponly=True,
            samesite="strict",
            path="/",
        )
    return response


@router.get("/api/auth/resource-ticket")
async def resource_ticket(request: Request):
    account, token = await _require_account(request)
    session_id = await run_foreground(session_id_for_token, token)
    if not session_id:
        raise HTTPException(401, "Authentication required.")
    return {
        "ticket": issue_ticket(
            account["id"],
            "resource",
            RESOURCE_TICKET_TTL_SECONDS,
            sessionId=session_id,
        ),
        "expiresIn": RESOURCE_TICKET_TTL_SECONDS,
    }


@router.post("/api/auth/socket-ticket")
async def socket_ticket(request: Request):
    account, token = await _require_account(request)
    session_id = await run_foreground(session_id_for_token, token)
    if not session_id:
        raise HTTPException(401, "Authentication required.")
    return {
        "ticket": issue_ticket(account["id"], "socket", 60, sessionId=session_id),
        "expiresIn": 60,
    }


@router.get("/api/catalog/status")
async def catalog_status(request: Request):
    account, _ = await _require_account(request)
    return await asyncio.to_thread(_catalog_status_payload, account["id"])


@router.websocket("/api/ws/catalog")
async def catalog_socket(websocket: WebSocket):
    account = websocket_account(websocket)
    if not account:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    previous = None
    last_sent = 0.0
    try:
        while True:
            payload = await asyncio.to_thread(_catalog_status_payload, account["id"])
            fingerprint = _catalog_status_fingerprint(payload)
            current = asyncio.get_running_loop().time()
            changed = fingerprint != previous
            if changed or current - last_sent >= 15:
                if previous is not None and changed:
                    previous_libraries = {value[0]: value for value in previous[2]}
                    for library in payload["libraries"]:
                        current_value = (
                            library["id"],
                            library["scanState"],
                            library["lastScanFinishedAt"],
                            library["catalogGeneration"],
                            library["lastRootEntityId"],
                        )
                        if previous_libraries.get(library["id"]) == current_value:
                            continue
                        await websocket.send_json(
                            {
                                "type": "catalog.updated",
                                "libraryId": library["id"],
                                "rootEntityId": library["lastRootEntityId"],
                                "generation": library["catalogGeneration"],
                                "reason": "scan"
                                if library["scanState"] != "idle"
                                else "refresh",
                            }
                        )
                else:
                    await websocket.send_json({"type": "catalog.status", **payload})
                previous = fingerprint
                last_sent = current
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError):
        return


@router.get("/api/metadata/languages")
async def metadata_languages(request: Request):
    await _require_account(request)
    return {"languages": MetadataLanguageSettings().get()}


@router.get("/api/languages")
async def supported_languages(request: Request):
    await _require_account(request)
    return {"languages": language_options()}


@router.get("/api/preferences/locale")
async def get_locale(request: Request):
    account, _ = await _require_account(request)
    return {"locale": AccountPreference(account["id"]).locale()}


@router.patch("/api/preferences/locale")
async def set_locale(request: Request):
    account, _ = await _require_account(request)
    try:
        return {
            "locale": AccountPreference(account["id"]).set_locale(
                (await _bounded_json_object(request)).get("locale")
            )
        }
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/preferences/metadata-language")
async def get_metadata_language(request: Request):
    account, _ = await _require_account(request)
    return AccountPreference(account["id"]).metadata_language()


@router.patch("/api/preferences/metadata-language")
async def set_metadata_language(request: Request):
    account, _ = await _require_account(request)
    try:
        return AccountPreference(account["id"]).set_metadata_language(
            (await _bounded_json_object(request)).get("language")
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/preferences/subtitles")
async def get_subtitles(request: Request):
    account, _ = await _require_account(request)
    return AccountPreference(account["id"]).subtitle_style()


@router.patch("/api/preferences/subtitles")
async def set_subtitles(request: Request):
    account, _ = await _require_account(request)
    try:
        return AccountPreference(account["id"]).set_subtitle_style(
            await _bounded_json_object(request)
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/catalog/libraries")
async def libraries(
    request: Request,
    language: str | None = Query(None),
    includeFirstPage: bool = Query(False),
    page: int = Query(1, ge=1),
    pageSize: int = Query(40, ge=1, le=100),
    sortBy: str | None = Query(None),
    sortOrder: str = Query("ascending"),
):
    account, _ = await _require_account(request)
    libraries_value = await run_foreground(catalog.libraries, account["id"])
    if not includeFirstPage:
        return {"libraries": libraries_value}
    preferred = await run_foreground(_preferred, account, language)
    first_pages = await asyncio.gather(
        *(
            run_foreground(
                catalog.list_items,
                account["id"],
                library["id"],
                preferred,
                page=page,
                page_size=pageSize,
                sort_by=sortBy,
                sort_order=sortOrder,
            )
            for library in libraries_value
        )
    )
    return {"libraries": libraries_value, "initialPage": first_pages}


@router.get("/api/catalog/home")
async def home(
    request: Request,
    language: str | None = Query(None),
    section: str | None = Query(None),
    libraryId: str | None = Query(None),
    view: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=100),
):
    account, _ = await _require_account(request)
    preferred = await run_foreground(_preferred, account, language)
    if section is None:
        result = await run_foreground(catalog.home, account["id"], preferred)
        return _catalog_response(result, view, limit)
    if section == "featured":
        return _catalog_response(
            {
                "latestItems": await run_foreground(
                    catalog.home_featured, account["id"], preferred
                )
            },
            view,
            limit,
        )
    if section == "continueWatching":
        return _catalog_response(
            {
                "continueWatching": await run_foreground(
                    catalog.home_continue_watching, account["id"], preferred
                )
            },
            view,
            limit,
        )
    if section == "nextUp":
        return _catalog_response(
            {
                "nextUp": await run_foreground(
                    catalog.home_next_up, account["id"], preferred
                )
            },
            view,
            limit,
        )
    if section == "derived":
        result = await run_foreground(catalog.home_derived, account["id"], preferred)
        return _catalog_response(result, view, limit)
    if section == "library":
        if not libraryId:
            raise HTTPException(
                400, "libraryId is required for the library Home section."
            )
        rows = await run_foreground(
            catalog.home_library_rows, account["id"], preferred, libraryId
        )
        if rows is None:
            raise HTTPException(404, "Library not found.")
        return _catalog_response({"libraryRows": rows}, view, limit)
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
    view: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=100),
):
    account, _ = await _require_account(request)
    preferred = await run_foreground(_preferred, account, language)
    effective_page_size = min(pageSize, limit) if limit is not None else pageSize
    result = await run_foreground(
        catalog.list_items,
        account["id"],
        libraryId,
        preferred,
        parent_id=parentId,
        page=page,
        page_size=effective_page_size,
        sort_by=sortBy,
        sort_order=sortOrder,
    )
    return _catalog_response(result, view)


@router.get("/api/catalog/search")
async def search(
    request: Request,
    query: str = Query(..., min_length=1, max_length=200),
    language: str | None = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(40, ge=1, le=100),
    view: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=100),
):
    account, _ = await _require_account(request)
    preferred = await run_foreground(_preferred, account, language)
    effective_page_size = min(pageSize, limit) if limit is not None else pageSize
    result = await run_foreground(
        catalog.search,
        account["id"],
        query,
        preferred,
        page,
        effective_page_size,
    )
    return _catalog_response(result, view)


@router.get("/api/catalog/favorites")
async def favorites(
    request: Request,
    language: str | None = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(100, ge=1, le=100),
    sortBy: str = Query("title"),
    sortOrder: str = Query("ascending"),
    view: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=100),
):
    account, _ = await _require_account(request)
    preferred = await run_foreground(_preferred, account, language)
    effective_page_size = min(pageSize, limit) if limit is not None else pageSize
    result = await run_foreground(
        catalog.favorites,
        account["id"],
        preferred,
        page,
        effective_page_size,
        sortBy,
        sortOrder,
    )
    return _catalog_response(result, view)


@router.get("/api/catalog/items/{entity_id}")
async def item(
    entity_id: str,
    request: Request,
    language: str | None = Query(None),
    view: str | None = Query(None),
):
    account, _ = await _require_account(request)
    preferred = await run_foreground(_preferred, account, language)
    result = await run_foreground(catalog.item, account["id"], entity_id, preferred)
    return _catalog_response(result, view)


@router.get("/api/catalog/items/{entity_id}/similar")
async def similar(
    entity_id: str,
    request: Request,
    language: str | None = Query(None),
    view: str | None = Query(None),
    limit: int = Query(8, ge=1, le=40),
):
    account, _ = await _require_account(request)
    preferred = await run_foreground(_preferred, account, language)
    result = await run_foreground(
        catalog.similar, account["id"], entity_id, preferred, limit
    )
    return _catalog_response(result, view)


@router.get("/api/catalog/items/{entity_id}/metadata")
async def item_metadata(entity_id: str, request: Request, language: str = Query(...)):
    account, _ = await _require_account(request)
    return await run_foreground(
        catalog.metadata, account["id"], entity_id, language, include_credits=True
    )


@router.get("/api/catalog/items/{entity_id}/images/{image_type}")
async def item_image(
    entity_id: str, image_type: str, request: Request, language: str = Query(...)
):
    requested_version = request.query_params.get("v")

    def resolve_cached_image() -> Path | None:
        account = account_from_access(request)
        row = catalog.require_entity(account["id"], entity_id)
        library_rows = catalog.db.execute(
            "SELECT directory FROM libraries WHERE id=?", (row[1],)
        )
        directory = (
            Path(library_rows[0][0]) if library_rows and library_rows[0][0] else None
        )
        if directory:
            for relative_path, content_hash in catalog.db.execute(
                "SELECT relative_path,quick_fingerprint FROM media_files WHERE entity_id=? AND role='image' ORDER BY relative_path COLLATE NOCASE",
                (entity_id,),
            ):
                candidate = directory / relative_path
                if (
                    candidate.stem.lower() in LOCAL_ARTWORK_NAMES.get(image_type, set())
                    and candidate.is_file()
                ):
                    cached = LocalArtworkCache(catalog.db).path(content_hash)
                    if cached and cached.is_file():
                        return cached
                    raise HTTPException(404, "Image not found.")
        # The catalog selection is canonical. Request-time provider reselection
        # can disagree with the projection while a secondary provider refreshes
        # and would reject a valid versioned URL from the catalog payload.
        if catalog._has_table("catalog_artwork_selection"):
            projected = catalog.db.execute(
                "SELECT local_path,version FROM catalog_artwork_selection WHERE entity_id=? AND locale=? AND image_type=?",
                (entity_id, language, image_type),
            )
            if projected:
                selected_path, selected_version = projected[0]
                if requested_version and requested_version != selected_version:
                    # Versioned URLs are immutable.  Never substitute a
                    # different fallback file under a stale client URL.
                    raise HTTPException(404, "Image version is no longer available.")
                if selected_path and Path(selected_path).is_file():
                    return Path(selected_path)
                if requested_version:
                    raise HTTPException(404, "Image version is no longer available.")
                return None
        # Legacy/cache-only fallback is used only when no canonical selection
        # exists. It never substitutes a different file for a versioned URL.
        image = catalog.selected_image(account["id"], entity_id, language, image_type)
        if image:
            provider = image.get("provider")
            provider_id = next(
                (
                    identity.get("id")
                    for identity in catalog._provider_ids(entity_id, row[3])
                    if identity.get("provider") == provider
                ),
                None,
            )
            rows = catalog.db.execute(
                "SELECT local_path FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=? AND image_type=? AND image_url=? AND local_path IS NOT NULL LIMIT 1",
                (provider, row[3], provider_id, image_type, image.get("url")),
            )
            if rows and rows[0][0] and Path(rows[0][0]).is_file():
                if requested_version:
                    raise HTTPException(404, "Image version is no longer available.")
                return Path(rows[0][0])
        if requested_version:
            raise HTTPException(404, "Image version is no longer available.")
        return None

    cached_image = await asyncio.get_running_loop().run_in_executor(
        _ARTWORK_EXECUTOR, resolve_cached_image
    )
    if cached_image:
        versioned = bool(request.query_params.get("v"))
        return FileResponse(
            cached_image,
            media_type="image/webp",
            headers={
                "Cache-Control": (
                    "private, max-age=31536000, immutable"
                    if versioned
                    else "private, max-age=300"
                )
            },
        )
    return Response(
        status_code=202,
        headers={"Retry-After": "2", "X-ZenStream-Image-State": "pending"},
    )


@router.get("/api/catalog/items/{entity_id}/people/{person_id}/image")
async def person_image(entity_id: str, person_id: str, request: Request):
    account = await asyncio.get_running_loop().run_in_executor(
        _ARTWORK_EXECUTOR, account_from_access, request
    )
    image = await asyncio.get_running_loop().run_in_executor(
        _ARTWORK_EXECUTOR, catalog.person_image, account["id"], entity_id, person_id
    )
    if image is None:
        raise HTTPException(404, "Person image not found.")
    return FileResponse(
        image,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/api/catalog/items/{entity_id}/detail")
async def item_detail(
    entity_id: str,
    request: Request,
    language: str | None = Query(None),
    seasonId: str | None = Query(None),
    section: str | None = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(40, ge=1, le=100),
    view: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=100),
):
    account, _ = await _require_account(request)
    preferred = await run_foreground(_preferred, account, language)
    effective_page_size = min(pageSize, limit) if limit is not None else pageSize
    result = await run_foreground(
        catalog.detail,
        account["id"],
        entity_id,
        preferred,
        seasonId,
        section,
        page,
        effective_page_size,
    )
    return _catalog_response(result, view)


@router.patch("/api/catalog/items/{entity_id}/state")
async def update_item_state(entity_id: str, request: Request):
    account, _ = await _require_account(request)
    state = await request.json()
    return await asyncio.to_thread(
        catalog.update_state, account["id"], entity_id, state
    )


@router.post("/api/playback/items/{entity_id}/negotiate")
async def negotiate_playback(entity_id: str, request: Request):
    account, token = await _require_account(request)
    session_id = await run_foreground(session_id_for_token, token)
    return await asyncio.to_thread(
        media.negotiate,
        account["id"],
        entity_id,
        await request.json(),
        session_id,
    )


@router.get("/api/playback/items/{entity_id}/source")
async def playback_source_metadata(entity_id: str, request: Request):
    account, _ = await _require_account(request)
    return await asyncio.to_thread(media.source_metadata, account["id"], entity_id)


@router.get("/api/playback/items/{entity_id}/trickplay")
async def trickplay_manifest(
    entity_id: str, request: Request, sourceId: str | None = Query(None)
):
    account, token = await _require_account(request)
    session_id = await run_foreground(session_id_for_token, token)
    catalog.require_entity(account["id"], entity_id)
    payload = await asyncio.to_thread(
        trickplay.manifest, account["id"], entity_id, sourceId, session_id
    )
    if payload["state"] != "ready":
        return Response(
            content=json.dumps(payload),
            status_code=202,
            headers={"Retry-After": "5"},
            media_type="application/json",
        )
    return payload


@router.get("/api/playback/items/{entity_id}/segments")
async def playback_segments(
    entity_id: str, request: Request, sourceId: str | None = Query(None)
):
    account, _ = await _require_account(request)
    catalog.require_entity(account["id"], entity_id)
    return await asyncio.to_thread(intro_outro.segments, entity_id, sourceId)


@router.get("/api/playback/items/{entity_id}/trickplay/{generation}/{sheet_index}.webp")
async def trickplay_sheet(
    entity_id: str, generation: str, sheet_index: int, request: Request
):
    account = account_from_access(request)
    catalog.require_entity(account["id"], entity_id)
    path = await asyncio.to_thread(
        trickplay.sheet_path, entity_id, generation, sheet_index
    )
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=3600"},
    )


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
        return FileResponse(
            path, media_type=media_type, headers={"Accept-Ranges": "bytes"}
        )
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

    return StreamingResponse(
        chunks(), status_code=206, headers=headers, media_type=media_type
    )


@router.get("/api/playback/sessions/{session_id}/{filename}")
async def playback_output(session_id: str, filename: str, request: Request):
    account = account_from_access(request)
    logger.debug(
        "playback output request session_id=%s filename=%s user_id=%s",
        session_id,
        filename,
        account["id"],
    )
    path = await asyncio.to_thread(
        media.session_file, account["id"], session_id, filename
    )
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
            cues.append(
                f"{index + 1}\n{_vtt_time(start)} --> {_vtt_time(max(end, start + 0.5))}\n{lyric}\n"
            )
        return "WEBVTT\n\n" + "\n".join(cues)
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("[")
    ]
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
        if (
            not target.is_file()
            or target.stat().st_mtime_ns < source.stat().st_mtime_ns
        ):
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
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    authenticate_admin_request(request, TOKEN)
    return {"users": Account().list()}


@router.post("/api/admin/users", status_code=201)
async def create_user(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    authenticate_admin_request(request, TOKEN)
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
    authenticate_admin_request(request, TOKEN)
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
    authenticate_admin_request(request, TOKEN)
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
    authenticate_admin_request(request, TOKEN)
    try:
        return Account().set_disabled(
            user_id, bool((await request.json()).get("disabled"))
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.delete("/api/admin/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    authenticate_admin_request(request, TOKEN)
    if not Account().delete(user_id):
        raise HTTPException(404, "User not found.")
