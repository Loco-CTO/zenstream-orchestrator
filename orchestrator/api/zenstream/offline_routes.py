from app.config import Config
from app.foreground import run_foreground
from app.offline import OfflineService
from fastapi import APIRouter, Request

from api.zenstream.client_routes import _require_account

router = APIRouter()


@router.get("/api/offline/capabilities")
async def offline_capabilities(request: Request):
    await _require_account(request)
    return await run_foreground(OfflineService(Config().database).capabilities)
