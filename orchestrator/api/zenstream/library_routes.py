"""Administrator APIs for provider settings and native library previews."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.config import Config
from app.library import LibraryRuntime, LibraryStore, runtime
from app.models.admin import Admin
from app.models.metadata import MetadataCache, MetadataCredentials
from app.providers import MetadataService, ProviderError, choose_image


router = APIRouter(prefix="/api/admin")
store = LibraryStore()
credentials = MetadataCredentials()


def require_admin(username: str | None, token: str | None) -> str:
    if not isinstance(username, str) or not isinstance(token, str) or not Admin(username.strip()).authenticate(token):
        raise HTTPException(403, "Invalid administrator credentials.")
    return username.strip()


def _entity_ids(entity_id: str) -> list[dict]:
    rows = store.db.execute("SELECT provider,identifier_type,provider_id,is_primary FROM entity_provider_ids WHERE entity_id=? ORDER BY is_primary DESC, provider", (entity_id,))
    return [{"provider": row[0], "type": row[1], "id": row[2], "primary": bool(row[3])} for row in rows]


def _entity(entity_id: str, locale: str = "en", include_metadata: bool = False) -> dict:
    rows = store.db.execute("SELECT id,library_id,parent_id,entity_type,relative_path,season_number,episode_number,episode_end_number,disc_number,track_number,match_status,match_confidence,match_method FROM library_entities WHERE id=?", (entity_id,))
    if not rows:
        raise HTTPException(404, "Library item not found.")
    row = rows[0]
    value = {"id": row[0], "libraryId": row[1], "parentId": row[2], "type": row[3], "relativePath": row[4], "seasonNumber": row[5], "episodeNumber": row[6], "episodeEndNumber": row[7], "discNumber": row[8], "trackNumber": row[9], "matchStatus": row[10], "matchConfidence": row[11], "matchMethod": row[12], "providerIds": _entity_ids(entity_id)}
    value["displayName"] = Path(row[4]).name if row[4] else row[3].replace("_", " ").title()
    if include_metadata:
        value["metadata"] = _metadata_for(value, locale)
    return value


def _metadata_for(item: dict, locale: str) -> dict | None:
    service = MetadataService()
    priorities = {"series": ["tvdb", "tmdb"], "episode": ["tvdb", "tmdb"], "season": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"], "artist": ["musicbrainz"], "release": ["musicbrainz"], "track": ["musicbrainz"], "collection": ["tvdb"]}
    merged: dict = {}
    for provider in priorities.get(item["type"], []):
        ids = [value for value in item["providerIds"] if value["provider"] == provider]
        if not ids:
            continue
        cached = service.fetch_fallback(provider, item["type"], ids[0]["id"], locale)
        if cached:
            for key, value in cached.items():
                if key == "images":
                    merged.setdefault("images", []).extend(value or [])
                elif not merged.get(key) and value:
                    merged[key] = value
    return merged or None


@router.get("/metadata/providers")
async def provider_status(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    return credentials.configured()


@router.put("/metadata/providers/{provider}")
async def update_provider(provider: str, request: Request, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
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
        if not value or credential_type not in {"api_key", "read_access_token", "bearer", "v4"}:
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
            MetadataService().test(provider, credential, credential_type)
        except ProviderError as error:
            raise HTTPException(400, f"Provider validation failed: {error}") from error
    credentials.set(provider, credential, credential_type)
    return credentials.configured()[provider]


@router.post("/metadata/providers/{provider}/test")
async def test_provider(provider: str, request: Request, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        if provider == "tmdb":
            credential = {"value": str(data.get("credential") or "")}
            MetadataService().test(provider, credential, str(data.get("credentialType") or "api_key"))
        elif provider == "tvdb":
            MetadataService().test(provider, {"apiKey": str(data.get("apiKey") or ""), "pin": str(data.get("pin") or "")})
        else:
            raise ProviderError("Unsupported provider")
    except ProviderError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True}


@router.get("/libraries")
async def list_libraries(Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    values = []
    for library in store.list():
        library["sourceLibraryIds"] = store.sources(library["id"])
        values.append(library)
    return values


@router.post("/libraries", status_code=201)
async def create_library(request: Request, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        library = store.create(str(data.get("name") or ""), str(data.get("type") or ""), data.get("directory"), bool(data.get("watchEnabled", True)), int(data.get("scanIntervalMinutes") or 1440), data.get("sourceLibraryIds") or [])
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    job = runtime.enqueue(library["id"], "collection_rebuild" if library["type"] == "collection" else "scan")
    runtime.refresh_watchers()
    library["sourceLibraryIds"] = store.sources(library["id"])
    library["jobId"] = job["id"]
    return library


@router.get("/libraries/{library_id}")
async def get_library(library_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    library = store.get(library_id)
    if not library:
        raise HTTPException(404, "Library not found.")
    library["sourceLibraryIds"] = store.sources(library_id)
    library["jobs"] = store.jobs(library_id)
    return library


@router.patch("/libraries/{library_id}")
async def update_library(library_id: str, request: Request, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    try:
        library = store.update(library_id, await request.json())
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    runtime.enqueue(library_id, "collection_rebuild" if library["type"] == "collection" else "scan")
    runtime.refresh_watchers()
    library["sourceLibraryIds"] = store.sources(library_id)
    return library


@router.delete("/libraries/{library_id}", status_code=204)
async def delete_library(library_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    if not store.delete(library_id):
        raise HTTPException(404, "Library not found.")
    runtime.refresh_watchers()


@router.post("/libraries/{library_id}/scan", status_code=202)
async def scan_library(library_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    library = store.get(library_id)
    if not library:
        raise HTTPException(404, "Library not found.")
    return runtime.enqueue(library_id, "collection_rebuild" if library["type"] == "collection" else "scan")


@router.get("/library-jobs/{job_id}")
async def get_job(job_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    job = store.job(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@router.get("/libraries/{library_id}/items")
async def list_items(library_id: str, parentId: str | None = Query(None), locale: str = Query("en"), page: int = Query(1, ge=1), pageSize: int = Query(40, ge=1, le=100), Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    if not store.get(library_id):
        raise HTTPException(404, "Library not found.")
    parentId = parentId or None
    params = [library_id, parentId]
    where = "library_id=? AND parent_id IS ?"
    total = store.db.execute(f"SELECT COUNT(*) FROM library_entities WHERE {where}", params)[0][0]
    rows = store.db.execute(f"SELECT id FROM library_entities WHERE {where} ORDER BY entity_type, relative_path COLLATE NOCASE LIMIT ? OFFSET ?", params + [pageSize, (page - 1) * pageSize])
    items = [_entity(row[0], locale) for row in rows]
    return {"items": items, "page": page, "pageSize": pageSize, "total": total}


@router.get("/library-items/{entity_id}")
async def get_item(entity_id: str, locale: str = Query("en"), Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    return _entity(entity_id, locale, include_metadata=True)


@router.post("/library-items/hydrate")
async def hydrate_items(request: Request, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    data = await request.json()
    locale = str(data.get("locale") or "en")
    values = []
    for entity_id in data.get("entityIds") or []:
        item = _entity(str(entity_id), locale)
        try:
            payload = _metadata_for(item, locale)
            if payload:
                values.append({"entityId": entity_id, "metadata": payload})
        except ProviderError:
            continue
    return {"items": values}


@router.get("/library-items/{entity_id}/matches")
async def find_matches(entity_id: str, query: str | None = Query(None), Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    item = _entity(entity_id)
    search_text = (query or Path(item["relativePath"] or "").stem).strip()
    service = MetadataService()
    providers = {"series": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"]}.get(item["type"], [])
    matches = []
    for provider in providers:
        try:
            client = service.client(provider)
            if hasattr(client, "search"):
                matches.extend(client.search(item["type"], search_text))
        except ProviderError:
            continue
    return {"query": search_text, "matches": matches[:50]}


@router.post("/library-items/{entity_id}/match")
async def set_match(entity_id: str, request: Request, Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    _entity(entity_id)
    data = await request.json()
    provider = str(data.get("provider") or "").lower()
    provider_id = str(data.get("providerId") or data.get("id") or "").strip()
    if provider not in {"tmdb", "tvdb", "musicbrainz"} or not provider_id:
        raise HTTPException(400, "A provider and provider ID are required.")
    entity_type = _entity(entity_id)["type"]
    identifier_type = "movie" if entity_type == "movie" else "series" if entity_type in {"series", "episode", "season"} else entity_type
    store.db.execute("DELETE FROM entity_provider_ids WHERE entity_id=?", (entity_id,))
    store.db.execute("INSERT INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,1)", (entity_id, provider, identifier_type, provider_id))
    store.db.execute("UPDATE library_entities SET match_status='matched',match_confidence=1.0,match_method='manual',updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), entity_id))
    return _entity(entity_id)


@router.get("/library-items/{entity_id}/image")
async def get_image(entity_id: str, imageType: str = Query("poster"), locale: str = Query("en"), Username: str | None = Header(None), TOKEN: str | None = Header(None)):
    require_admin(Username, TOKEN)
    item = _entity(entity_id, locale)
    library = store.get(item["libraryId"])
    if not library:
        raise HTTPException(404, "Library not found.")
    local = store.db.execute("SELECT relative_path FROM media_files WHERE entity_id=? AND role='image' ORDER BY relative_path LIMIT 1", (entity_id,))
    if local and library["directory"]:
        path = Path(library["directory"]) / local[0][0]
        if path.is_file():
            return FileResponse(path)
    metadata = _metadata_for(item, locale)
    if not metadata:
        raise HTTPException(404, "Image metadata is not available yet.")
    image = choose_image(metadata.get("images", []), locale, imageType)
    if not image:
        raise HTTPException(404, "Image not found.")
    cache_root = Path(store.db.db_file).parent / "metadata-cache" / "images"
    cache_root.mkdir(parents=True, exist_ok=True)
    extension = Path(image["url"].split("?", 1)[0]).suffix.lower() or ".jpg"
    target = cache_root / f"{hashlib.sha256(image['url'].encode()).hexdigest()}{extension}"
    if not target.is_file():
        try:
            response = httpx.get(image["url"], timeout=20, follow_redirects=True)
            response.raise_for_status()
            target.write_bytes(response.content)
        except (httpx.HTTPError, OSError) as error:
            raise HTTPException(502, "Provider image could not be downloaded.") from error
    selected_provider = image.get("provider") or next((value["provider"] for value in item["providerIds"] if value["provider"] in {"tmdb", "tvdb"}), "provider")
    selected_id = next((value["id"] for value in item["providerIds"] if value["provider"] == selected_provider), "")
    cached_file = store.db.execute("SELECT local_path FROM metadata_images WHERE provider=? AND entity_type=? AND provider_id=? AND image_type=? AND image_url=?", (selected_provider, item["type"], selected_id, imageType, image["url"]))
    if cached_file and cached_file[0][0] and Path(cached_file[0][0]).is_file():
        return FileResponse(cached_file[0][0])
    MetadataCache().put_image(selected_provider, item["type"], selected_id, image.get("language"), imageType, image["url"], str(target))
    return FileResponse(target)
