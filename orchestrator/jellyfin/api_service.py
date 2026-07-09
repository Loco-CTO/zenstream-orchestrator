import os

import requests


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
    return user_id if isinstance(user_id, str) and user_id else None
