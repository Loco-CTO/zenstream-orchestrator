from __future__ import annotations

import asyncio
import contextvars
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.images import LocalArtworkCache
from app.intro_outro import IntroOutroStore, render_audio_preview
from app.jobs import scheduler
from app.library import LibraryStore, runtime
from app.logging_config import get_logger
from app.metadata_domain import choose_artwork
from app.metadata_services import MetadataIngestService, MetadataReadService
from app.models.admin import ADMIN_SESSION_COOKIE, Admin
from app.models.metadata import (
    IMAGE_LANGUAGE_SCHEMA,
    MetadataCredentials,
    MetadataLanguageSettings,
    normalize_metadata_locale,
)
from app.providers import (
    IMAGE_TYPES,
    PRIMARY_PROVIDER_BY_ENTITY,
    MetadataService,
    ProviderError,
)
from app.search_scoring import match_score, normalize_search_text
from app.trickplay import TrickplayExtractor
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

logger = get_logger("library_api")
store = LibraryStore()
credentials = MetadataCredentials()
_admin_hydration: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "admin_catalog_hydration", default=None
)


def _trickplay_asset(entity_id: str) -> dict | None:
    tables = {
        row[0]
        for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not {"trickplay_assets", "trickplay_sheets"}.issubset(tables):
        return None
    rows = store.db.execute(
        "SELECT media_file_id,frame_width,frame_height,interval_seconds,state,output_key,error "
        "FROM trickplay_assets WHERE entity_id=? ORDER BY updated_at DESC LIMIT 1",
        (entity_id,),
    )
    if not rows:
        return None
    media_file_id, width, height, interval, state, generation, error = rows[0]
    value = {
        "mediaFileId": media_file_id,
        "frameWidth": width,
        "frameHeight": height,
        "intervalSeconds": interval,
        "state": state,
        "generation": generation,
        "error": error,
        "frameCount": 0,
        "sheets": [],
    }
    if state != "ready" or not generation:
        return value
    sheets = store.db.execute(
        "SELECT sheet_index,frame_count FROM trickplay_sheets "
        "WHERE media_file_id=? AND output_key=? ORDER BY sheet_index",
        (media_file_id, generation),
    )
    value["frameCount"] = sum(row[1] for row in sheets)
    value["sheets"] = [{"index": row[0], "frameCount": row[1]} for row in sheets]
    return value


_admin_identity: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "admin_identity", default=None
)


def authenticate_admin_request(
    request: Request, token_header: str | None = None
) -> str:
    cookie_token = request.cookies.get(ADMIN_SESSION_COOKIE) or request.cookies.get(
        "zenstream-admin-session"
    )
    # Administrator sessions are browser-only and must never be supplied via a
    # client-controlled bearer/header value.  `require_admin` retains its
    # direct-call fallback for internal jobs/tests, but HTTP routes require the
    # HttpOnly cookie boundary here.
    if not cookie_token:
        raise HTTPException(403, "Administrator cookie authentication is required.")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        forwarded_proto = request.headers.get("x-forwarded-proto")
        forwarded_host = request.headers.get("x-forwarded-host")
        expected = f"{forwarded_proto or request.url.scheme}://{forwarded_host or request.headers.get('host', request.url.netloc)}"
        if (
            not origin
            or urlsplit(origin).scheme + "://" + urlsplit(origin).netloc != expected
        ):
            raise HTTPException(403, "Cross-site administrator request rejected.")
    admin = Admin.from_token(cookie_token)
    if admin is None:
        raise HTTPException(403, "Invalid administrator credentials.")
    return admin.username


async def _admin_boundary(request: Request):
    identity = authenticate_admin_request(request)
    context_token = _admin_identity.set(identity)
    try:
        yield
    finally:
        _admin_identity.reset(context_token)


def require_admin(username: str | None = None, token: str | None = None) -> str:
    identity = _admin_identity.get()
    if identity:
        return identity
    admin = Admin.from_token(token)
    if admin is None:
        raise HTTPException(403, "Invalid administrator credentials.")
    return admin.username


router = APIRouter(prefix="/api/admin", dependencies=[Depends(_admin_boundary)])


def _configured_locale(value: str | None) -> str:
    configured = MetadataLanguageSettings().get()
    requested = normalize_metadata_locale(value) if value else configured[0]
    if requested not in configured:
        raise HTTPException(400, "Metadata language is not configured.")
    return requested


def _entity_ids(entity_id: str) -> list[dict]:
    hydration = _admin_hydration.get()
    if hydration is not None and entity_id in hydration["providers"]:
        return hydration["providers"][entity_id]
    entity_rows = store.db.execute(
        "SELECT entity_type FROM library_entities WHERE id=?", (entity_id,)
    )
    entity_type = entity_rows[0][0] if entity_rows else ""
    primary_provider = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
    rows = store.db.execute(
        "SELECT provider,identifier_type,provider_id,is_primary FROM entity_provider_ids WHERE entity_id=? ORDER BY CASE WHEN provider=? THEN 0 ELSE 1 END, provider",
        (entity_id, primary_provider),
    )
    value = [
        {
            "provider": row[0],
            "type": row[1],
            "id": row[2],
            "primary": row[0] == primary_provider,
            "role": "primary" if row[0] == primary_provider else "secondary",
        }
        for row in rows
    ]
    if hydration is not None:
        hydration["providers"][entity_id] = value
    return value


def _entity(entity_id: str, locale: str = "en", include_metadata: bool = False) -> dict:
    hydration = _admin_hydration.get()
    row = hydration["entities"].get(entity_id) if hydration is not None else None
    rows = (
        []
        if row is not None
        else store.db.execute(
            "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,disc_number,track_number,match_status,match_confidence,match_method FROM library_entities WHERE id=?",
            (entity_id,),
        )
    )
    if row is None and not rows:
        raise HTTPException(404, "Library item not found.")
    row = row or rows[0]
    value = {
        "id": row[0],
        "libraryId": row[1],
        "parentId": row[2],
        "type": row[3],
        "primaryProvider": PRIMARY_PROVIDER_BY_ENTITY.get(row[3]),
        "relativePath": row[4],
        "seasonNumber": row[5],
        "episodeNumber": row[6],
        "episodeEndNumber": row[7],
        "discNumber": row[8],
        "trackNumber": row[9],
        "matchStatus": row[10],
        "matchConfidence": row[11],
        "matchMethod": row[12],
        "providerIds": _entity_ids(entity_id),
    }
    value["displayName"] = (
        Path(row[4]).name if row[4] else row[3].replace("_", " ").title()
    )
    if row[3] in {"movie", "episode"}:
        value["trickplay"] = _trickplay_asset(entity_id)
    children = (
        hydration["children"].get(entity_id, [])
        if hydration is not None
        else store.db.execute(
            "SELECT id,entity_type,relative_path,season_number,episode_number,track_number FROM library_entities WHERE parent_id=? ORDER BY season_number,episode_number,track_number,relative_path COLLATE NOCASE",
            (entity_id,),
        )
    )
    value["children"] = [
        {
            "id": child[0],
            "type": child[1],
            "relativePath": child[2],
            "displayName": Path(child[2]).stem
            if child[2]
            else child[1].replace("_", " ").title(),
            "seasonNumber": child[3],
            "episodeNumber": child[4],
            "trackNumber": child[5],
        }
        for child in children
    ]
    if include_metadata:
        value["metadata"] = None
    return value


def _image_entities(item: dict, image_type: str) -> list[dict]:
    entities = [item]
    if image_type != "Primary" or item.get("type") not in {"season", "episode"}:
        return entities
    seen = {item["id"]}
    parent_id = item.get("parentId")
    while parent_id and parent_id not in seen:
        parent = _entity(parent_id)
        entities.append(parent)
        seen.add(parent_id)
        parent_id = parent.get("parentId")
    return entities


def _metadata_for(
    item: dict, locale: str, fetch: bool = False, fallback: bool = True
) -> dict | None:
    del fetch, fallback
    requested = normalize_metadata_locale(locale)
    if requested not in MetadataLanguageSettings().get():
        raise HTTPException(400, "Metadata language is not configured.")
    hydration = _admin_hydration.get()
    if hydration is not None:
        value = hydration["metadata"].get((item["id"], requested))
        if value is not None:
            return value
    value = MetadataReadService(store.db).resolve_raw(
        item["type"], item.get("providerIds", []), requested
    )
    return value or None


def _refresh_item_metadata_sync(entity_id: str) -> dict:
    from app.catalog_read_model import CatalogReadModel

    item = _entity(entity_id)
    if item["type"] not in {"movie", "series", "season", "episode"}:
        raise HTTPException(
            400,
            "Metadata refresh is available only for movies, series, seasons, and episodes.",
        )
    identities = [
        identity
        for identity in item.get("providerIds", [])
        if identity.get("provider") in {"tmdb", "tvdb"} and identity.get("id")
    ]
    if not identities:
        raise HTTPException(409, "This item has no supported provider identity.")
    service = MetadataService()
    ingest = MetadataIngestService(service, background_assets=False)
    locales = ingest.locales()
    refreshed = []
    failures = []
    for identity in identities:
        provider = str(identity["provider"])
        provider_id = str(identity["id"])
        try:
            ingest.ingest_locales(
                provider,
                item["type"],
                provider_id,
                locales,
                force=True,
            )
            refreshed.append(provider)
        except (ProviderError, ValueError, OSError) as error:
            logger.exception(
                "manual item metadata refresh failed entity_id=%s provider=%s provider_id=%s",
                entity_id,
                provider,
                provider_id,
            )
            failures.append(
                {
                    "provider": provider,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    CatalogReadModel(store.db).refresh_roots([entity_id])
    if not refreshed:
        raise HTTPException(
            502,
            {
                "message": "Metadata and artwork could not be refreshed.",
                "failures": failures,
            },
        )
    return {
        "itemId": entity_id,
        "state": "completed_with_warnings" if failures else "completed",
        "locales": locales,
        "providers": refreshed,
        "failures": failures,
    }


def _local_image_for_type(relative_path: str, image_type: str) -> bool:
    """Match conventional local artwork names to their canonical category."""
    stem = Path(relative_path).stem.lower()
    names = {
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
    return stem in names.get(image_type, set())


_search_text = normalize_search_text
_trigram_score = match_score


def _rank_library_item_ids(
    db, library_id: str, parent_id: str | None, locale: str, query: str
) -> list[str]:
    """Rank one library level using fuzzy trigrams over paths and localized titles."""
    rows = db.execute(
        """SELECT e.id,e.entity_type,e.relative_path,m.payload
           FROM library_entities e
           LEFT JOIN entity_provider_ids p ON p.entity_id=e.id
           LEFT JOIN metadata_cache m ON m.provider=p.provider
             AND m.entity_type=e.entity_type AND m.provider_id=p.provider_id AND m.locale=?
           WHERE e.library_id=? AND e.parent_id IS ?""",
        ((locale or "en").lower(), library_id, parent_id),
    )
    candidates: dict[str, dict] = {}
    for entity_id, entity_type, relative_path, payload_text in rows:
        candidate = candidates.setdefault(
            entity_id, {"type": entity_type, "path": relative_path or "", "values": []}
        )
        if payload_text:
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError):
                payload = {}
            if payload.get(
                "_imageLanguageSchema"
            ) == IMAGE_LANGUAGE_SCHEMA and payload.get("title"):
                candidate["values"].append(str(payload["title"]))
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return []
    ranked = []
    for entity_id, candidate in candidates.items():
        path = candidate["path"]
        values = [path, Path(path).stem, *candidate["values"]]
        score = max(
            (match_score(normalized_query, value) for value in values), default=0.0
        )
        if score > 0:
            ranked.append((-score, candidate["type"], path.casefold(), entity_id))
    ranked.sort()
    return [value[3] for value in ranked]


@router.get("/metadata/providers")
async def provider_status(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    return credentials.configured()


@router.get("/metadata/languages")
async def metadata_languages(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    return {
        "locales": MetadataLanguageSettings().get(),
        "options": await asyncio.to_thread(MetadataService().language_options),
    }


@router.put("/metadata/languages")
async def update_metadata_languages(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        locales = MetadataLanguageSettings().set(data.get("locales"))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    run = scheduler.enqueue_metadata_refresh()
    return {"locales": locales, "backfill": run}


@router.post("/metadata/refresh")
async def refresh_metadata(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    """Explicitly repair/refetch all indexed provider metadata and artwork."""
    require_admin(Username, TOKEN)
    return {"backfill": scheduler.enqueue_metadata_refresh()}


@router.put("/metadata/providers/{provider}")
async def update_provider(
    provider: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    provider = provider.lower()
    if provider not in {"tmdb", "tvdb"}:
        raise HTTPException(400, "Only TMDB and TheTVDB credentials can be configured.")
    data = await request.json()
    if data.get("clear"):
        credentials.clear(provider)
        return credentials.configured()[provider]
    if provider == "tmdb":
        value = str(data.get("credential") or data.get("apiKey") or "").strip()
        credential_type = str(data.get("credentialType") or "api_key")
        if not value or credential_type not in {
            "api_key",
            "read_access_token",
            "bearer",
            "v4",
        }:
            raise HTTPException(400, "A valid TMDB credential and type are required.")
        credential = {"value": value}
    else:
        value = str(data.get("apiKey") or data.get("credential") or "").strip()
        if not value:
            raise HTTPException(400, "A TheTVDB API key is required.")
        credential_type = "api_key"
        credential = {"apiKey": value, "pin": str(data.get("pin") or "").strip()}
    if data.get("validate", True):
        try:
            await asyncio.to_thread(
                MetadataService().test, provider, credential, credential_type
            )
        except ProviderError as error:
            raise HTTPException(400, f"Provider validation failed: {error}") from error
    credentials.set(provider, credential, credential_type)
    return credentials.configured()[provider]


@router.post("/metadata/providers/{provider}/test")
async def test_provider(
    provider: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        if provider == "tmdb":
            credential = {"value": str(data.get("credential") or "")}
            await asyncio.to_thread(
                MetadataService().test,
                provider,
                credential,
                str(data.get("credentialType") or "api_key"),
            )
        elif provider == "tvdb":
            await asyncio.to_thread(
                MetadataService().test,
                provider,
                {
                    "apiKey": str(data.get("apiKey") or ""),
                    "pin": str(data.get("pin") or ""),
                },
            )
        else:
            raise ProviderError("Unsupported provider")
    except ProviderError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True}


@router.get("/libraries")
async def list_libraries(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    values = []
    for library in store.list():
        library["sourceLibraryIds"] = store.sources(library["id"])
        values.append(library)
    return values


@router.post("/libraries", status_code=201)
async def create_library(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        library = store.create(
            str(data.get("name") or ""),
            str(data.get("type") or ""),
            data.get("directory"),
            bool(data.get("watchEnabled", True)),
            int(data.get("scanIntervalMinutes") or 1440),
            data.get("sourceLibraryIds") or [],
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    job = runtime.enqueue(
        library["id"],
        "collection_rebuild" if library["type"] == "collection" else "scan",
    )
    scheduler.refresh_library_definition(library)
    # Watchdog.stop()/join() and recursive scheduling can touch a large media
    # tree.  Keep that synchronous filesystem work away from the ASGI event
    # loop so a library change cannot make every request appear unavailable.
    await asyncio.to_thread(runtime.refresh_watchers)
    library["sourceLibraryIds"] = store.sources(library["id"])
    library["jobId"] = job["id"]
    return library


@router.get("/libraries/{library_id}")
async def get_library(
    library_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    library = store.get(library_id)
    if not library:
        raise HTTPException(404, "Library not found.")
    library["sourceLibraryIds"] = store.sources(library_id)
    library["jobs"] = store.jobs(library_id)
    return library


@router.patch("/libraries/{library_id}")
async def update_library(
    library_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    try:
        library = store.update(library_id, await request.json())
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    runtime.enqueue(
        library_id, "collection_rebuild" if library["type"] == "collection" else "scan"
    )
    scheduler.refresh_library_definition(library)
    await asyncio.to_thread(runtime.refresh_watchers)
    library["sourceLibraryIds"] = store.sources(library_id)
    return library


@router.delete("/libraries/{library_id}", status_code=204)
async def delete_library(
    library_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    if not runtime.terminate_library(library_id):
        raise HTTPException(409, "Library jobs are still stopping; try again shortly.")
    if not store.delete(library_id):
        raise HTTPException(404, "Library not found.")
    scheduler.remove_library_definition(library_id)
    await asyncio.to_thread(runtime.refresh_watchers)


@router.post("/libraries/{library_id}/scan", status_code=202)
async def scan_library(
    library_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    library = store.get(library_id)
    if not library:
        raise HTTPException(404, "Library not found.")
    return runtime.enqueue(
        library_id, "collection_rebuild" if library["type"] == "collection" else "scan"
    )


@router.get("/library-jobs/{job_id}")
async def get_job(
    job_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    job = store.job(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@router.get("/jobs")
async def list_jobs(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    definitions = scheduler.store.definitions()
    values = []
    for definition in definitions:
        recent = scheduler.store.runs(definition["id"], 10)
        if definition["kind"] == "library_scan":
            recent = scheduler.store.library_runs(
                (definition.get("config") or {}).get("libraryId"), 10
            )
        current = recent[0] if recent else None
        values.append(
            {
                **definition,
                "lastState": current["state"] if current else definition["lastState"],
                "lastMessage": (current.get("message") or current.get("error"))
                if current
                else definition["lastMessage"],
                "recentRuns": recent,
            }
        )
    return {"jobs": values}


@router.get("/jobs/{job_id}")
async def get_scheduled_job(
    job_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    definition = scheduler.store.definition(job_id)
    if not definition:
        raise HTTPException(404, "Scheduled job not found.")
    recent = (
        scheduler.store.library_runs(
            (definition.get("config") or {}).get("libraryId"), 50
        )
        if definition["kind"] == "library_scan"
        else scheduler.store.runs(job_id, 50)
    )
    return {**definition, "recentRuns": recent}


@router.patch("/jobs/{job_id}")
async def update_scheduled_job(
    job_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    try:
        return scheduler.store.update_definition(job_id, await request.json())
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/jobs/{job_id}/run", status_code=202)
async def run_scheduled_job(
    job_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    try:
        return scheduler.run_now(job_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/jobs/{job_id}/runs/{run_id}/terminate")
async def terminate_scheduled_job(
    job_id: str,
    run_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    definition = scheduler.store.definition(job_id)
    if not definition:
        raise HTTPException(404, "Scheduled job not found.")
    if definition["kind"] == "library_scan":
        library_id = (definition.get("config") or {}).get("libraryId")
        run = store.job(run_id)
        if not run or run["libraryId"] != library_id:
            raise HTTPException(404, "Task run not found.")
        return runtime.terminate(run_id)
    run = scheduler.terminate(job_id, run_id)
    if not run:
        raise HTTPException(404, "Task run not found.")
    return run


def _list_admin_items_sync(
    library_id: str,
    parent_id: str | None,
    locale: str,
    query: str,
    page: int,
    page_size: int,
):
    if not store.get(library_id):
        raise HTTPException(404, "Library not found.")
    parent_id = parent_id or None
    query = query.strip()
    if query:
        ranked_ids = _rank_library_item_ids(
            store.db, library_id, parent_id, locale, query
        )
        total = len(ranked_ids)
        rows = [
            (entity_id,)
            for entity_id in ranked_ids[(page - 1) * page_size : page * page_size]
        ]
    else:
        params = [library_id, parent_id]
        where = "library_id=? AND parent_id IS ?"
        total = store.db.execute(
            f"SELECT COUNT(*) FROM library_entities WHERE {where}", params
        )[0][0]
        rows = store.db.execute(
            f"SELECT id FROM library_entities WHERE {where} ORDER BY entity_type, relative_path COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        )
    entity_ids = [row[0] for row in rows]
    hydration = {
        "entities": {},
        "providers": {},
        "children": {},
        "metadata": {},
    }
    if entity_ids:
        placeholders = ",".join("?" for _ in entity_ids)
        hydration["entities"] = {
            row[0]: row
            for row in store.db.execute(
                f"SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,disc_number,track_number,match_status,match_confidence,match_method FROM library_entities WHERE id IN ({placeholders})",
                entity_ids,
            )
        }
        provider_rows = store.db.execute(
            f"SELECT entity_id,provider,identifier_type,provider_id FROM entity_provider_ids WHERE entity_id IN ({placeholders}) ORDER BY entity_id,provider",
            entity_ids,
        )
        for entity_id, provider, identifier_type, provider_id in provider_rows:
            entity_type = hydration["entities"].get(entity_id, (None, None, None, ""))[
                3
            ]
            primary = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
            hydration["providers"].setdefault(entity_id, []).append(
                {
                    "provider": provider,
                    "type": identifier_type,
                    "id": provider_id,
                    "primary": provider == primary,
                    "role": "primary" if provider == primary else "secondary",
                }
            )
        for entity_id, values in hydration["providers"].items():
            entity_type = hydration["entities"].get(entity_id, (None, None, None, ""))[
                3
            ]
            primary = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
            values.sort(
                key=lambda value: (value["provider"] != primary, value["provider"])
            )
        child_rows = store.db.execute(
            f"SELECT id,entity_type,relative_path,season_number,episode_number,track_number,parent_id FROM library_entities WHERE parent_id IN ({placeholders}) ORDER BY season_number,episode_number,track_number,relative_path COLLATE NOCASE",
            entity_ids,
        )
        for row in child_rows:
            hydration["children"].setdefault(row[6], []).append(row[:6])
        projection_tables = {
            row[0]
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('catalog_item_projection','catalog_read_model_status')"
            )
        }
        ready = False
        if (
            "catalog_item_projection" in projection_tables
            and "catalog_read_model_status" in projection_tables
        ):
            status = store.db.execute(
                "SELECT state FROM catalog_read_model_status WHERE id=1"
            )
            ready = bool(status and status[0][0] == "ready")
        if ready:
            projection_rows = store.db.execute(
                f"SELECT entity_id,payload FROM catalog_item_projection WHERE locale=? AND entity_id IN ({placeholders})",
                [locale, *entity_ids],
            )
            for entity_id, payload in projection_rows:
                try:
                    value = json.loads(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    hydration["metadata"][(entity_id, locale)] = value
    token = _admin_hydration.set(hydration)
    try:
        items = []
        for row in rows:
            item = _entity(row[0], locale)
            item["metadata"] = _metadata_for(item, locale, False, fallback=False)
            items.append(item)
    finally:
        _admin_hydration.reset(token)
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "query": query,
    }


@router.get("/libraries/{library_id}/items")
async def list_items(
    library_id: str,
    parentId: str | None = Query(None),
    locale: str | None = Query(None),
    query: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    pageSize: int = Query(40, ge=1, le=100),
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    locale = _configured_locale(locale)
    return await asyncio.to_thread(
        _list_admin_items_sync,
        library_id,
        parentId,
        locale,
        query,
        page,
        pageSize,
    )


@router.get("/library-items/{entity_id}")
async def get_item(
    entity_id: str,
    locale: str | None = Query(None),
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    locale = _configured_locale(locale)
    item = _entity(entity_id, locale, include_metadata=True)
    item["metadata"] = await asyncio.to_thread(
        _metadata_for, item, locale, False, False
    )
    return item


@router.post("/library-items/{entity_id}/metadata/refresh")
async def refresh_item_metadata(
    entity_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    return await asyncio.to_thread(_refresh_item_metadata_sync, entity_id)


@router.get("/library-items/{entity_id}/trickplay/{generation}/{sheet_index}.webp")
async def get_trickplay_sheet(
    entity_id: str,
    generation: str,
    sheet_index: int,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    item = _entity(entity_id)
    if item["type"] not in {"movie", "episode"}:
        raise HTTPException(404, "Trickplay sheet not found.")
    path = TrickplayExtractor().sheet_path(entity_id, generation, sheet_index)
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/library-items/{entity_id}/intro-outro")
async def get_intro_outro_inspection(
    entity_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    item = _entity(entity_id)
    if item["type"] != "episode":
        raise HTTPException(
            404, "Intro and outro inspection is only available for episodes."
        )
    return IntroOutroStore(store.db).inspection(entity_id)


@router.get("/library-items/{entity_id}/intro-outro/{kind}.mp3")
async def get_intro_outro_audio_preview(
    entity_id: str,
    kind: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    item = _entity(entity_id)
    if item["type"] != "episode":
        raise HTTPException(404, "Audio previews are only available for episodes.")
    try:
        clip = IntroOutroStore(store.db).preview_clip(entity_id, kind)
        content = await asyncio.to_thread(
            render_audio_preview,
            clip["path"],
            clip["startSeconds"],
            min(30.0, clip["durationSeconds"]),
        )
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    return Response(
        content=content,
        media_type="audio/mpeg",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/library-items/{entity_id}/matches")
async def find_matches(
    entity_id: str,
    query: str | None = Query(None),
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    item = _entity(entity_id)
    search_text = (query or Path(item["relativePath"] or "").stem).strip()
    service = MetadataService()
    providers = {"series": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"]}.get(
        item["type"], []
    )
    matches = []
    for provider in providers:
        try:
            client = service.client(provider)
            if hasattr(client, "search"):
                matches.extend(
                    await asyncio.to_thread(client.search, item["type"], search_text)
                )
        except ProviderError:
            continue
    return {"query": search_text, "matches": matches[:50]}


@router.post("/library-items/{entity_id}/match")
async def set_match(
    entity_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    _entity(entity_id)
    data = await request.json()
    provider = str(data.get("provider") or "").lower()
    provider_id = str(data.get("providerId") or data.get("id") or "").strip()
    if provider not in {"tmdb", "tvdb", "musicbrainz"} or not provider_id:
        raise HTTPException(400, "A provider and provider ID are required.")
    entity_type = _entity(entity_id)["type"]
    identifier_type = (
        "movie"
        if entity_type == "movie"
        else "series"
        if entity_type in {"series", "episode", "season"}
        else entity_type
    )
    store.db.execute("DELETE FROM entity_provider_ids WHERE entity_id=?", (entity_id,))
    store.db.execute(
        "INSERT INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,?)",
        (
            entity_id,
            provider,
            identifier_type,
            provider_id,
            int(PRIMARY_PROVIDER_BY_ENTITY.get(entity_type) == provider),
        ),
    )
    store.db.execute(
        "UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='manual',updated_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), entity_id),
    )
    return _entity(entity_id)


@router.get("/library-items/{entity_id}/image")
async def get_image(
    entity_id: str,
    imageType: str = Query("Primary"),
    locale: str | None = Query(None),
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    locale = _configured_locale(locale)
    if imageType not in IMAGE_TYPES:
        raise HTTPException(
            400,
            f"Unsupported image type '{imageType}'. Expected one of: {', '.join(sorted(IMAGE_TYPES))}",
        )
    item = _entity(entity_id, locale)
    pending = False
    for candidate in _image_entities(item, imageType):
        library = store.get(candidate["libraryId"])
        if not library:
            raise HTTPException(404, "Library not found.")
        local = store.db.execute(
            "SELECT relative_path,quick_fingerprint FROM media_files WHERE entity_id=? AND role='image' ORDER BY relative_path",
            (candidate["id"],),
        )
        if library["directory"]:
            for relative_path, content_hash in local:
                if not _local_image_for_type(relative_path, imageType):
                    continue
                path = Path(library["directory"]) / relative_path
                if path.is_file():
                    cached = LocalArtworkCache(store.db).path(content_hash)
                    if cached and cached.is_file():
                        return FileResponse(
                            cached,
                            media_type="image/webp",
                            headers={"X-ZenStream-Image-State": "ready"},
                        )
                    continue
    # Library previews must never turn a cache miss into provider work. Scans
    # and the administrator-triggered backfill own metadata population.
    image = None
    for candidate in _image_entities(item, imageType):
        metadata = await asyncio.to_thread(
            _metadata_for, candidate, locale, False, False
        )
        if not metadata:
            pending = pending or bool(candidate.get("providerIds"))
            continue
        read_service = MetadataReadService(store.db)
        image = choose_artwork(
            metadata.get("images", []),
            locale,
            imageType,
            metadata.get("originalLanguage"),
            read_service.providers(candidate["type"]),
        )
        if image:
            break
    if not image:
        if pending:
            logger.info(
                "image pending entity_id=%s locale=%s image_type=%s",
                entity_id,
                locale,
                imageType,
            )
            return Response(
                status_code=202,
                headers={"Retry-After": "2", "X-ZenStream-Image-State": "pending"},
            )
        logger.warning(
            "image unavailable because entity has no cached artwork entity_id=%s image_type=%s",
            entity_id,
            imageType,
        )
        return Response(status_code=404, headers={"X-ZenStream-Image-State": "missing"})
    cached_file = store.db.execute(
        "SELECT local_path FROM metadata_images WHERE image_type=? AND image_url=?",
        (imageType, image["url"]),
    )
    for (local_path,) in cached_file:
        if local_path and Path(local_path).is_file():
            return FileResponse(
                local_path,
                media_type="image/webp",
                headers={"X-ZenStream-Image-State": "ready"},
            )
    return Response(
        status_code=202,
        headers={"Retry-After": "2", "X-ZenStream-Image-State": "pending"},
    )
