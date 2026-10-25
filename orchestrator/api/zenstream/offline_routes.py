from app.config import Config
from app.foreground import run_control, run_foreground
from app.offline import OfflineService
from app.offline_assets import OfflineAssetRequest, OfflineAssets, byte_range
from app.offline_selections import OfflineSelectionRequest, OfflineSelections
from app.offline_sync import AccountSyncRequest, AccountSyncService
from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from api.zenstream.client_routes import _bounded_json_object, _require_account, catalog

router = APIRouter()


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
