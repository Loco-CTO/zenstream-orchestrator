import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from api.zenstream import client_routes
from starlette.requests import Request


def _json_request(payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/auth/browser-login",
            "query_string": b"",
            "headers": [
                (b"host", b"example.test"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
        },
        receive,
    )


class ClientBrowserLoginRouteTest(unittest.TestCase):
    def setUp(self):
        client_routes._RATE_LIMIT_EVENTS.clear()

    def test_success_returns_user_and_secure_session_cookie(self):
        account = {"id": "user-1", "username": "viewer"}
        account_model = MagicMock()
        account_model.authenticate_password.return_value = account
        account_model.create_session.return_value = {"token": "opaque-session"}

        with patch.object(client_routes, "Account", return_value=account_model):
            response = asyncio.run(
                client_routes.browser_login(
                    _json_request({"username": "viewer", "password": "secret"})
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"user": account})
        cookie = response.headers["set-cookie"]
        self.assertIn("__Host-zenstream-session=opaque-session", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)
        account_model.authenticate_password.assert_called_once_with("viewer", "secret")
        account_model.create_session.assert_called_once_with("user-1")


if __name__ == "__main__":
    unittest.main()
