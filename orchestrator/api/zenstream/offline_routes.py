from app.config import Config
from app.foreground import run_control, run_foreground
from app.offline import OfflineService
from app.offline_selections import OfflineSelectionRequest, OfflineSelections
from app.offline_sync import AccountSyncRequest, AccountSyncService
from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from api.zenstream.client_routes import _bounded_json_object, _require_account, catalog

router = APIRouter()


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
