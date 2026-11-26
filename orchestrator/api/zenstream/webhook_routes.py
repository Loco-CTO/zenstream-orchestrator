import sqlite3
from typing import Annotated

from app.config import Config
from app.discord_webhooks import queue_test, remove_webhook, save_webhook, settings
from app.foreground import run_auth, run_control
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints

from api.zenstream.library_routes import authenticate_admin_request


async def require_admin(request: Request):
    await run_auth(authenticate_admin_request, request)


router = APIRouter(
    prefix="/api/admin/webhooks",
    dependencies=[Depends(require_admin)],
    include_in_schema=False,
)


class WebhookInput(BaseModel):
    name: Annotated[str, StringConstraints(max_length=80)]
    url: Annotated[str, StringConstraints(max_length=512)] | None = None
    events: list[Annotated[str, StringConstraints(max_length=64)]] = Field(
        max_length=40
    )


async def _control(action, *args):
    try:
        return await run_control(lambda: action(Config().database, *args))
    except LookupError:
        raise HTTPException(404, "Webhook not found.") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(
            409, "This Discord webhook URL is already configured."
        ) from None


async def _input(request):
    # Validation details from Pydantic can echo the submitted webhook token.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise HTTPException(413, "Webhook settings are too large.")
        body.extend(chunk)
    try:
        return WebhookInput.model_validate_json(body)
    except ValueError:
        raise HTTPException(400, "Invalid webhook settings.") from None


@router.get("")
async def list_webhooks():
    return await _control(settings)


@router.post("", status_code=201)
async def create_webhook(request: Request):
    return await _control(save_webhook, None, await _input(request))


@router.put("/{hook_id}")
async def update_webhook(hook_id: str, request: Request):
    return await _control(save_webhook, hook_id, await _input(request))


@router.delete("/{hook_id}", status_code=204)
async def delete_webhook(hook_id: str):
    await _control(remove_webhook, hook_id)


@router.post("/{hook_id}/test", status_code=202)
async def test_webhook(hook_id: str):
    await _control(queue_test, hook_id)
    return {"message": "Test message queued."}
