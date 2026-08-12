import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from api.zenstream import client_routes
from starlette.requests import Request


def _json_request(
    payload: object,
    *,
    scheme: str = "https",
    host: str = "example.test",
    method: str = "POST",
    path: str = "/api/auth/browser-login",
) -> Request:
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
            "method": method,
            "scheme": scheme,
            "path": path,
            "query_string": b"",
            "headers": [
                (b"host", host.encode("ascii")),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
            "server": (host, 443 if scheme == "https" else 80),
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

    def test_loopback_login_sets_port_scoped_cookie_and_expires_legacy_cookie(self):
        account = {"id": "user-1", "username": "viewer"}
        account_model = MagicMock()
        account_model.authenticate_password.return_value = account
        account_model.create_session.return_value = {"token": "opaque-session"}

        with patch.object(client_routes, "Account", return_value=account_model):
            response = asyncio.run(
                client_routes.browser_login(
                    _json_request(
                        {"username": "viewer", "password": "secret"},
                        scheme="http",
                        host="127.0.0.1:9098",
                    )
                )
            )

        cookies = [
            value.decode("latin-1")
            for name, value in response.raw_headers
            if name.lower() == b"set-cookie"
        ]
        self.assertTrue(
            any("zenstream-session-9098=opaque-session" in value for value in cookies)
        )
        self.assertTrue(
            any(
                value.startswith('zenstream-session=""') and "Max-Age=0" in value
                for value in cookies
            )
        )
        scoped = next(value for value in cookies if "opaque-session" in value)
        self.assertNotIn("Secure", scoped)

    def test_logout_expires_scoped_production_and_legacy_cookie_names(self):
        request = _json_request(
            {},
            scheme="http",
            host="localhost:9098",
            path="/api/auth/logout",
        )
        account_model = MagicMock()
        with (
            patch.object(
                client_routes,
                "require_account",
                return_value=({"id": "user-1"}, "opaque-session"),
            ),
            patch.object(client_routes, "Account", return_value=account_model),
        ):
            response = asyncio.run(client_routes.logout(request))

        cookies = [
            value.decode("latin-1")
            for name, value in response.raw_headers
            if name.lower() == b"set-cookie"
        ]
        for name in (
            "zenstream-session-9098",
            "__Host-zenstream-session",
            "zenstream-session",
        ):
            self.assertTrue(
                any(value.startswith(f'{name}=""') for value in cookies),
                name,
            )
        account_model.revoke.assert_called_once_with("opaque-session")


class ClientPreferenceRouteTest(unittest.TestCase):
    def test_patch_routes_reject_non_object_json(self):
        routes = (
            (client_routes.set_locale, "/api/preferences/locale"),
            (
                client_routes.set_metadata_language,
                "/api/preferences/metadata-language",
            ),
            (client_routes.set_subtitles, "/api/preferences/subtitles"),
        )
        with patch.object(
            client_routes,
            "require_account",
            return_value=({"id": "user-1"}, "token"),
        ):
            for route, path in routes:
                with self.subTest(path=path), self.assertRaisesRegex(
                    client_routes.HTTPException, "A JSON object is required"
                ) as raised:
                    asyncio.run(
                        route(
                            _json_request(
                                [],
                                method="PATCH",
                                path=path,
                            )
                        )
                    )
                self.assertEqual(raised.exception.status_code, 400)

    def test_patch_routes_reject_oversized_json_before_persistence(self):
        request = _json_request(
            {"locale": "x" * client_routes.AUTH_BODY_LIMIT_BYTES},
            method="PATCH",
            path="/api/preferences/locale",
        )
        preference = MagicMock()
        with (
            patch.object(
                client_routes,
                "require_account",
                return_value=({"id": "user-1"}, "token"),
            ),
            patch.object(client_routes, "AccountPreference", return_value=preference),
            self.assertRaises(client_routes.HTTPException) as raised,
        ):
            asyncio.run(client_routes.set_locale(request))

        self.assertEqual(raised.exception.status_code, 413)
        preference.set_locale.assert_not_called()


if __name__ == "__main__":
    unittest.main()
