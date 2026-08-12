from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import time
from urllib.parse import urlsplit

from app.models.account import Account
from fastapi import HTTPException, Request, WebSocket

_TICKET_TTL_LIMITS = {"resource": 15 * 60, "socket": 60}
_RESERVED_TICKET_CLAIMS = {"uid", "kind", "iat", "exp"}
CLIENT_SESSION_COOKIE = "__Host-zenstream-session"
DEV_CLIENT_SESSION_COOKIE = "zenstream-session"
DEV_CLIENT_SESSION_COOKIE_PREFIX = f"{DEV_CLIENT_SESSION_COOKIE}-"
_DEFAULT_BROWSER_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
)


def _normalized_origin(value: str | None) -> str | None:
    parsed = urlsplit(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def browser_origins() -> list[str]:
    values = [
        value.strip()
        for value in os.getenv("CORS_ORIGINS", "").split(",")
        if value.strip()
    ]
    values.extend(_DEFAULT_BROWSER_ORIGINS)
    return list(dict.fromkeys(filter(None, map(_normalized_origin, values))))


def administrator_origin_allowed(request: Request) -> bool:
    supplied = _normalized_origin(request.headers.get("origin"))
    if supplied is None:
        return False
    direct = _normalized_origin(
        f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    )
    forwarded = _normalized_origin(
        f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://"
        f"{request.headers.get('x-forwarded-host', '')}"
    )
    return supplied in {direct, forwarded, *browser_origins()}


def _request_hostname(request: Request) -> str:
    try:
        return (request.url.hostname or "").rstrip(".").lower()
    except (UnicodeError, ValueError):
        return ""


def _request_port(request: Request) -> int:
    try:
        port = request.url.port
    except ValueError:
        port = None
    if port is not None:
        return port
    return 443 if request.url.scheme == "https" else 80


def _is_loopback_host(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def cookie_secure(request: Request) -> bool:
    """Use secure cookies in production; permit explicit loopback HTTP development."""
    return request.url.scheme == "https" or not _is_loopback_host(
        _request_hostname(request)
    )


def session_cookie_name(request: Request) -> str:
    if cookie_secure(request):
        return CLIENT_SESSION_COOKIE
    return f"{DEV_CLIENT_SESSION_COOKIE_PREFIX}{_request_port(request)}"


def _session_cookie_token(request: Request) -> str | None:
    primary = request.cookies.get(session_cookie_name(request))
    if primary:
        return primary
    # Accept the unscoped loopback cookie during the migration window. Login and
    # logout expire it so separate local Orchestrator ports stop overwriting one
    # another as soon as the client next authenticates.
    if not cookie_secure(request):
        return request.cookies.get(DEV_CLIENT_SESSION_COOKIE)
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def require_account(request: Request) -> tuple[dict, str]:
    token = bearer_token(request.headers.get("authorization")) or _session_cookie_token(
        request
    )
    account = getattr(request.state, "authenticated", None)
    if account is None:
        account = Account().authenticate_token(token)
    if not account or not token:
        raise HTTPException(401, "Authentication required.")
    return account, token


def session_id_for_token(token: str | None) -> str | None:
    """Resolve the server-owned session id without exposing it as identity."""
    if not token:
        return None
    rows = Account().db.read_execute(
        "SELECT id FROM user_sessions WHERE token_hash=? AND expires_at>?",
        (_token_hash(token), _iso_now()),
    )
    return rows[0][0] if rows else None


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def optional_account(request: Request) -> tuple[dict, str] | None:
    token = bearer_token(request.headers.get("authorization")) or _session_cookie_token(
        request
    )
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
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("A ticket user is required.")
    maximum_ttl = _TICKET_TTL_LIMITS.get(kind)
    if maximum_ttl is None or not isinstance(ttl, int) or ttl < 1 or ttl > maximum_ttl:
        raise ValueError("Invalid ticket kind or lifetime.")
    if _RESERVED_TICKET_CLAIMS.intersection(claims):
        raise ValueError("Ticket claims may not replace reserved claims.")
    issued_at = int(time.time())
    payload = {
        "uid": user_id,
        "kind": kind,
        "iat": issued_at,
        "exp": issued_at + ttl,
        **claims,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(_secret(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def read_ticket(
    value: str | None,
    kind: str | None = None,
    expected_claims: dict | None = None,
) -> dict:
    try:
        raw = str(value or "")
        if len(raw) > 4096 or raw.count(".") != 1:
            raise ValueError
        encoded, supplied = raw.split(".", 1)
        actual = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
        if len(actual) != hashlib.sha256().digest_size:
            raise ValueError
        expected = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(actual, expected):
            raise ValueError
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        now = int(time.time())
        if not isinstance(payload, dict):
            raise ValueError
        ticket_kind = payload.get("kind")
        user_id = payload.get("uid")
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        maximum_ttl = _TICKET_TTL_LIMITS.get(ticket_kind)
        if (
            not isinstance(user_id, str)
            or not user_id
            or not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
            or maximum_ttl is None
            or issued_at > now + 30
            or expires_at <= now
            or expires_at - issued_at < 1
            or expires_at - issued_at > maximum_ttl
        ):
            raise ValueError
        if kind and ticket_kind != kind:
            raise ValueError
        for claim, expected_value in (expected_claims or {}).items():
            if payload.get(claim) != expected_value:
                raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError, binascii.Error):
        raise HTTPException(401, "Invalid or expired access ticket.")


def account_from_access(
    request: Request, kind: str = "resource", **expected_claims
) -> dict:
    authenticated = optional_account(request)
    if authenticated:
        return authenticated[0]
    payload = read_ticket(
        request.query_params.get("access"), kind, expected_claims or None
    )
    route_entity = request.path_params.get("entity_id")
    claimed_entity = payload.get("entity")
    if claimed_entity is not None and route_entity is not None:
        if claimed_entity != route_entity:
            raise HTTPException(401, "Invalid or expired access ticket.")
    rows = Account().db.read_execute(
        "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE id=?",
        (payload.get("uid"),),
    )
    if not rows or rows[0][4]:
        raise HTTPException(401, "Account is unavailable.")
    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise HTTPException(401, "Invalid or expired access ticket.")
    session_rows = Account().db.read_execute(
        "SELECT 1 FROM user_sessions WHERE id=? AND user_id=? AND expires_at>?",
        (session_id, payload["uid"], _iso_now()),
    )
    if not session_rows:
        raise HTTPException(401, "Invalid or expired access ticket.")
    route_session_id = request.path_params.get("session_id")
    if claimed_entity is not None and route_session_id is not None:
        sessions = Account().db.read_execute(
            "SELECT 1 FROM playback_sessions WHERE id=? AND user_id=? AND entity_id=?",
            (route_session_id, payload["uid"], claimed_entity),
        )
        if not sessions:
            raise HTTPException(401, "Invalid or expired access ticket.")
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
    if not rows or rows[0][4]:
        return None
    session_id = payload.get("sessionId")
    valid_session = Account().db.read_execute(
        "SELECT 1 FROM user_sessions WHERE id=? AND user_id=? AND expires_at>?",
        (session_id, payload.get("uid"), _iso_now()),
    )
    return Account._public(rows[0]) if valid_session else None
