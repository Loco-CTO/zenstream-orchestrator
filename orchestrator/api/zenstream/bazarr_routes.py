from __future__ import annotations

from app.bazarr import (
    BazarrError,
    BazarrMatchError,
    BazarrConnectionStore,
    BazarrSubtitleService,
)
from app.client_auth import require_account
from app.foreground import run_auth, run_control, run_foreground
from fastapi import APIRouter, HTTPException, Query, Request

from api.zenstream.library_routes import authenticate_admin_request

router = APIRouter()


async def _require_account(request: Request) -> tuple[dict, str]:
    return await run_auth(require_account, request)


async def _require_admin(request: Request) -> None:
    await run_auth(authenticate_admin_request, request)


def _provider_error(error: BazarrError) -> HTTPException:
    if isinstance(error, BazarrMatchError):
        return HTTPException(
            409,
            detail={"code": error.code, "message": str(error)},
        )
    return HTTPException(502, "The subtitle downloader request failed.")


def _status(user_id: str, entity_id: str, source_id: str | None) -> dict:
    return BazarrSubtitleService().status(user_id, entity_id, source_id)


def _search(user_id: str, entity_id: str, source_id: str) -> dict:
    return BazarrSubtitleService().search(user_id, entity_id, source_id)


def _download(
    user_id: str, entity_id: str, source_id: str, match_id: str
) -> dict:
    return BazarrSubtitleService().download(user_id, entity_id, source_id, match_id)


def _public_settings() -> dict:
    return BazarrConnectionStore().public()


def _save_settings(data: dict) -> dict:
    return BazarrConnectionStore().save(data)


@router.get("/api/catalog/items/{entity_id}/bazarr/status")
async def bazarr_status(
    entity_id: str,
    request: Request,
    sourceId: str | None = Query(None),
):
    account, _ = await _require_account(request)
    try:
        return await run_foreground(
            _status,
            account["id"],
            entity_id,
            sourceId,
        )
    except BazarrError as error:
        raise _provider_error(error) from error


@router.post("/api/catalog/items/{entity_id}/bazarr/search")
async def bazarr_search(entity_id: str, request: Request):
    account, _ = await _require_account(request)
    data = await request.json()
    if not isinstance(data, dict) or not str(data.get("sourceId") or "").strip():
        raise HTTPException(400, "sourceId is required.")
    try:
        return await run_foreground(
            _search,
            account["id"],
            entity_id,
            str(data["sourceId"]),
        )
    except BazarrError as error:
        raise _provider_error(error) from error


@router.post("/api/catalog/items/{entity_id}/bazarr/download")
async def bazarr_download(entity_id: str, request: Request):
    account, _ = await _require_account(request)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "Subtitle download request must be an object.")
    source_id = str(data.get("sourceId") or "").strip()
    match_id = str(data.get("matchId") or "").strip()
    if not source_id or not match_id:
        raise HTTPException(400, "sourceId and matchId are required.")
    try:
        return await run_foreground(
            _download,
            account["id"],
            entity_id,
            source_id,
            match_id,
        )
    except BazarrError as error:
        raise _provider_error(error) from error


@router.get("/api/admin/bazarr/settings")
async def get_bazarr_settings(request: Request):
    await _require_admin(request)
    return await run_control(_public_settings)


@router.put("/api/admin/bazarr/settings")
async def update_bazarr_settings(request: Request):
    await _require_admin(request)
    data = await request.json()
    try:
        return await run_control(_save_settings, data)
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
