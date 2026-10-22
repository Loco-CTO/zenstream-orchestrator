"""Authenticated Orchestrator routes for the optional in-process Lumi package."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from app.client_auth import require_account
from app.foreground import run_auth, run_control
from app.lumi_host import LumiHostError, lumi_host
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from api.zenstream.client_routes import catalog as catalog_service
from api.zenstream.library_routes import authenticate_admin_request

router = APIRouter()
_MAX_BODY_BYTES = 64_000
logger = logging.getLogger("zenstream.lumi")
lumi_host.catalog = catalog_service


def _private_json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


async def _account(request: Request) -> dict[str, Any]:
    account, _token = await run_auth(require_account, request)
    if not isinstance(account, dict) or not isinstance(account.get("id"), str):
        raise HTTPException(401, "Authentication required.")
    return account


async def _admin(request: Request) -> str:
    return await run_auth(authenticate_admin_request, request)


async def _object_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(413, "The request body is too large.")
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(400, "A valid JSON object is required.") from error
    if not isinstance(payload, dict):
        raise HTTPException(400, "A valid JSON object is required.")
    return payload


def _service():
    if not lumi_host.status()["integration"]["enabled"]:
        raise HTTPException(503, "The Lumi integration is disabled.")
    service = lumi_host.service
    if service is None:
        raise HTTPException(503, "Install and enable a supported Qwen3.5 model first.")
    return service


def _value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _conversation(value: Any) -> dict[str, Any]:
    item = _value(value)
    if not isinstance(item, dict):
        raise HTTPException(503, "Lumi returned an invalid conversation.")
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "model": item.get("model"),
        "thinking": item.get("thinking") is True,
        "createdAt": item.get("created_at", item.get("createdAt")),
        "updatedAt": item.get("updated_at", item.get("updatedAt")),
    }


def _reference(value: Any) -> dict[str, Any]:
    return {
        "type": getattr(value, "type", None),
        "id": getattr(value, "id", None),
        "title": getattr(value, "title", None),
    }


def _source(value: Any) -> dict[str, Any]:
    return {
        "url": getattr(value, "url", None),
        "websiteName": getattr(value, "website_name", None),
        "title": getattr(value, "title", None),
        "faviconUrl": getattr(value, "favicon_url", None),
    }


def _message(value: Any) -> dict[str, Any]:
    return {
        "id": getattr(value, "id", None),
        "role": getattr(value, "role", None),
        "content": getattr(value, "content", ""),
        "createdAt": getattr(value, "created_at", None),
        "references": [_reference(item) for item in getattr(value, "references", ())],
        "sources": [_source(item) for item in getattr(value, "sources", ())],
    }


def _encode_message_cursor(owner_id: str, conversation_id: str, before_ordinal: int) -> str:
    payload = json.dumps(
        {
            "version": 1,
            "owner": owner_id,
            "conversation": conversation_id,
            "before": before_ordinal,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_message_cursor(value: str, owner_id: str, conversation_id: str) -> int:
    try:
        if not value or len(value) > 1_024:
            raise ValueError
        payload = json.loads(
            base64.b64decode(
                value + "=" * (-len(value) % 4),
                altchars=b"-_",
                validate=True,
            )
        )
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("owner") != owner_id
            or payload.get("conversation") != conversation_id
            or type(payload.get("before")) is not int
            or payload["before"] < 1
        ):
            raise ValueError
        return payload["before"]
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(400, "The Lumi message cursor is invalid.") from error


def _raise_service_error(error: Exception) -> None:
    """Map package errors to a small stable HTTP surface without leaking internals."""
    status_code, message = _service_error(error)
    raise HTTPException(status_code, message) from error


def _service_error(error: Exception) -> tuple[int, str]:
    """Return a safe public status and message for a Lumi package error."""
    name = type(error).__name__
    if name == "ConversationNotFound":
        return 404, "Conversation not found."
    if name == "ModelConfigurationError":
        return 409, "The selected model or thinking option is unavailable."
    if name == "LumiServiceBusy":
        return 429, "Lumi is busy. Try again shortly."
    if isinstance(error, (ValueError, TypeError)):
        return 400, "The Lumi request is invalid."
    return 503, "Lumi is temporarily unavailable."


def _turn_payload(turn: Any) -> dict[str, Any]:
    return {
        "conversation": _conversation(turn.conversation),
        "answer": {
            "markdown": turn.answer.markdown,
            "finishReason": getattr(turn.answer, "finish_reason", "stop"),
            "references": [_reference(value) for value in turn.answer.references],
            "sources": [_source(value) for value in turn.answer.sources],
        },
    }


def _sse_event(name: str, value: dict[str, Any]) -> bytes:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n".encode()


@router.get("/api/lumi/models")
async def lumi_models(request: Request):
    await _account(request)
    _service()
    return _private_json(lumi_host.public_models())


@router.get("/api/lumi/conversations")
async def lumi_conversations(request: Request):
    account = await _account(request)
    service = _service()
    try:
        values = await service.list_conversations_for_account(account["id"], limit=50)
    except Exception as error:
        _raise_service_error(error)
    return _private_json({"conversations": [_conversation(value) for value in values]})


@router.get("/api/lumi/conversations/{conversation_id}")
async def lumi_conversation(
    request: Request,
    conversation_id: str,
    cursor: str | None = None,
):
    account = await _account(request)
    service = _service()
    try:
        snapshot = await service.get_conversation_for_account(account["id"], conversation_id)
        next_cursor = None
        if cursor is None:
            messages = snapshot.messages
            has_more = getattr(snapshot, "has_older_messages", False)
            if has_more and messages:
                next_cursor = _encode_message_cursor(
                    account["id"], conversation_id, messages[0].ordinal
                )
        else:
            before_ordinal = _decode_message_cursor(
                cursor, account["id"], conversation_id
            )
            page_loader = getattr(service, "get_message_page_for_account", None)
            if not callable(page_loader):
                raise HTTPException(409, "This Lumi release does not support message pagination.")
            page = await page_loader(
                account["id"],
                conversation_id,
                before_ordinal=before_ordinal,
            )
            messages = page.messages
            has_more = page.has_more
            if has_more and page.next_before_ordinal is not None:
                next_cursor = _encode_message_cursor(
                    account["id"], conversation_id, page.next_before_ordinal
                )
    except Exception as error:
        if isinstance(error, HTTPException):
            raise
        _raise_service_error(error)
    return _private_json(
        {
            "conversation": _conversation(snapshot.conversation),
            "messages": [_message(value) for value in messages],
            "nextCursor": next_cursor,
            "hasMore": has_more,
            "hasSummary": getattr(snapshot, "summary", None) is not None,
        }
    )


@router.patch("/api/lumi/conversations/{conversation_id}")
async def lumi_rename_conversation(request: Request, conversation_id: str):
    account = await _account(request)
    payload = await _object_body(request)
    title = payload.get("title")
    if (
        set(payload) != {"title"}
        or not isinstance(title, str)
        or not title.strip()
        or len(" ".join(title.split())) > 120
    ):
        raise HTTPException(400, "The Lumi conversation title is invalid.")
    try:
        result = await _service().rename_conversation_for_account(
            account["id"], conversation_id, " ".join(title.split())
        )
    except Exception as error:
        _raise_service_error(error)
    return _private_json({"conversation": _conversation(result)})


@router.delete("/api/lumi/conversations/{conversation_id}")
async def lumi_delete_conversation(request: Request, conversation_id: str):
    account = await _account(request)
    try:
        await _service().delete_conversation_for_account(account["id"], conversation_id)
    except Exception as error:
        _raise_service_error(error)
    return _private_json({"deleted": True})


@router.post("/api/lumi/conversations/{conversation_id}/turns")
async def lumi_turn(request: Request, conversation_id: str):
    account = await _account(request)
    payload = await _object_body(request)
    message = payload.get("message")
    model = payload.get("model")
    thinking = payload.get("thinking")
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 6_000
        or set(payload) - {"message", "model", "thinking"}
        or (model is None) != (thinking is None)
        or (model is not None and (not isinstance(model, str) or len(model) > 200))
        or (thinking is not None and type(thinking) is not bool)
    ):
        raise HTTPException(400, "The Lumi turn request is invalid.")
    service = _service()
    try:
        turn = await service.chat_for_account(
            account["id"],
            conversation_id,
            message.strip(),
            model=model,
            thinking=thinking,
        )
    except Exception as error:
        _raise_service_error(error)
    return _private_json(_turn_payload(turn))


@router.post("/api/lumi/conversations/{conversation_id}/turns/stream")
async def lumi_turn_stream(request: Request, conversation_id: str):
    turn_id = uuid4().hex
    started = time.perf_counter()
    account = await _account(request)
    payload = await _object_body(request)
    message = payload.get("message")
    model = payload.get("model")
    thinking = payload.get("thinking")
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 6_000
        or set(payload) - {"message", "model", "thinking"}
        or (model is None) != (thinking is None)
        or (model is not None and (not isinstance(model, str) or len(model) > 200))
        or (thinking is not None and type(thinking) is not bool)
    ):
        raise HTTPException(400, "The Lumi turn request is invalid.")
    service = _service()
    logger.info(
        "Lumi stream accepted turn_id=%s message_chars=%d model_override=%s thinking_override=%s",
        turn_id,
        len(message.strip()),
        model is not None,
        thinking is not None,
    )

    async def events():
        stream = None
        completed = False
        event_count = 0
        delta_count = 0
        visible_chars = 0
        first_event_ms = None
        try:
            stream_method = getattr(service, "stream_chat_for_account", None)
            if not callable(stream_method):
                # Installed Lumi releases remain pinned until an administrator
                # selects an update. Keep older supported releases usable while
                # the browser switches to the streaming endpoint.
                turn = await service.chat_for_account(
                    account["id"],
                    conversation_id,
                    message.strip(),
                    model=model,
                    thinking=thinking,
                )
                completed = True
                logger.info(
                    "Lumi stream completed turn_id=%s mode=legacy duration_ms=%.1f",
                    turn_id,
                    (time.perf_counter() - started) * 1000,
                )
                yield _sse_event("complete", _turn_payload(turn))
                return

            parameters = inspect.signature(stream_method).parameters
            supports_turn_id = "turn_id" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            stream_kwargs = {"model": model, "thinking": thinking}
            if supports_turn_id:
                stream_kwargs["turn_id"] = turn_id
            logger.info(
                "Lumi stream execution started turn_id=%s mode=streaming supports_turn_id=%s",
                turn_id,
                supports_turn_id,
            )
            stream = stream_method(
                account["id"],
                conversation_id,
                message.strip(),
                **stream_kwargs,
            )
            async for event in stream:
                kind = getattr(event, "kind", None)
                event_count += 1
                if first_event_ms is None:
                    first_event_ms = (time.perf_counter() - started) * 1000
                    logger.info(
                        "Lumi stream first event turn_id=%s kind=%s time_to_first_event_ms=%.1f",
                        turn_id,
                        kind or "unknown",
                        first_event_ms,
                    )
                if kind == "delta" and not completed:
                    text = getattr(event, "text", None)
                    if isinstance(text, str) and text:
                        delta_count += 1
                        visible_chars += len(text)
                        if delta_count == 1 or delta_count % 64 == 0:
                            logger.info(
                                "Lumi stream progress turn_id=%s delta_count=%d visible_chars=%d elapsed_ms=%.1f",
                                turn_id,
                                delta_count,
                                visible_chars,
                                (time.perf_counter() - started) * 1000,
                            )
                        yield _sse_event("delta", {"text": text})
                elif kind == "reset" and not completed:
                    reason = getattr(event, "reason", None)
                    if reason not in {"intermediate", "cpu_fallback"}:
                        reason = "intermediate"
                    logger.info(
                        "Lumi stream reset turn_id=%s reason=%s elapsed_ms=%.1f",
                        turn_id,
                        reason,
                        (time.perf_counter() - started) * 1000,
                    )
                    yield _sse_event("reset", {"reason": reason})
                elif kind == "status" and not completed:
                    status = getattr(event, "status", None)
                    if status in {"context_expansion", "summarization", "generation"}:
                        yield _sse_event("status", {"stage": status})
                elif kind == "complete" and not completed:
                    turn = getattr(event, "turn", None)
                    if turn is None:
                        raise RuntimeError("Lumi stream completed without a turn")
                    completed = True
                    logger.info(
                        "Lumi stream completed turn_id=%s mode=streaming event_count=%d delta_count=%d visible_chars=%d duration_ms=%.1f",
                        turn_id,
                        event_count,
                        delta_count,
                        visible_chars,
                        (time.perf_counter() - started) * 1000,
                    )
                    yield _sse_event("complete", _turn_payload(turn))
                elif not completed:
                    logger.warning(
                        "Lumi stream ignored unsupported event turn_id=%s event_kind=%s",
                        turn_id,
                        kind or "unknown",
                    )
            if not completed:
                logger.error(
                    "Lumi stream ended without completion turn_id=%s event_count=%d delta_count=%d duration_ms=%.1f",
                    turn_id,
                    event_count,
                    delta_count,
                    (time.perf_counter() - started) * 1000,
                )
                yield _sse_event(
                    "error", {"message": "Lumi did not complete the response."}
                )
        except asyncio.CancelledError:
            logger.info(
                "Lumi stream cancelled turn_id=%s completed=%s event_count=%d delta_count=%d duration_ms=%.1f",
                turn_id,
                completed,
                event_count,
                delta_count,
                (time.perf_counter() - started) * 1000,
            )
            raise
        except Exception as error:
            logger.exception(
                "Lumi streaming turn failed turn_id=%s category=%s event_count=%d delta_count=%d duration_ms=%.1f",
                turn_id,
                type(error).__name__,
                event_count,
                delta_count,
                (time.perf_counter() - started) * 1000,
            )
            _status_code, safe_message = _service_error(error)
            yield _sse_event("error", {"message": safe_message})
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.warning(
                        "Lumi stream close failed turn_id=%s duration_ms=%.1f",
                        turn_id,
                        (time.perf_counter() - started) * 1000,
                        exc_info=True,
                    )
            logger.info(
                "Lumi stream closed turn_id=%s completed=%s event_count=%d delta_count=%d duration_ms=%.1f",
                turn_id,
                completed,
                event_count,
                delta_count,
                (time.perf_counter() - started) * 1000,
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.patch("/api/lumi/conversations/{conversation_id}/choice")
async def lumi_conversation_choice(request: Request, conversation_id: str):
    account = await _account(request)
    payload = await _object_body(request)
    if (
        set(payload) != {"model", "thinking"}
        or not isinstance(payload.get("model"), str)
        or not 1 <= len(payload["model"]) <= 200
        or type(payload.get("thinking")) is not bool
    ):
        raise HTTPException(400, "The Lumi model choice is invalid.")
    try:
        result = await _service().update_choice_for_account(
            account["id"], conversation_id, payload["model"], payload["thinking"]
        )
    except Exception as error:
        _raise_service_error(error)
    return _private_json({"conversation": _conversation(result)})


@router.put("/api/lumi/preferences/model")
async def lumi_model_preference(request: Request):
    account = await _account(request)
    payload = await _object_body(request)
    if (
        set(payload) != {"model", "thinking"}
        or not isinstance(payload.get("model"), str)
        or not 1 <= len(payload["model"]) <= 200
        or type(payload.get("thinking")) is not bool
    ):
        raise HTTPException(400, "The Lumi model choice is invalid.")
    try:
        result = await _service().update_user_preference_for_account(
            account["id"], payload["model"], payload["thinking"]
        )
    except Exception as error:
        _raise_service_error(error)
    item = _value(result)
    return _private_json(
        {
            "preference": {
                "model": item.get("model"),
                "thinking": item.get("thinking") is True,
            }
        }
    )


@router.get("/api/admin/lumi/status")
async def admin_lumi_status(request: Request):
    await _admin(request)
    return _private_json(lumi_host.status())


@router.get("/api/admin/lumi/releases")
async def admin_lumi_releases(request: Request):
    await _admin(request)
    try:
        release_manager = lumi_host.release_manager
        releases = await run_control(lumi_host.list_releases_sync, release_manager)
    except Exception as error:
        logger.exception("Lumi published release discovery failed")
        raise HTTPException(
            503, "Published Lumi releases are temporarily unavailable."
        ) from error
    return _private_json({"releases": releases})


@router.put("/api/admin/lumi/settings")
async def update_admin_lumi_settings(request: Request):
    await _admin(request)
    payload = await _object_body(request)
    enabled = payload.get("enabled")
    if type(enabled) is not bool or set(payload) - {"enabled", "releaseTag"}:
        raise HTTPException(400, "The Lumi integration settings are invalid.")
    if not enabled:
        await lumi_host.disable()
        return _private_json(lumi_host.status())
    release_tag = payload.get("releaseTag")
    if not isinstance(release_tag, str):
        raise HTTPException(400, "Select a published Lumi release to install.")
    try:
        await lumi_host.enable(release_tag)
    except LumiHostError as error:
        raise HTTPException(409, str(error)) from error
    return _private_json(lumi_host.status(), status_code=202)


@router.delete("/api/admin/lumi/installation")
async def remove_admin_lumi_installation(request: Request):
    await _admin(request)
    try:
        await lumi_host.remove_installation()
    except LumiHostError as error:
        raise HTTPException(409, str(error)) from error
    return _private_json(lumi_host.status())


@router.get("/api/admin/lumi/models")
async def admin_lumi_models(request: Request):
    await _admin(request)
    return _private_json(lumi_host.model_settings())


@router.patch("/api/admin/lumi/models/settings")
async def update_admin_lumi_runtime_settings(request: Request):
    await _admin(request)
    payload = await _object_body(request)
    if (
        not payload
        or set(payload) - {"defaultThinking", "limits", "gpuMode"}
        or (
            "defaultThinking" in payload
            and type(payload["defaultThinking"]) is not bool
        )
        or ("limits" in payload and not isinstance(payload["limits"], dict))
        or (
            "gpuMode" in payload
            and payload["gpuMode"] is not None
            and (
                not isinstance(payload["gpuMode"], str)
                or payload["gpuMode"] not in {"automatic", "cpu_only", "gpu_preferred"}
            )
        )
    ):
        raise HTTPException(400, "The Lumi runtime settings are invalid.")
    try:
        settings = await lumi_host.update_runtime_settings(
            default_thinking=payload.get("defaultThinking"),
            limits=payload.get("limits"),
            gpu_mode=payload.get("gpuMode"),
        )
    except LumiHostError as error:
        raise HTTPException(409, str(error)) from error
    return _private_json(settings)


@router.patch("/api/admin/lumi/models/{model_id}")
async def update_admin_lumi_model(request: Request, model_id: str):
    await _admin(request)
    payload = await _object_body(request)
    if (
        set(payload) - {"enabled", "isDefault"}
        or type(payload.get("enabled")) is not bool
        or ("isDefault" in payload and type(payload["isDefault"]) is not bool)
    ):
        raise HTTPException(400, "The Lumi model settings are invalid.")
    try:
        await lumi_host.set_model_choice(
            model_id,
            enabled=payload["enabled"],
            is_default=payload.get("isDefault", False),
        )
    except LumiHostError as error:
        raise HTTPException(409, str(error)) from error
    return _private_json(lumi_host.model_settings())


@router.post("/api/admin/lumi/models/{model_id}/download")
async def download_admin_lumi_model(request: Request, model_id: str):
    await _admin(request)
    try:
        await lumi_host.start_model_install(model_id)
    except LumiHostError as error:
        raise HTTPException(409, str(error)) from error
    return _private_json(lumi_host.model_settings(), status_code=202)


@router.post("/api/admin/lumi/models/{model_id}/cancel")
async def cancel_admin_lumi_model_download(request: Request, model_id: str):
    await _admin(request)
    try:
        await lumi_host.cancel_model_install(model_id)
    except LumiHostError as error:
        raise HTTPException(409, str(error)) from error
    return _private_json(lumi_host.model_settings(), status_code=202)


@router.delete("/api/admin/lumi/models/{model_id}")
async def delete_admin_lumi_model(request: Request, model_id: str):
    await _admin(request)
    try:
        removed = await lumi_host.remove_model(model_id)
    except LumiHostError as error:
        raise HTTPException(409, str(error)) from error
    return _private_json({"id": model_id, "removed": removed})
