from app.config import Config
from app.foreground import run_control, run_foreground
from app.offline import OfflineService
from app.offline_sync import AccountSyncRequest, AccountSyncService
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from api.zenstream.client_routes import _bounded_json_object, _require_account, catalog

router = APIRouter()


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
