from __future__ import annotations

from app.calendar import (
    CALENDAR_PROVIDERS,
    CalendarConnectionStore,
    CalendarReadService,
    parse_calendar_window,
)
from app.client_auth import require_account
from app.foreground import run_auth, run_control, run_foreground
from fastapi import APIRouter, HTTPException, Query, Request

from api.zenstream.library_routes import authenticate_admin_request

router = APIRouter()


async def _require_admin(request: Request) -> None:
    await run_auth(authenticate_admin_request, request)


@router.get("/api/calendar")
async def calendar_events(
    request: Request,
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    account, _ = await run_auth(require_account, request)
    try:
        window_start, window_end = parse_calendar_window(start, end)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return await run_foreground(
        CalendarReadService().list,
        account["id"],
        window_start,
        window_end,
    )


@router.get("/api/admin/calendar/settings")
async def get_calendar_settings(request: Request):
    await _require_admin(request)
    return await run_control(CalendarConnectionStore().public)


@router.put("/api/admin/calendar/settings")
async def update_calendar_settings(request: Request):
    await _require_admin(request)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "Calendar settings must be an object")
    try:
        result = await run_control(_save_calendar_settings, data)
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    return result


def _save_calendar_settings(data: dict) -> dict:
    store = CalendarConnectionStore()
    for provider in CALENDAR_PROVIDERS:
        if provider in data:
            store.save(provider, data[provider])
    return store.public()

