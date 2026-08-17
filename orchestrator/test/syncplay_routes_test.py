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


class _HubWebSocket:
    def __init__(self):
        self.messages = []
        self.closed = []

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.messages.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


class SyncplayWebSocketHubTest(unittest.TestCase):
    def test_replaces_duplicate_socket_for_same_participant(self):
        async def scenario():
            hub = app_module.WebSocketHub()
            old = _HubWebSocket()
            new = _HubWebSocket()
            await hub.connect(old, "user-1", "tab-1")
            await hub.connect(new, "user-1", "tab-1")
            await hub.broadcast({"version": 1, "type": "group", "group": {"id": "group-1"}})
            await asyncio.sleep(0)
            try:
                self.assertEqual(await hub.sockets_for("user-1", "tab-1"), (new,))
                self.assertEqual(old.messages, [])
                self.assertEqual(len(new.messages), 1)
                self.assertEqual(old.closed, [(1000, "Replaced")])
            finally:
                await hub.remove(new)

        asyncio.run(scenario())

    def test_keeps_different_participants_independent(self):
        async def scenario():
            hub = app_module.WebSocketHub()
            first = _HubWebSocket()
            second = _HubWebSocket()
            await hub.connect(first, "user-1", "tab-1")
            await hub.connect(second, "user-1", "tab-2")
            await hub.broadcast({"version": 1, "type": "group-ended", "id": "group-1"})
            await asyncio.sleep(0)
            try:
                self.assertEqual(len(first.messages), 1)
                self.assertEqual(len(second.messages), 1)
                self.assertEqual(await hub.sockets_for("user-1", "tab-1"), (first,))
                self.assertEqual(await hub.sockets_for("user-1", "tab-2"), (second,))
            finally:
                await hub.remove(first)
                await hub.remove(second)

        asyncio.run(scenario())


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
        group = MagicMock()
        group.state.return_value = {"id": "group-1", "members": []}

        with (
            patch("app.client_auth.Account", return_value=account_model),
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

    def test_join_uses_authenticated_username_when_client_omits_username_header(self):
        account_model = MagicMock()
        account_model.authenticate_token.return_value = {
            "id": "user-1",
            "username": "anson",
        }
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        state = {"id": "group-1", "revision": 0, "members": []}
        group = MagicMock()
        group.state.return_value = state

        def mutate(user, expected_revision, operation_id, apply):
            apply(cursor, state)
            return state

        group.mutate.side_effect = mutate

        async def run_control_inline(function, *args, **kwargs):
            if function is app_module.SyncplayGroup:
                return group
            return function(*args, **kwargs)

        with (
            patch("app.client_auth.Account", return_value=account_model),
            patch.object(app_module, "run_control", side_effect=run_control_inline),
            patch.object(app_module.hub, "broadcast", new=AsyncMock()) as broadcast,
        ):
            response = asyncio.run(
                app_module.syncplay_join(
                    "group-1", _request(method="POST", cookie="browser-session")
                )
            )

        self.assertIs(response, state)
        self.assertIn(
            (
                "UPDATE syncplay_members SET username=? WHERE group_id=? AND user_id=? AND participant_id=?",
                ("anson", "group-1", "user-1", "browser-tab"),
            ),
            [call.args for call in cursor.execute.call_args_list],
        )
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
