from __future__ import annotations

import contextvars
import json
from datetime import datetime, timezone
from pathlib import Path

from app.client_auth import administrator_origin_allowed
from app.foreground import run_auth, run_control, run_foreground
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
WATCHER_TASK_PREFIX = "library_watch:"
FULL_LIBRARY_RUN_KINDS = {"scan", "collection_rebuild"}
credentials = MetadataCredentials()
_admin_hydration: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "admin_catalog_hydration", default=None
)


def _watcher_task_id(library_id: str) -> str:
    return f"{WATCHER_TASK_PREFIX}{library_id}"


def _watcher_task(library: dict, limit: int = 10) -> dict:
    recent = scheduler.store.library_runs(library["id"], limit, kinds={"reconcile"})
    current = next(
        (run for run in recent if run["state"] in {"queued", "running", "terminating"}),
        None,
    )
    latest = recent[0] if recent else None
    return {
        "id": _watcher_task_id(library["id"]),
        "key": _watcher_task_id(library["id"]),
        "name": f"Watch {library['name']}",
        "description": "Automatic filesystem watcher reconciliation history.",
        "kind": "library_watch",
        "nextRunAt": None,
        "lastRunAt": (
            (latest.get("finishedAt") or latest.get("createdAt")) if latest else None
        ),
        "lastState": current["state"]
        if current
        else (latest["state"] if latest else "idle"),
        "lastMessage": (
            (latest.get("message") or latest.get("error")) if latest else None
        ),
        "config": {"libraryId": library["id"]},
        "triggers": [],
        "recentRuns": recent,
        "historyOnly": True,
    }


def _is_watcher_task_id(job_id: str) -> bool:
    return job_id.startswith(WATCHER_TASK_PREFIX)


def _require_mutable_task(job_id: str) -> None:
    if _is_watcher_task_id(job_id):
        raise HTTPException(
            409, "Watcher history tasks cannot be configured or started."
        )


def _full_scan_summary(definition: dict, recent: list[dict]) -> dict:
    """Project a library-scan definition from full-lane runs only."""
    current = next(
        (run for run in recent if run["state"] in {"queued", "running", "terminating"}),
        None,
    )
    latest = recent[0] if recent else None
    return {
        "lastRunAt": (
            (latest.get("finishedAt") or latest.get("createdAt")) if latest else None
        ),
        "lastRunId": latest.get("id") if latest else None,
        "lastState": current["state"]
        if current
        else (latest["state"] if latest else "idle"),
        "lastMessage": (
            (current.get("message") or current.get("error"))
            if current
            else ((latest.get("message") or latest.get("error")) if latest else None)
        ),
    }


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
        raise HTTPException(401, "Administrator cookie authentication is required.")
    if request.method in {
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    } and not administrator_origin_allowed(request):
        raise HTTPException(403, "Cross-site administrator request rejected.")
    admin = Admin.from_token(cookie_token)
    if admin is None:
        raise HTTPException(401, "Invalid administrator credentials.")
    return admin.username


async def _admin_boundary(request: Request):
    identity = await run_auth(authenticate_admin_request, request)
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


async def _configured_locale_async(value: str | None) -> str:
    return await run_control(_configured_locale, value)


def _list_libraries_sync():
    values = []
    for library in store.list():
        library["sourceLibraryIds"] = store.sources(library["id"])
        values.append(library)
    return values


def _create_library_sync(values: dict):
    library = store.create(
        str(values.get("name") or ""),
        str(values.get("type") or ""),
        values.get("directory"),
        bool(values.get("watchEnabled", True)),
        int(values.get("scanIntervalMinutes") or 1440),
        values.get("sourceLibraryIds") or [],
        values.get("sortOrder"),
    )
    scheduler.refresh_library_definition(library)
    runtime.refresh_watchers()
    job = runtime.enqueue(
        library["id"],
        "collection_rebuild" if library["type"] == "collection" else "scan",
    )
    library["sourceLibraryIds"] = store.sources(library["id"])
    library["jobId"] = job["id"]
    return library


def _get_library_sync(library_id: str):
    library = store.get(library_id)
    if not library:
        raise HTTPException(404, "Library not found.")
    library["sourceLibraryIds"] = store.sources(library_id)
    library["jobs"] = store.jobs(library_id)
    return library


def _update_library_sync(library_id: str, values: dict):
    library = store.update(library_id, values)
    if set(values) != {"sortOrder"}:
        runtime.enqueue(
            library_id,
            "collection_rebuild" if library["type"] == "collection" else "scan",
        )
    scheduler.refresh_library_definition(library)
    runtime.refresh_watchers()
    library["sourceLibraryIds"] = store.sources(library_id)
    return library


def _delete_library_sync(library_id: str):
    if not runtime.terminate_library(library_id):
        raise HTTPException(409, "Library jobs are still stopping; try again shortly.")
    if not store.delete(library_id):
        raise HTTPException(404, "Library not found.")
    scheduler.remove_library_definition(library_id)
    runtime.refresh_watchers()


def _scan_library_sync(library_id: str):
    library = store.get(library_id)
    if not library:
        raise HTTPException(404, "Library not found.")
    return runtime.enqueue(
        library_id,
        "collection_rebuild" if library["type"] == "collection" else "scan",
    )


def _move_library_sync(library_id: str, direction: str):
    library = store.move(library_id, direction)
    library["sourceLibraryIds"] = store.sources(library_id)
    return library


def _list_jobs_sync():
    definitions = scheduler.store.definitions()
    values = []
    for definition in definitions:
        recent = scheduler.store.runs(definition["id"], 10)
        summary = None
        if definition["kind"] == "library_scan":
            recent = scheduler.store.library_runs(
                (definition.get("config") or {}).get("libraryId"),
                10,
                kinds=FULL_LIBRARY_RUN_KINDS,
            )
            summary = _full_scan_summary(definition, recent)
        current = next(
            (
                run
                for run in recent
                if run["state"] in {"queued", "running", "terminating"}
            ),
            None,
        )
        values.append(
            {
                **definition,
                "historyOnly": False,
                **(
                    summary
                    or {
                        "lastState": current["state"]
                        if current
                        else definition["lastState"],
                        "lastMessage": (current.get("message") or current.get("error"))
                        if current
                        else definition["lastMessage"],
                    }
                ),
                "recentRuns": recent,
            }
        )
    for library in store.list():
        if library["type"] != "collection":
            values.append(_watcher_task(library, 10))
    return {"jobs": values}


def _get_scheduled_job_sync(job_id: str):
    if _is_watcher_task_id(job_id):
        library_id = job_id.removeprefix(WATCHER_TASK_PREFIX)
        library = store.get(library_id)
        if not library or library["type"] == "collection":
            raise HTTPException(404, "Watcher task not found.")
        return _watcher_task(library, 50)
    definition = scheduler.store.definition(job_id)
    if not definition:
        raise HTTPException(404, "Scheduled job not found.")
    recent = (
        scheduler.store.library_runs(
            (definition.get("config") or {}).get("libraryId"),
            50,
            kinds=FULL_LIBRARY_RUN_KINDS,
        )
        if definition["kind"] == "library_scan"
        else scheduler.store.runs(job_id, 50)
    )
    summary = (
        _full_scan_summary(definition, recent)
        if definition["kind"] == "library_scan"
        else {}
    )
    return {**definition, **summary, "recentRuns": recent, "historyOnly": False}


def _update_scheduled_job_sync(job_id: str, values: dict):
    _require_mutable_task(job_id)
    return scheduler.store.update_definition(job_id, values)


def _add_scheduled_trigger_sync(job_id: str, values: dict):
    _require_mutable_task(job_id)
    return scheduler.store.add_trigger(job_id, values)


def _remove_scheduled_trigger_sync(job_id: str, trigger_id: str):
    _require_mutable_task(job_id)
    return scheduler.store.remove_trigger(job_id, trigger_id)


def _run_scheduled_job_sync(job_id: str, options):
    _require_mutable_task(job_id)
    return scheduler.run_now(job_id, options)


def _terminate_scheduled_job_sync(job_id: str, run_id: str):
    _require_mutable_task(job_id)
    definition = scheduler.store.definition(job_id)
    if not definition:
        raise HTTPException(404, "Scheduled job not found.")
    if definition["kind"] == "library_scan":
        library_id = (definition.get("config") or {}).get("libraryId")
        run = store.job(run_id)
        if (
            not run
            or run["libraryId"] != library_id
            or run["kind"] not in FULL_LIBRARY_RUN_KINDS
        ):
            raise HTTPException(404, "Task run not found.")
        return runtime.terminate(run_id)
    run = scheduler.terminate(job_id, run_id)
    if not run:
        raise HTTPException(404, "Task run not found.")
    return run


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
    if hydration is not None:
        value["revision"] = hydration.get("revisions", {}).get(entity_id, "")
    else:
        value["revision"] = _entity_revision(entity_id, locale)
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
    if not identities and item["type"] not in {"movie", "episode"}:
        raise HTTPException(409, "This item has no supported provider identity.")
    service = MetadataService()
    ingest = MetadataIngestService(service, background_assets=False)
    locales = ingest.locales()
    refreshed = []
    failures = []
    processed: set[tuple[str, str]] = set()
    index = 0
    while index < len(identities):
        identity = identities[index]
        index += 1
        provider = str(identity["provider"])
        provider_id = str(identity["id"])
        key = (provider, provider_id)
        if key in processed:
            continue
        processed.add(key)
        try:
            documents = ingest.ingest_locales(
                provider,
                item["type"],
                provider_id,
                locales,
                force=True,
            )
            refreshed.append(provider)
            if item["type"] == "series" and provider == "tvdb":
                # TVDB is the authoritative root provider.  Its normalized
                # remote IDs may add TMDB as an optional secondary source;
                # retain the link even when a later TMDB fetch is unavailable.
                for document in documents.values():
                    for linked in document.get("ids", []) or []:
                        if linked.get("provider") == "tmdb" and linked.get("id"):
                            store.db.execute(
                                "INSERT OR IGNORE INTO entity_provider_ids(entity_id,provider,identifier_type,provider_id,is_primary) VALUES(?,?,?,?,0)",
                                (
                                    entity_id,
                                    "tmdb",
                                    "series",
                                    str(linked["id"]),
                                ),
                            )
                            identities.append(
                                {
                                    "provider": "tmdb",
                                    "id": str(linked["id"]),
                                }
                            )
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
    if item["type"] in {"movie", "episode"}:
        try:
            from app.screen_extractor import extract_entity

            extract_entity(store.db, entity_id, item["type"], force=False)
        except Exception as error:
            failures.append(
                {
                    "provider": "screen_extractor",
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
    return await run_control(credentials.configured)


@router.get("/metadata/languages")
async def metadata_languages(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    return {
        **(await run_control(MetadataLanguageSettings().get_settings)),
        "options": await run_foreground(MetadataService().language_options),
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
        settings = MetadataLanguageSettings()
        if "preferNoLanguageForBackdrop" in data:
            saved = await run_control(
                settings.update,
                data.get("locales"),
                data["preferNoLanguageForBackdrop"],
            )
        else:
            saved = await run_control(settings.update, data.get("locales"))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    run = await run_control(scheduler.enqueue_metadata_refresh)
    return {**saved, "backfill": run}


@router.post("/metadata/refresh")
async def refresh_metadata(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    """Explicitly repair/refetch all indexed provider metadata and artwork."""
    require_admin(Username, TOKEN)
    return {"backfill": await run_control(scheduler.enqueue_metadata_refresh)}


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

        def clear_provider():
            credentials.clear(provider)
            return credentials.configured()[provider]

        return await run_control(clear_provider)
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
            await run_foreground(
                MetadataService().test, provider, credential, credential_type
            )
        except ProviderError as error:
            raise HTTPException(400, f"Provider validation failed: {error}") from error

    def save_provider():
        credentials.set(provider, credential, credential_type)
        return credentials.configured()[provider]

    return await run_control(save_provider)


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
            await run_foreground(
                MetadataService().test,
                provider,
                credential,
                str(data.get("credentialType") or "api_key"),
            )
        elif provider == "tvdb":
            await run_foreground(
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
    return await run_control(_list_libraries_sync)


@router.post("/libraries", status_code=201)
async def create_library(
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    data = await request.json()
    try:
        library = await run_control(_create_library_sync, data)
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    return library


@router.get("/libraries/{library_id}")
async def get_library(
    library_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    return await run_control(_get_library_sync, library_id)


@router.patch("/libraries/{library_id}")
async def update_library(
    library_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    try:
        library = await run_control(
            _update_library_sync, library_id, await request.json()
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    return library


@router.delete("/libraries/{library_id}", status_code=204)
async def delete_library(
    library_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    await run_control(_delete_library_sync, library_id)


@router.post("/libraries/{library_id}/scan", status_code=202)
async def scan_library(
    library_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    return await run_control(_scan_library_sync, library_id)


@router.post("/libraries/{library_id}/move")
async def move_library(
    library_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    try:
        values = await request.json()
        return await run_control(
            _move_library_sync, library_id, str(values.get("direction") or "")
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error


@router.get("/library-jobs/{job_id}")
async def get_job(
    job_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    job = await run_control(store.job, job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@router.get("/jobs")
async def list_jobs(
    Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    return await run_control(_list_jobs_sync)


@router.get("/jobs/{job_id}")
async def get_scheduled_job(
    job_id: str, Username: str | None = Header(None), TOKEN: str | None = Header(None)
):
    require_admin(Username, TOKEN)
    return await run_control(_get_scheduled_job_sync, job_id)


@router.patch("/jobs/{job_id}")
async def update_scheduled_job(
    job_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    _require_mutable_task(job_id)
    try:
        return await run_control(
            _update_scheduled_job_sync, job_id, await request.json()
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/jobs/{job_id}/triggers")
async def add_scheduled_trigger(
    job_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    _require_mutable_task(job_id)
    try:
        return await run_control(
            _add_scheduled_trigger_sync, job_id, await request.json()
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/jobs/{job_id}/triggers/{trigger_id}")
async def remove_scheduled_trigger(
    job_id: str,
    trigger_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    _require_mutable_task(job_id)
    try:
        return await run_control(_remove_scheduled_trigger_sync, job_id, trigger_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/jobs/{job_id}/run", status_code=202)
async def run_scheduled_job(
    job_id: str,
    request: Request,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    _require_mutable_task(job_id)
    try:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return await run_control(
            _run_scheduled_job_sync, job_id, (payload or {}).get("options")
        )
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
    return await run_control(_terminate_scheduled_job_sync, job_id, run_id)


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
    catalog_generation = _catalog_generation(library_id)
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
        "revisions": {},
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
        tables = {
            row[0]
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('catalog_item_projection','catalog_artwork_selection')"
            )
        }
        projection_expr = (
            "COALESCE((SELECT generation FROM catalog_item_projection p WHERE p.entity_id=e.id AND p.locale=?),0),"
            "COALESCE((SELECT updated_at FROM catalog_item_projection p WHERE p.entity_id=e.id AND p.locale=?),'')"
            if "catalog_item_projection" in tables
            else "0,''"
        )
        artwork_expr = (
            "COALESCE((SELECT GROUP_CONCAT(version, '|') FROM catalog_artwork_selection a WHERE a.entity_id=e.id AND a.locale=?),'') ,"
            "COALESCE((SELECT MAX(updated_at) FROM catalog_artwork_selection a WHERE a.entity_id=e.id AND a.locale=?),'')"
            if "catalog_artwork_selection" in tables
            else "'',''"
        )
        revision_sql = (
            f"SELECT e.id,e.updated_at,{projection_expr},{artwork_expr} "
            f"FROM library_entities e WHERE e.id IN ({placeholders})"
        )
        revision_params = []
        if "catalog_item_projection" in tables:
            revision_params.extend([locale, locale])
        if "catalog_artwork_selection" in tables:
            revision_params.extend([locale, locale])
        revision_params.extend(entity_ids)
        try:
            revision_rows = store.db.execute(revision_sql, revision_params)
        except Exception:
            # Older/minimal test databases may have the projection table before
            # its version columns were introduced; entity.updated_at remains a
            # valid conservative revision in that case.
            revision_rows = [
                (
                    entity_id,
                    (
                        store.db.execute(
                            "SELECT updated_at FROM library_entities WHERE id=?",
                            (entity_id,),
                        )
                        or [[""]]
                    )[0][0],
                    0,
                    "",
                    "",
                    "",
                )
                for entity_id in hydration["entities"]
            ]
        for revision_row in revision_rows:
            hydration["revisions"][revision_row[0]] = ":".join(
                str(value or "") for value in revision_row[1:]
            )
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
        "catalogGeneration": catalog_generation,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "query": query,
    }


def _get_item_sync(entity_id: str, locale: str) -> dict:
    item = _entity(entity_id, locale, include_metadata=True)
    item["metadata"] = _metadata_for(item, locale, False, False)
    return item


def _get_trickplay_sheet_sync(entity_id: str, generation: str, sheet_index: int):
    item = _entity(entity_id)
    if item["type"] not in {"movie", "episode"}:
        raise HTTPException(404, "Trickplay sheet not found.")
    return TrickplayExtractor().sheet_path(entity_id, generation, sheet_index)


def _get_intro_outro_inspection_sync(entity_id: str):
    item = _entity(entity_id)
    if item["type"] != "episode":
        raise HTTPException(
            404, "Intro and outro inspection is only available for episodes."
        )
    return IntroOutroStore(store.db).inspection(entity_id)


def _get_intro_outro_clip_sync(entity_id: str, kind: str):
    item = _entity(entity_id)
    if item["type"] != "episode":
        raise HTTPException(404, "Audio previews are only available for episodes.")
    return IntroOutroStore(store.db).preview_clip(entity_id, kind)


def _find_match_item_sync(entity_id: str):
    item = _entity(entity_id)
    return item, (Path(item["relativePath"] or "").stem).strip()


def _set_match_sync(entity_id: str, provider: str, provider_id: str):
    item = _entity(entity_id)
    entity_type = item["type"]
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


def _catalog_status_sync(library_id: str):
    if not store.get(library_id):
        raise HTTPException(404, "Library not found.")
    has_summary = bool(
        store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_library_summary'"
        )
    )
    generation_select = "COALESCE(s.generation,0)" if has_summary else "0"
    summary_join = (
        "LEFT JOIN catalog_library_summary s ON s.library_id=l.id"
        if has_summary
        else ""
    )
    rows = store.db.execute(
        f"""
        SELECT {generation_select}, l.scan_state,
            EXISTS(SELECT 1 FROM library_jobs j WHERE j.library_id=l.id AND j.kind IN ('scan','collection_rebuild') AND j.state IN ('queued','running','terminating')),
            EXISTS(SELECT 1 FROM library_jobs j WHERE j.library_id=l.id AND j.kind='reconcile' AND j.state IN ('queued','running','terminating'))
        FROM libraries l {summary_join} WHERE l.id=?
        """,
        (library_id,),
    )
    if not rows:
        raise HTTPException(404, "Library not found.")
    return {
        "catalogGeneration": int(rows[0][0] or 0),
        "scanState": rows[0][1],
        "activeScan": bool(rows[0][2]),
        "activeReconcile": bool(rows[0][3]),
    }


def _entity_revision(entity_id: str, locale: str) -> str:
    """Return a stable card revision from inventory, projection, and artwork."""
    tables = {
        row[0]
        for row in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('catalog_item_projection','catalog_artwork_selection')"
        )
    }
    row = store.db.execute(
        "SELECT updated_at FROM library_entities WHERE id=?", (entity_id,)
    )
    if not row:
        return ""
    values: list[object] = [row[0][0]]
    if "catalog_item_projection" in tables:
        try:
            projection = store.db.execute(
                "SELECT generation,updated_at FROM catalog_item_projection WHERE entity_id=? AND locale=?",
                (entity_id, locale),
            )
            values.extend(projection[0] if projection else (0, ""))
        except Exception:
            values.extend((0, ""))
    else:
        values.extend((0, ""))
    if "catalog_artwork_selection" in tables:
        try:
            artwork = store.db.execute(
                "SELECT GROUP_CONCAT(version,'|'),MAX(updated_at) FROM catalog_artwork_selection WHERE entity_id=? AND locale=?",
                (entity_id, locale),
            )
            values.extend(artwork[0] if artwork else ("", ""))
        except Exception:
            values.extend(("", ""))
    else:
        values.extend(("", ""))
    return ":".join(str(value or "") for value in values)


def _catalog_generation(library_id: str) -> int:
    tables = {
        row[0]
        for row in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_library_summary'"
        )
    }
    if not tables:
        return 0
    rows = store.db.execute(
        "SELECT generation FROM catalog_library_summary WHERE library_id=?",
        (library_id,),
    )
    return int(rows[0][0]) if rows else 0


@router.get("/libraries/{library_id}/catalog-status")
async def catalog_status(
    library_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    return await run_control(_catalog_status_sync, library_id)


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
    locale = await _configured_locale_async(locale)
    return await run_control(
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
    locale = await _configured_locale_async(locale)
    return await run_control(_get_item_sync, entity_id, locale)


@router.post("/library-items/{entity_id}/metadata/refresh")
async def refresh_item_metadata(
    entity_id: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    return await run_control(_refresh_item_metadata_sync, entity_id)


@router.get("/library-items/{entity_id}/trickplay/{generation}/{sheet_index}.webp")
async def get_trickplay_sheet(
    entity_id: str,
    generation: str,
    sheet_index: int,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    path = await run_control(
        _get_trickplay_sheet_sync, entity_id, generation, sheet_index
    )
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
    return await run_control(_get_intro_outro_inspection_sync, entity_id)


@router.get("/library-items/{entity_id}/intro-outro/{kind}.mp3")
async def get_intro_outro_audio_preview(
    entity_id: str,
    kind: str,
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    try:
        clip = await run_control(_get_intro_outro_clip_sync, entity_id, kind)
        content = await run_foreground(
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
    item, default_search_text = await run_control(_find_match_item_sync, entity_id)
    search_text = (query or default_search_text).strip()
    providers = {"series": ["tvdb", "tmdb"], "movie": ["tmdb", "tvdb"]}.get(
        item["type"], []
    )
    matches = []
    for provider in providers:
        try:

            def search_provider():
                client = MetadataService().client(provider)
                return (
                    client.search(item["type"], search_text)
                    if hasattr(client, "search")
                    else []
                )

            matches.extend(await run_foreground(search_provider))
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
    data = await request.json()
    provider = str(data.get("provider") or "").lower()
    provider_id = str(data.get("providerId") or data.get("id") or "").strip()
    if provider not in {"tmdb", "tvdb", "musicbrainz"} or not provider_id:
        raise HTTPException(400, "A provider and provider ID are required.")
    return await run_control(_set_match_sync, entity_id, provider, provider_id)


def _get_image_sync(entity_id: str, image_type: str, locale: str):
    if image_type not in IMAGE_TYPES:
        raise HTTPException(
            400,
            f"Unsupported image type '{image_type}'. Expected one of: {', '.join(sorted(IMAGE_TYPES))}",
        )
    item = _entity(entity_id, locale)
    pending = False
    for candidate in _image_entities(item, image_type):
        library = store.get(candidate["libraryId"])
        if not library:
            raise HTTPException(404, "Library not found.")
        local = store.db.execute(
            "SELECT relative_path,quick_fingerprint FROM media_files WHERE entity_id=? AND role='image' ORDER BY relative_path",
            (candidate["id"],),
        )
        if library["directory"]:
            for relative_path, content_hash in local:
                if not _local_image_for_type(relative_path, image_type):
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

    image = None
    image_item = item
    for candidate in _image_entities(item, image_type):
        selected = store.db.execute(
            "SELECT local_path FROM catalog_artwork_selection "
            "WHERE entity_id=? AND locale=? AND image_type=? LIMIT 1",
            (candidate["id"], locale, image_type),
        )
        if selected and selected[0][0] and Path(selected[0][0]).is_file():
            return FileResponse(
                selected[0][0],
                media_type="image/webp",
                headers={"X-ZenStream-Image-State": "ready"},
            )
        if selected:
            pending = True
            continue
        metadata = _metadata_for(candidate, locale, False, False)
        if not metadata:
            pending = pending or bool(candidate.get("providerIds"))
            continue
        read_service = MetadataReadService(store.db)
        image = choose_artwork(
            metadata.get("images", []),
            locale,
            image_type,
            metadata.get("originalLanguage"),
            read_service.providers(candidate["type"]),
            prefer_no_language_for_backdrop=(
                MetadataLanguageSettings().prefer_no_language_for_backdrop()
            ),
        )
        if image:
            image_item = candidate
            break
    if not image:
        if pending:
            logger.info(
                "image pending entity_id=%s locale=%s image_type=%s",
                entity_id,
                locale,
                image_type,
            )
            return Response(
                status_code=202,
                headers={"Retry-After": "2", "X-ZenStream-Image-State": "pending"},
            )
        logger.warning(
            "image unavailable because entity has no cached artwork entity_id=%s image_type=%s",
            entity_id,
            image_type,
        )
        return Response(status_code=404, headers={"X-ZenStream-Image-State": "missing"})
    cached_file = store.db.execute(
        "SELECT local_path FROM metadata_images WHERE provider=? AND entity_type=? "
        "AND provider_id=? AND image_type=? AND image_url=? AND local_path IS NOT NULL",
        (
            image.get("provider"),
            image_item["type"],
            next(
                (
                    identity.get("id")
                    for identity in image_item.get("providerIds", [])
                    if identity.get("provider") == image.get("provider")
                ),
                None,
            ),
            image_type,
            image["url"],
        ),
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


@router.get("/library-items/{entity_id}/image")
async def get_image(
    entity_id: str,
    imageType: str = Query("Primary"),
    locale: str | None = Query(None),
    Username: str | None = Header(None),
    TOKEN: str | None = Header(None),
):
    require_admin(Username, TOKEN)
    locale = await _configured_locale_async(locale)
    return await run_control(_get_image_sync, entity_id, imageType, locale)
