from hashlib import sha256
import os
from threading import RLock
import time

import requests


# Syncplay can produce several authenticated commands in a second (presence,
# seek, pause, and recovery). Re-checking /Users/Me for each one puts a remote
# Jellyfin request directly in the interaction path. Keep successful checks
# briefly, without retaining the raw token, so revoked tokens are still
# revalidated promptly.
AUTH_CACHE_TTL_SECONDS = 30
_auth_cache: dict[bytes, tuple[float, str]] = {}
_auth_cache_lock = RLock()


def _build_auth_header(token: str) -> dict:
    """Build the authorization header."""
    return {
        "X-Emby-Token": token,
        "Authorization": (
            f'MediaBrowser Token="{token}", Client="ZenStream Orchestrator", '
            'Device="ZenStream Server", DeviceId="Orchestrator", Version="0.0.1b"'
        ),
    }


def authenticated_user_id(token: str) -> str | None:
    token_key = sha256(token.encode()).digest()
    now = time.monotonic()
    with _auth_cache_lock:
        cached = _auth_cache.get(token_key)
        if cached and cached[0] > now:
            return cached[1]

    base_url = os.getenv("JELLYFIN_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("Environment variable `JELLYFIN_URL` not set")
    try:
        response = requests.get(
            f"{base_url}/Users/Me",
            headers=_build_auth_header(token),
            timeout=5,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        user_id = response.json().get("Id")
    except (ValueError, AttributeError):
        return None
    if not isinstance(user_id, str) or not user_id:
        return None

    with _auth_cache_lock:
        _auth_cache[token_key] = (now + AUTH_CACHE_TTL_SECONDS, user_id)
        if len(_auth_cache) > 1024:
            expired = [
                key for key, (expires_at, _) in _auth_cache.items() if expires_at <= now
            ]
            for key in expired:
                _auth_cache.pop(key, None)
            overflow = len(_auth_cache) - 1024
            if overflow > 0:
                oldest = sorted(_auth_cache, key=lambda key: _auth_cache[key][0])[
                    :overflow
                ]
                for key in oldest:
                    _auth_cache.pop(key, None)
    return user_id
