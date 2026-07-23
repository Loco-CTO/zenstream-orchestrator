from __future__ import annotations

import asyncio
import json
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.config import Config
from app.library import LibraryRuntime, LibraryStore, runtime
from app.jobs import scheduler
from app.models.admin import Admin
from app.models.metadata import (
    IMAGE_LANGUAGE_SCHEMA,
    MetadataCredentials,
    MetadataLanguageSettings,
    normalize_metadata_locale,
)
from app.metadata_domain import choose_artwork
from app.metadata_services import MetadataReadService
from app.providers import (
    IMAGE_TYPES,
    PRIMARY_PROVIDER_BY_ENTITY,
    MetadataService,
    ProviderError,
)
from app.logging_config import get_logger


router = APIRouter(prefix="/api/admin")
logger = get_logger("library_api")
store = LibraryStore()
credentials = MetadataCredentials()


def require_admin(username: str | None, token: str | None) -> str:
    if (
        not isinstance(username, str)
        or not isinstance(token, str)
        or not Admin(username.strip()).authenticate(token)
    ):
        raise HTTPException(403, "Invalid administrator credentials.")
    return username.strip()


def _configured_locale(value: str | None) -> str:
    configured = MetadataLanguageSettings().get()
    requested = normalize_metadata_locale(value) if value else configured[0]
    if requested not in configured:
        raise HTTPException(400, "Metadata language is not configured.")
    return requested


def _entity_ids(entity_id: str) -> list[dict]:
    entity_rows = store.db.execute(
        "SELECT entity_type FROM library_entities WHERE id=?", (entity_id,)
    )
    entity_type = entity_rows[0][0] if entity_rows else ""
    primary_provider = PRIMARY_PROVIDER_BY_ENTITY.get(entity_type)
    rows = store.db.execute(
        "SELECT provider,identifier_type,provider_id,is_primary FROM entity_provider_ids WHERE entity_id=? ORDER BY CASE WHEN provider=? THEN 0 ELSE 1 END, provider",
        (entity_id, primary_provider),
    )
    return [
        {
            "provider": row[0],
            "type": row[1],
            "id": row[2],
            "primary": row[0] == primary_provider,
            "role": "primary" if row[0] == primary_provider else "secondary",
        }
        for row in rows
    ]


def _entity(entity_id: str, locale: str = "en", include_metadata: bool = False) -> dict:
    rows = store.db.execute(
        "SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,disc_number,track_number,match_status,match_confidence,match_method FROM library_entities WHERE id=?",
        (entity_id,),
    )
    if not rows:
        raise HTTPException(404, "Library item not found.")
    row = rows[0]
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
    children = store.db.execute(
        "SELECT id,entity_type,relative_path,season_number,episode_number,track_number FROM library_entities WHERE parent_id=? ORDER BY season_number,episode_number,track_number,relative_path COLLATE NOCASE",
        (entity_id,),
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


def _metadata_for(
    item: dict, locale: str, fetch: bool = False, fallback: bool = True
) -> dict | None:
    del fetch, fallback
    requested = normalize_metadata_locale(locale)
    if requested not in MetadataLanguageSettings().get():
        raise HTTPException(400, "Metadata language is not configured.")
    value = MetadataReadService(store.db).resolve_raw(
        item["type"], item.get("providerIds", []), requested
    )
    return value or None


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


def _search_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )


def _trigrams(value: str) -> set[str]:
    padded = f"  {value} "
    return {padded[index : index + 3] for index in range(max(1, len(padded) - 2))}


def _trigram_score(query: str, candidate: str) -> float:
    query = _search_text(query)
    candidate = _search_text(candidate)
    if not query or not candidate:
        return 0.0
    query_grams = _trigrams(query)
    candidate_grams = _trigrams(candidate)
    score = (2 * len(query_grams & candidate_grams)) / (
        len(query_grams) + len(candidate_grams)
    )
    if query == candidate:
        return 1.0
    if candidate.startswith(query):
        return max(score, 0.96)
    if query in candidate:
        return max(score, 0.9)
    return score


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
    normalized_query = _search_text(query)
    if not normalized_query:
        return []
    threshold = 0.18 if len(normalized_query) <= 4 else 0.24
    ranked = []
    for entity_id, candidate in candidates.items():
        path = candidate["path"]
        values = [path, Path(path).stem, *candidate["values"]]
        score = max(
            (_trigram_score(normalized_query, value) for value in values), default=0.0
        )
        if score >= threshold:
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
    runtime.refresh_watchers()
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
    runtime.refresh_watchers()
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
    runtime.refresh_watchers()


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
    if not store.get(library_id):
        raise HTTPException(404, "Library not found.")
    parentId = parentId or None
    query = query.strip()
    if query:
        ranked_ids = _rank_library_item_ids(
            store.db, library_id, parentId, locale, query
        )
        total = len(ranked_ids)
        rows = [
            (entity_id,)
            for entity_id in ranked_ids[(page - 1) * pageSize : page * pageSize]
        ]
    else:
        params = [library_id, parentId]
        where = "library_id=? AND parent_id IS ?"
        total = store.db.execute(
            f"SELECT COUNT(*) FROM library_entities WHERE {where}", params
        )[0][0]
        rows = store.db.execute(
            f"SELECT id FROM library_entities WHERE {where} ORDER BY entity_type, relative_path COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [pageSize, (page - 1) * pageSize],
        )
    items = []
    for row in rows:
        item = _entity(row[0], locale)
        item["metadata"] = _metadata_for(item, locale, False, fallback=False)
        items.append(item)
    return {
        "items": items,
        "page": page,
        "pageSize": pageSize,
        "total": total,
        "query": query,
    }


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
    library = store.get(item["libraryId"])
    if not library:
        raise HTTPException(404, "Library not found.")
    local = store.db.execute(
        "SELECT relative_path FROM media_files WHERE entity_id=? AND role='image' ORDER BY relative_path",
        (entity_id,),
    )
    if library["directory"]:
        for (relative_path,) in local:
            if not _local_image_for_type(relative_path, imageType):
                continue
            path = Path(library["directory"]) / relative_path
            if path.is_file():
                return FileResponse(path)
    # Library previews must never turn a cache miss into provider work. Scans
    # and the administrator-triggered backfill own metadata population.
    metadata = await asyncio.to_thread(_metadata_for, item, locale, False, False)
    if not metadata:
        if item.get("providerIds"):
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
        else:
            logger.warning(
                "image unavailable because entity has no provider IDs entity_id=%s image_type=%s",
                entity_id,
                imageType,
            )
            return Response(
                status_code=404, headers={"X-ZenStream-Image-State": "error"}
            )
    read_service = MetadataReadService(store.db)
    image = choose_artwork(
        metadata.get("images", []),
        locale,
        imageType,
        metadata.get("originalLanguage"),
        read_service.providers(item["type"]),
    )
    if not image:
        return Response(status_code=404, headers={"X-ZenStream-Image-State": "missing"})
    cached_file = store.db.execute(
        "SELECT local_path FROM metadata_images WHERE image_type=? AND image_url=?",
        (imageType, image["url"]),
    )
    for (local_path,) in cached_file:
        if local_path and Path(local_path).is_file():
            return FileResponse(
                local_path, headers={"X-ZenStream-Image-State": "ready"}
            )
    return Response(
        status_code=202,
        headers={"Retry-After": "2", "X-ZenStream-Image-State": "pending"},
    )
