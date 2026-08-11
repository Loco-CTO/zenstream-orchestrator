import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from api.zenstream import application_routes as app_module
from fastapi import HTTPException
from starlette.requests import Request


def _request(
    method: str = "GET",
    *,
    cookie: str | None = None,
    bearer: str | None = None,
    participant: str | None = "browser-tab",
) -> Request:
    headers = [(b"host", b"example.test")]
    if cookie:
        headers.append(
            (b"cookie", f"__Host-zenstream-session={cookie}".encode("ascii"))
        )
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode("ascii")))
    if participant:
        headers.append((b"x-zenstream-participant", participant.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/api/syncplay/groups",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
        }
    )


class SyncplayRouteAuthenticationTest(unittest.TestCase):
    def test_browser_cookie_can_list_groups(self):
        account_model = MagicMock()
        account_model.authenticate_token.return_value = {
            "id": "user-1",
            "username": "Viewer",
        }
        syncplay_model = MagicMock()
        syncplay_model.db.execute.return_value = []

        with (
            patch("app.client_auth.Account", return_value=account_model),
            patch.object(app_module, "SyncplayGroup", return_value=syncplay_model),
        ):
            result = asyncio.run(
                app_module.syncplay_groups(_request(cookie="browser-session"))
            )

        self.assertEqual(result, {"groups": []})
        account_model.authenticate_token.assert_called_once_with("browser-session")

    def test_browser_cookie_can_create_group(self):
        account_model = MagicMock()
        account_model.authenticate_token.return_value = {
            "id": "user-1",
            "username": "Viewer",
        }
        directory_model = MagicMock()
        directory_model._row.return_value = ("user-1", "Viewer")
        group = MagicMock()
        group.state.return_value = {"id": "group-1", "members": []}

        with (
            patch("app.client_auth.Account", return_value=account_model),
            patch.object(app_module, "Account", return_value=directory_model),
            patch.object(
                app_module.SyncplayGroup, "create", return_value=group
            ) as create,
            patch.object(app_module.hub, "broadcast", new=AsyncMock()) as broadcast,
        ):
            response = asyncio.run(
                app_module.syncplay_create(
                    _request(method="POST", cookie="browser-session")
                )
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(json.loads(response.body), {"id": "group-1", "members": []})
        create.assert_called_once_with("user-1", "browser-tab", "Viewer")
        broadcast.assert_awaited_once()

    def test_bearer_client_remains_supported(self):
        account_model = MagicMock()
        account_model.authenticate_token.return_value = {
            "id": "user-1",
            "username": "Viewer",
        }
        syncplay_model = MagicMock()
        syncplay_model.db.execute.return_value = []

        with (
            patch("app.client_auth.Account", return_value=account_model),
            patch.object(app_module, "SyncplayGroup", return_value=syncplay_model),
        ):
            result = asyncio.run(
                app_module.syncplay_groups(_request(bearer="api-token"))
            )

        self.assertEqual(result, {"groups": []})
        account_model.authenticate_token.assert_called_once_with("api-token")

    def test_invalid_authentication_is_rejected(self):
        account_model = MagicMock()
        account_model.authenticate_token.return_value = None
        with patch("app.client_auth.Account", return_value=account_model):
            with self.assertRaises(HTTPException) as raised:
                app_module._sync_identity(_request(cookie="invalid-session"))
        self.assertEqual(raised.exception.status_code, 401)

    def test_missing_participant_is_rejected(self):
        account_model = MagicMock()
        account_model.authenticate_token.return_value = {
            "id": "user-1",
            "username": "Viewer",
        }
        with patch("app.client_auth.Account", return_value=account_model):
            with self.assertRaises(HTTPException) as raised:
                app_module._sync_identity(
                    _request(cookie="browser-session", participant=None)
                )
        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
