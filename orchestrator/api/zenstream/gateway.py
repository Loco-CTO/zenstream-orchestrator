"""ZenStream-owned content, asset, and media gateway endpoints.

The public API deliberately uses ZenStream route names. Jellyfin remains an
implementation detail of the gateway and is never exposed in returned URLs.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from jellyfin.api_service import authenticated_user_id


router = APIRouter(prefix="/api")
_client: httpx.AsyncClient | None = None
_token_vault: dict[str, tuple[str, float]] = {}
_vault_lock = asyncio.Lock()
_asset_limit = asyncio.Semaphore(8)
# Resource tickets are used by native playback engines that cannot add request
# headers. Keep them aligned with the in-memory token lease so a logged-in
# mobile session does not lose its fallback stream URL after a short idle.
_RESOURCE_TTL = 6 * 60 * 60
_PLAYBACK_TTL = 6 * 60 * 60
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_PASS_RESPONSE_HEADERS = {
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
}


@asynccontextmanager
async def gateway_lifespan():
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=25),
        follow_redirects=True,
    )
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


def _base_url() -> str:
    value = os.getenv("JELLYFIN_URL", "").rstrip("/")
    if not value:
        raise HTTPException(503, "Jellyfin server is not configured.")
    return value


def _secret() -> bytes:
    value = os.getenv("SECRET_KEY", "")
    if not value:
        raise HTTPException(503, "Orchestrator secret is not configured.")
    return value.encode()


def _encode(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(_secret(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _decode(value: str) -> dict:
    try:
        encoded, supplied = value.split(".", 1)
        expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
        if not hmac.compare_digest(expected, actual):
            raise ValueError
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if not isinstance(payload, dict) or float(payload.get("exp", 0)) <= time.time():
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
        raise HTTPException(401, "Invalid or expired resource ticket.")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _remember_token(token: str) -> None:
    async with _vault_lock:
        _token_vault[_token_hash(token)] = (token, time.time() + _PLAYBACK_TTL)
        expired = [key for key, (_, expiry) in _token_vault.items() if expiry <= time.time()]
        for key in expired:
            _token_vault.pop(key, None)


async def _token_from_ticket(ticket: str) -> tuple[str, dict]:
    payload = _decode(ticket)
    token_hash = payload.get("th")
    if not isinstance(token_hash, str):
        raise HTTPException(401, "Invalid resource ticket.")
    async with _vault_lock:
        stored = _token_vault.get(token_hash)
    if not stored or stored[1] <= time.time():
        raise HTTPException(401, "Resource ticket is no longer available.")
    return stored[0], payload


async def _authenticate(
    x_jellyfin_token: str | None,
    access: str | None = None,
) -> tuple[str, str]:
    token = x_jellyfin_token
    ticket_payload = _decode(access) if access else None
    if not token and access:
        token, ticket_payload = await _token_from_ticket(access)
    if not token:
        raise HTTPException(401, "Authentication required.")
    if ticket_payload and ticket_payload.get("th") != _token_hash(token):
        raise HTTPException(403, "Resource ticket does not belong to this token.")
    await _remember_token(token)
    user_id = await asyncio.to_thread(authenticated_user_id, token)
    if not user_id:
        raise HTTPException(401, "Invalid token.")
    if ticket_payload and ticket_payload.get("uid") and ticket_payload.get("uid") != user_id:
        raise HTTPException(403, "Resource ticket does not belong to this user.")
    return token, user_id


def _auth_headers(token: str, request: Request, *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "Accept": request.headers.get("accept", "application/json"),
        "Authorization": (
            f'MediaBrowser Token="{token}", Client="ZenStream Orchestrator", '
            'Device="ZenStream Server", DeviceId="Orchestrator", Version="0.0.1b"'
        ),
        "X-Emby-Token": token,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _query(request: Request, user_id: str | None = None) -> list[tuple[str, str]]:
    values = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key.lower() not in {"userid", "access", "lease"}
    ]
    if user_id:
        values.append(("userId", user_id))
    return values


def _upstream_url(path: str, query: list[tuple[str, str]] | None = None) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{_base_url()}{path}" + (f"?{urlencode(query)}" if query else "")


async def _upstream_json(
    request: Request,
    token: str,
    path: str,
    *,
    method: str = "GET",
    query: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    user_id: str | None = None,
) -> Response:
    if _client is None:
        raise HTTPException(503, "Gateway is not ready.")
    if body and method in {"POST", "PATCH", "PUT"}:
        try:
            decoded = json.loads(body)
            if isinstance(decoded, dict) and user_id:
                decoded["UserId"] = user_id
                body = json.dumps(decoded).encode()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    upstream = await _client.request(
        method,
        _upstream_url(path, query),
        headers=_auth_headers(token, request, json_body=body is not None),
        content=body,
    )
    content = upstream.content
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP
        and key.lower() not in {"content-encoding", "content-length"}
    }
    return Response(content, status_code=upstream.status_code, headers=response_headers)


async def _request_body(request: Request) -> bytes | None:
    body = await request.body()
    return body or None


@router.post("/auth/login")
async def login(request: Request):
    if _client is None:
        raise HTTPException(503, "Gateway is not ready.")
    body = await request.body()
    try:
        credentials = json.loads(body or b"{}")
        username = str(credentials.get("username", credentials.get("Username", ""))).strip()
        password = str(credentials.get("password", credentials.get("Pw", "")))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise HTTPException(400, "Invalid login request.")
    if not username or not password:
        raise HTTPException(400, "Username and password are required.")
    upstream = await _client.post(
        _upstream_url("/Users/AuthenticateByName"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": (
                'MediaBrowser Client="ZenStream", Device="ZenStream Server", '
                'DeviceId="Orchestrator", Version="0.0.1b"'
            ),
        },
        json={"Username": username, "Pw": password},
    )
    if upstream.status_code >= 400:
        return Response(upstream.content, status_code=upstream.status_code, headers={"Content-Type": "application/json"})
    payload = upstream.json()
    token = payload.get("AccessToken") if isinstance(payload, dict) else None
    user = payload.get("User") if isinstance(payload, dict) else None
    user_id = user.get("Id") if isinstance(user, dict) else None
    if isinstance(token, str) and isinstance(user_id, str):
        await _remember_token(token)
        payload["ResourceTicket"] = _encode({
            "uid": user_id,
            "th": _token_hash(token),
            "exp": time.time() + _RESOURCE_TTL,
            "kind": "resource",
        })
        payload["ResourceTicketExpiresAt"] = int(time.time() + _RESOURCE_TTL)
    return JSONResponse(payload, status_code=upstream.status_code)


@router.get("/auth/resource-ticket")
async def resource_ticket(
    x_jellyfin_token: str | None = Header(None),
):
    token, user_id = await _authenticate(x_jellyfin_token)
    expiry = int(time.time() + _RESOURCE_TTL)
    return {"ticket": _encode({"uid": user_id, "th": _token_hash(token), "exp": expiry, "kind": "resource"}), "expiresAt": expiry}


async def _forward_content(request: Request, path: str, *, add_user: bool = True, method: str = "GET"):
    token, user_id = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    return await _upstream_json(
        request,
        token,
        path,
        method=method,
        query=_query(request, user_id if add_user else None),
        body=await _request_body(request) if method != "GET" else None,
        user_id=user_id,
    )


@router.get("/content/items")
async def content_items(request: Request):
    return await _forward_content(request, "/Items")


@router.get("/content/items/{item_id}")
async def content_item(item_id: str, request: Request):
    return await _forward_content(request, f"/Items/{item_id}", add_user=False)


@router.get("/content/items/{item_id}/trickplay")
async def content_trickplay(item_id: str, request: Request):
    return await _forward_content(request, f"/Items/{item_id}", add_user=False)


@router.get("/content/items/{item_id}/similar")
async def content_similar(item_id: str, request: Request):
    return await _forward_content(request, f"/Items/{item_id}/Similar")


@router.get("/content/items/{item_id}/local-trailers")
async def content_local_trailers(item_id: str, request: Request):
    return await _forward_content(request, f"/Items/{item_id}/LocalTrailers", add_user=False)


@router.get("/content/views")
async def content_views(request: Request):
    token, user_id = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    return await _upstream_json(request, token, f"/Users/{user_id}/Views", query=_query(request))


@router.get("/content/resume")
async def content_resume(request: Request):
    return await _forward_content(request, "/UserItems/Resume")


@router.get("/content/next-up")
async def content_next_up(request: Request):
    return await _forward_content(request, "/Shows/NextUp")


@router.get("/content/search")
async def content_search(request: Request):
    return await _forward_content(request, "/Items")


@router.get("/content/shows/{series_id}/seasons")
async def content_seasons(series_id: str, request: Request):
    return await _forward_content(request, f"/Shows/{series_id}/Seasons")


@router.get("/content/shows/{series_id}/episodes")
async def content_episodes(series_id: str, request: Request):
    return await _forward_content(request, f"/Shows/{series_id}/Episodes")


@router.post("/user/items/{item_id}/favorite")
@router.delete("/user/items/{item_id}/favorite")
async def favorite_item(item_id: str, request: Request):
    return await _forward_content(request, f"/UserFavoriteItems/{item_id}", add_user=False, method=request.method)


@router.post("/user/items/{item_id}/played")
@router.delete("/user/items/{item_id}/played")
async def played_item(item_id: str, request: Request):
    return await _forward_content(request, f"/UserPlayedItems/{item_id}", add_user=False, method=request.method)


@router.post("/playback/progress")
async def playback_progress(request: Request):
    return await _forward_content(request, "/Sessions/Playing/Progress", add_user=False, method="POST")


@router.get("/playback/markers/{item_id}")
async def playback_markers(item_id: str, request: Request):
    token, _ = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    for path in (
        f"/Episode/{item_id}/IntroSkipperSegments",
        f"/Episode/{item_id}/Timestamps",
        f"/MediaSegments/{item_id}",
    ):
        if _client is None:
            break
        response = await _client.get(_upstream_url(path), headers=_auth_headers(token, request))
        if response.status_code < 400:
            return Response(response.content, status_code=response.status_code, headers={"Content-Type": response.headers.get("content-type", "application/json")})
    raise HTTPException(404, "Playback markers were not found.")


def _asset_ticket(token: str, user_id: str, *, path: str | None = None, query: list[tuple[str, str]] | None = None) -> str:
    return _encode({
        "uid": user_id,
        "th": _token_hash(token),
        "exp": time.time() + _PLAYBACK_TTL if path else time.time() + _RESOURCE_TTL,
        "kind": "media" if path else "resource",
        "path": path,
        "query": query or [],
    })


async def _media_ticket(request: Request, lease: str | None) -> tuple[str, dict]:
    access = lease or request.query_params.get("access")
    # Normal gateway authentication returns the user id. Media URLs also
    # need the decoded lease to resolve their Jellyfin path and query.
    payload = _decode(access) if access else {"kind": "resource"}
    token, user_id = await _authenticate(request.headers.get("x-jellyfin-token"), access)
    if payload.get("kind") not in {"media", "resource"}:
        raise HTTPException(401, "Invalid media ticket.")
    if payload.get("uid") and payload.get("uid") != user_id:
        raise HTTPException(403, "Resource ticket does not belong to this user.")
    return token, payload


def _proxied_manifest_uri(item_id: str, token: str, upstream_url: str, uri: str) -> str:
    absolute = urljoin(upstream_url, uri)
    parsed = urlsplit(absolute)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"api_key", "apikey"}
    ]
    lease = _asset_ticket(token, "", path=parsed.path, query=query)
    return f"/api/video/{item_id}/stream?lease={lease}"


def _rewrite_manifest(item_id: str, token: str, upstream_url: str, content: bytes) -> bytes:
    text = content.decode("utf-8", errors="replace")
    uri_pattern = re.compile(r'URI="([^"]+)"')
    rewritten: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("#"):
            line = uri_pattern.sub(
                lambda match: f'URI="{_proxied_manifest_uri(item_id, token, upstream_url, match.group(1))}"',
                line,
            )
        elif line.strip():
            prefix = line[: len(line) - len(line.lstrip())]
            suffix = line[len(line.rstrip("\r\n")) :]
            line = prefix + _proxied_manifest_uri(item_id, token, upstream_url, line.strip()) + suffix
        rewritten.append(line)
    return "".join(rewritten).encode()


async def _stream_response(
    request: Request,
    token: str,
    upstream_url: str,
    *,
    manifest_item_id: str | None = None,
) -> Response:
    if _client is None:
        raise HTTPException(503, "Gateway is not ready.")
    headers = _auth_headers(token, request)
    # Do not reuse an upstream keep-alive socket for long-lived media reads.
    # The Jellyfin edge occasionally closes a reused socket before the first
    # chunk, which appears to the browser as an incomplete chunked response.
    headers["Connection"] = "close"
    for name in ("range", "if-range", "if-none-match", "if-modified-since"):
        if value := request.headers.get(name):
            headers[name.title()] = value
    upstream_context = _client.stream(request.method, upstream_url, headers=headers)
    upstream = await upstream_context.__aenter__()
    if upstream.status_code >= 400:
        content = await upstream.aread()
        await upstream.aclose()
        return Response(content, status_code=upstream.status_code, headers={"Content-Type": upstream.headers.get("content-type", "application/json")})

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in _PASS_RESPONSE_HEADERS and key.lower() not in _HOP_BY_HOP
    }
    # A decoded response may not retain the upstream wire length. When there
    # is no content encoding, however, Jellyfin's length is the exact byte
    # count that the streaming iterator will yield. Keeping it avoids an
    # unnecessary chunked wrapper around large media responses.
    if "content-encoding" not in upstream.headers:
        if content_length := upstream.headers.get("content-length"):
            response_headers["content-length"] = content_length
    else:
        response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "").lower()
    if manifest_item_id and ("mpegurl" in content_type or ".m3u8" in upstream_url.lower()):
        manifest = await upstream.aread()
        await upstream.aclose()
        response_headers.pop("content-length", None)
        return Response(
            _rewrite_manifest(manifest_item_id, token, upstream_url, manifest),
            status_code=upstream.status_code,
            headers=response_headers,
        )

    # HLS media segments are finite (normally a few hundred KB), but some
    # Jellyfin edge responses close a no-range streaming socket before the
    # first chunk reaches the ASGI server. Buffering only these finite HLS
    # segments keeps playlist playback reliable without buffering full-file
    # direct playback, which continues to use range streaming below.
    if manifest_item_id and content_type in {
        "video/mp2t",
        "video/mp4",
        "audio/mp4",
        "audio/aac",
        "audio/mpeg",
    }:
        segment = await upstream.aread()
        await upstream.aclose()
        response_headers.pop("content-length", None)
        return Response(segment, status_code=upstream.status_code, headers=response_headers)

    async def body() -> AsyncIterator[bytes]:
        try:
            if request.method != "HEAD":
                async for chunk in upstream.aiter_raw(64 * 1024):
                    yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers)


async def _buffered_asset_response(request: Request, token: str, upstream_url: str) -> Response:
    """Fetch assets completely before sending them to the browser.

    Jellyfin/CDN image responses can close an idle keep-alive connection while
    the browser is loading a large batch. Passing that half-read response
    through as a streaming response turns the failure into a broken image.
    Assets are small enough to buffer, and a bounded retry makes concurrent
    home/library grids resilient to a transient upstream connection close.
    """
    if _client is None:
        raise HTTPException(503, "Gateway is not ready.")
    headers = _auth_headers(token, request)
    headers["Accept-Encoding"] = "identity"
    for name in ("if-none-match", "if-modified-since"):
        if value := request.headers.get(name):
            headers[name.title()] = value

    async with _asset_limit:
        for attempt in range(3):
            try:
                upstream = await _client.get(upstream_url, headers=headers)
                response_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower() in _PASS_RESPONSE_HEADERS
                    and key.lower() not in {"content-encoding", "content-length"}
                    and key.lower() not in _HOP_BY_HOP
                }
                return Response(
                    upstream.content,
                    status_code=upstream.status_code,
                    headers=response_headers,
                )
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.DecodingError):
                if attempt == 2:
                    raise HTTPException(502, "The upstream asset could not be loaded.")
                await asyncio.sleep(0.1 * (attempt + 1))


@router.get("/assets/items/{item_id}/images/{image_type}")
async def item_image(item_id: str, image_type: str, request: Request):
    token, _ = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    index = request.query_params.get("index", "")
    path = f"/Items/{item_id}/Images/{image_type}{('/' + index) if index else ''}"
    return await _buffered_asset_response(request, token, _upstream_url(path, _query(request)))


@router.get("/assets/users/{user_id}/image")
async def user_image(user_id: str, request: Request):
    token, authenticated_user = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    if user_id != authenticated_user:
        raise HTTPException(403, "User image access denied.")
    return await _buffered_asset_response(request, token, _upstream_url(f"/Users/{user_id}/Images/Primary", _query(request)))


@router.get("/assets/people/{person_name}/image")
async def person_image(person_name: str, request: Request):
    token, _ = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    return await _buffered_asset_response(request, token, _upstream_url(f"/Persons/{person_name}/Images/Primary", _query(request)))


@router.get("/video/{item_id}/subtitles/{source_id}/{stream_index}")
async def subtitles(item_id: str, source_id: str, stream_index: int, request: Request):
    token, _ = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    query = [
        (key, value)
        for key, value in _query(request)
        if key.lower() not in {"mediasourceid", "format", "addvtttimemap", "copytimestamps", "startpositionticks"}
    ]
    query.extend([
        ("MediaSourceId", source_id),
        ("format", "vtt"),
        ("addVttTimeMap", request.query_params.get("addVttTimeMap", "false")),
        ("copyTimestamps", request.query_params.get("copyTimestamps", "false")),
        ("startPositionTicks", request.query_params.get("startPositionTicks", "0")),
    ])
    return await _stream_response(request, token, _upstream_url(f"/Videos/{item_id}/{source_id}/Subtitles/{stream_index}/Stream.vtt", query))


@router.get("/video/{item_id}/trickplay/{width}/{tile_index}")
async def trickplay(item_id: str, width: str, tile_index: int, request: Request):
    token, _ = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    return await _buffered_asset_response(request, token, _upstream_url(f"/Videos/{item_id}/Trickplay/{width}/{tile_index}.jpg", _query(request)))


@router.api_route("/video/{item_id}/stream", methods=["GET", "HEAD"])
async def video_stream(item_id: str, request: Request):
    token, payload = await _media_ticket(request, request.query_params.get("lease"))
    if payload.get("path"):
        path = payload["path"]
        query = [(str(key), str(value)) for key, value in payload.get("query", [])]
    else:
        path = f"/Videos/{item_id}/stream"
        query = _query(request)
    query = [(key, value) for key, value in query if key not in {"access", "lease"}]
    return await _stream_response(request, token, _upstream_url(path, query), manifest_item_id=item_id)


def rewrite_playback_urls(value: object, token: str, user_id: str, item_id: str) -> object:
    if isinstance(value, dict):
        result = {key: rewrite_playback_urls(item, token, user_id, item_id) for key, item in value.items()}
        for key in ("DirectStreamUrl", "TranscodingUrl"):
            url = result.get(key)
            if isinstance(url, str) and url:
                parsed = urlsplit(url)
                upstream_path = parsed.path or "/Videos/stream"
                query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in {"api_key", "apikey"}]
                public_query = [("lease", _asset_ticket(token, user_id, path=upstream_path, query=query))]
                public_query.extend(
                    (key, value)
                    for key, value in query
                    if key.lower() in {"starttimeticks", "playsessionid"}
                )
                result[key] = f"/api/video/{item_id}/stream?{urlencode(public_query)}"
        return result
    if isinstance(value, list):
        return [rewrite_playback_urls(item, token, user_id, item_id) for item in value]
    return value


@router.post("/content/items/{item_id}/playback")
async def content_playback_rewrite(item_id: str, request: Request):
    token, user_id = await _authenticate(request.headers.get("x-jellyfin-token"), request.query_params.get("access"))
    response = await _upstream_json(request, token, f"/Items/{item_id}/PlaybackInfo", method="POST", query=_query(request, user_id), body=await _request_body(request), user_id=user_id)
    if response.status_code >= 400:
        return response
    try:
        payload = json.loads(response.body)
        return JSONResponse(rewrite_playback_urls(payload, token, user_id, item_id), status_code=response.status_code)
    except (AttributeError, TypeError, json.JSONDecodeError):
        return response
