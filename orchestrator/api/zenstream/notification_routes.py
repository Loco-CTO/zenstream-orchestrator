from __future__ import annotations

from app.client_auth import require_account
from app.foreground import run_auth, run_control, run_foreground
from app.notifications import NotificationService
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter()


async def _account(request: Request) -> dict:
    account, _ = await run_auth(require_account, request)
    return account


@router.get("/api/notifications")
async def notifications(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
):
    account = await _account(request)
    return await run_foreground(
        NotificationService().list,
        account["id"],
        limit,
        cursor,
    )


@router.patch("/api/notifications/{notification_id}")
async def update_notification(notification_id: str, request: Request):
    account = await _account(request)
    data = await request.json()
    if not isinstance(data, dict) or not isinstance(data.get("read"), bool):
        raise HTTPException(400, "The read field must be a boolean.")
    return await run_control(
        NotificationService().mark_read,
        account["id"],
        notification_id,
        data["read"],
    )


@router.delete("/api/notifications/{notification_id}")
async def delete_notification(notification_id: str, request: Request):
    account = await _account(request)
    return await run_control(
        NotificationService().delete_notification,
        account["id"],
        notification_id,
    )


@router.post("/api/notifications/read-all")
async def read_all_notifications(request: Request):
    account = await _account(request)
    return await run_control(NotificationService().mark_all_read, account["id"])


@router.get("/api/notifications/summary")
async def notification_summary(request: Request):
    account = await _account(request)
    return await run_foreground(NotificationService().summary, account["id"])


@router.get("/api/notifications/push-config")
async def notification_push_config(request: Request):
    await _account(request)
    return NotificationService.push_config()


@router.put("/api/notifications/push-subscription")
async def register_push_subscription(request: Request):
    account = await _account(request)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "The push subscription must be an object.")
    return await run_control(
        NotificationService().put_subscription,
        account["id"],
        data,
    )


@router.delete("/api/notifications/push-subscription")
async def remove_push_subscription(
    request: Request,
    endpoint: str | None = Query(None),
):
    account = await _account(request)
    return await run_control(
        NotificationService().delete_subscription,
        account["id"],
        endpoint,
    )
