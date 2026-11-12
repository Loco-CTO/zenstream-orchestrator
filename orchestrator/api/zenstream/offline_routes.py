from typing import Annotated

from app.config import Config
from app.foreground import run_control, run_foreground
from app.netease_lyrics import (
    LyricsFallback,
    NetEaseUnavailable,
    fetch_netease_lyrics,
    netease_enabled,
)
from app.offline import OfflineService
from app.offline_assets import OfflineAssetRequest, OfflineAssets, byte_range
from app.offline_bundle import (
    bundle,
    external_subtitles,
    item_bundle,
    supplement_path,
    supplement_revision,
)
from app.offline_reconcile import OfflineReconciler, OfflineReconcileRequest
from app.offline_selections import OfflineSelectionRequest, OfflineSelections
from app.offline_sync import AccountSyncRequest, AccountSyncService
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from api.zenstream.client_routes import (
    _bounded_json_object,
    _require_account,
    catalog,
    subtitle,
)

router = APIRouter()


@router.get("/api/offline/items/{item_id}/bundle")
async def get_offline_item_bundle(
    item_id: str,
    request: Request,
    sourceId: str = Query(min_length=1, max_length=200),
    sourceRevision: str = Query(min_length=1, max_length=200),
    language: str = Query("en", min_length=2, max_length=30),
):
    account, _ = await _require_account(request)
    return await run_foreground(
        item_bundle,
        OfflineAssets(catalog),
        account["id"],
        item_id,
        sourceId,
        sourceRevision,
        language,
    )


@router.get("/api/offline/items/{item_id}/subtitles/{file_id}.vtt")
async def get_offline_item_subtitle(
    item_id: str,
    file_id: str,
    request: Request,
    sourceId: str = Query(min_length=1, max_length=200),
    sourceRevision: str = Query(min_length=1, max_length=200),
    v: str | None = Query(None, max_length=200),
):
    account, _ = await _require_account(request)
    status, _, tracks = await run_foreground(
        external_subtitles,
        OfflineAssets(catalog),
        account["id"],
        item_id,
        sourceId,
        sourceRevision,
    )
    if status != "unchanged":
        raise HTTPException(409, "The downloaded media source changed.")
    selected = next((track for track in tracks if track["fileId"] == file_id), None)
    if selected is None:
        raise HTTPException(404, "Subtitle does not belong to this source.")
    if v is not None and selected["revision"] != v:
        raise HTTPException(409, "The subtitle changed; refresh its descriptor.")
    return await subtitle(item_id, file_id, request)


@router.post("/api/offline/reconcile")
async def reconcile_offline_objects(request: Request):
    account, _ = await _require_account(request)
    try:
        body = OfflineReconcileRequest.model_validate(
            await _bounded_json_object(request, limit=262144)
        )
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error
    return await run_foreground(
        OfflineReconciler(catalog).reconcile, account["id"], body
    )


@router.get("/api/offline/assets/{asset_id}/bundle")
async def get_offline_bundle(
    asset_id: str,
    request: Request,
    language: str = Query("en", min_length=2, max_length=30),
    fallback: Annotated[
        LyricsFallback | None,
        Query(
            description="Include NetEase lyrics when the download has no local lyrics."
        ),
    ] = None,
):
    account, _ = await _require_account(request)
    result = await run_foreground(
        bundle, OfflineAssets(catalog), account["id"], asset_id, language
    )
    if (
        result["lyrics"] is None
        and "lyrics_unavailable" not in result["warnings"]
        and fallback == "netease"
        and netease_enabled()
    ):
        track = next(
            (item for item in result["items"] if item["id"] == result["itemId"]), None
        )
        if track is not None:
            try:
                result["lyrics"] = await fetch_netease_lyrics(track)
            except NetEaseUnavailable:
                result["warnings"].append("lyrics_unavailable")
            if result["lyrics"] is not None:
                result["supplementRevision"] = supplement_revision(result)
    return result


@router.get("/api/offline/assets/{asset_id}/supplements/{index}.vtt")
async def get_offline_supplement(asset_id: str, index: int, request: Request):
    account, _ = await _require_account(request)
    target = await run_control(
        supplement_path, OfflineAssets(catalog), account["id"], asset_id, index
    )
    return FileResponse(
        target, media_type="text/vtt", headers={"Cache-Control": "private, no-store"}
    )


@router.post("/api/offline/assets")
async def create_offline_asset(request: Request):
    account, _ = await _require_account(request)
    data = await _bounded_json_object(request)
    try:
        body = OfflineAssetRequest.model_validate(data)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error
    return await run_control(OfflineAssets(catalog).request, account["id"], body)


@router.get("/api/offline/assets/{asset_id}")
async def get_offline_asset(asset_id: str, request: Request):
    account, _ = await _require_account(request)
    return await run_foreground(OfflineAssets(catalog).get, account["id"], asset_id)


@router.api_route("/api/offline/assets/{asset_id}/content", methods=["GET", "HEAD"])
async def offline_asset_content(asset_id: str, request: Request):
    account, _ = await _require_account(request)
    stream, descriptor = await run_control(
        OfflineAssets(catalog).open_delivery, account["id"], asset_id
    )
    size = descriptor["sizeBytes"]
    etag = '"' + descriptor["sha256"] + '"'
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "private, no-store",
    }
    if request.headers.get("if-none-match") == etag:
        await run_control(stream.close)
        return Response(status_code=304, headers=headers)
    start, end = 0, size - 1
    status = 200
    requested = request.headers.get("range")
    if requested and request.headers.get("if-range", etag) == etag:
        try:
            start, end = byte_range(requested, size)
        except ValueError:
            await run_control(stream.close)
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
            )
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        await run_control(stream.close)
        return Response(
            status_code=status, headers=headers, media_type=descriptor["mimeType"]
        )

    def chunks():
        try:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(
        chunks(),
        status_code=status,
        headers=headers,
        media_type=descriptor["mimeType"],
        background=BackgroundTask(stream.close),
    )


async def _selection_body(request):
    try:
        return OfflineSelectionRequest.model_validate(
            await _bounded_json_object(request)
        )
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error


@router.post("/api/offline/selections")
async def create_offline_selection(request: Request):
    account, _ = await _require_account(request)
    body = await _selection_body(request)
    return await run_control(OfflineSelections(catalog).create, account["id"], body)


@router.get("/api/offline/selections/{selection_id}")
async def get_offline_selection(
    selection_id: str, request: Request, offset: int = Query(0, ge=0)
):
    account, _ = await _require_account(request)
    return await run_foreground(
        OfflineSelections(catalog).get, account["id"], selection_id, offset
    )


@router.delete("/api/offline/selections/{selection_id}")
async def cancel_offline_selection(selection_id: str, request: Request):
    account, _ = await _require_account(request)
    return await run_control(
        OfflineSelections(catalog).cancel, account["id"], selection_id
    )


@router.get("/api/offline/capabilities")
async def offline_capabilities(request: Request):
    await _require_account(request)
    return await run_foreground(OfflineService(Config().database).capabilities)


@router.post("/api/account/sync")
async def account_sync(request: Request):
    account, _ = await _require_account(request)
    data = await _bounded_json_object(request, limit=262144)
    try:
        batch = AccountSyncRequest.model_validate(data)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error
    return await run_control(
        AccountSyncService(catalog).synchronize, account["id"], batch
    )
