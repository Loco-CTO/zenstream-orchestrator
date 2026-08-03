from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import HTTPException, Request, WebSocket

from app.models.account import Account


def bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def require_account(request: Request) -> tuple[dict, str]:
    token = bearer_token(request.headers.get("authorization"))
    account = getattr(request.state, "authenticated", None)
    if account is None:
        account = Account().authenticate_token(token)
    if not account or not token:
        raise HTTPException(401, "Authentication required.")
    return account, token


def optional_account(request: Request) -> tuple[dict, str] | None:
    token = bearer_token(request.headers.get("authorization"))
    account = getattr(request.state, "authenticated", None)
    if account is None:
        account = Account().authenticate_token(token)
    return (account, token) if account and token else None


def _secret() -> bytes:
    value = os.getenv("SECRET_KEY", "")
    if not value:
        raise HTTPException(503, "Orchestrator secret is not configured.")
    return value.encode("utf-8")


def issue_ticket(user_id: str, kind: str, ttl: int, **claims) -> str:
    payload = {"uid": user_id, "kind": kind, "exp": int(time.time()) + ttl, **claims}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(_secret(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def read_ticket(value: str | None, kind: str | None = None) -> dict:
    try:
        encoded, supplied = str(value or "").split(".", 1)
        actual = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
        expected = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(actual, expected):
            raise ValueError
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(
            time.time()
        ):
            raise ValueError
        if kind and payload.get("kind") != kind:
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        raise HTTPException(401, "Invalid or expired access ticket.")


def account_from_access(request: Request, kind: str = "resource") -> dict:
    authenticated = optional_account(request)
    if authenticated:
        return authenticated[0]
    payload = read_ticket(request.query_params.get("access"), kind)
    rows = Account().db.read_execute(
        "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE id=?",
        (payload.get("uid"),),
    )
    if not rows or rows[0][4]:
        raise HTTPException(401, "Account is unavailable.")
    return Account._public(rows[0])


def websocket_account(websocket: WebSocket) -> dict | None:
    ticket = websocket.query_params.get("ticket")
    if not ticket:
        return None
    try:
        payload = read_ticket(ticket, "socket")
    except HTTPException:
        return None
    rows = Account().db.read_execute(
        "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE id=?",
        (payload.get("uid"),),
    )
    return Account._public(rows[0]) if rows and not rows[0][4] else None
