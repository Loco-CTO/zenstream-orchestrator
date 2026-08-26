import os
import unittest
from unittest.mock import MagicMock, patch

from app import client_auth
from app.client_auth import issue_ticket, read_ticket
from fastapi import HTTPException
from starlette.requests import Request


def _request(host: str, scheme: str = "http", cookie: str | None = None) -> Request:
    headers = [(b"host", host.encode("ascii"))]
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": scheme,
            "path": "/api/auth/me",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 443 if scheme == "https" else 80),
        }
    )


class ClientTicketTest(unittest.TestCase):
    def setUp(self):
        self.secret = patch.dict(os.environ, {"SECRET_KEY": "ticket-test-secret"})
        self.secret.start()

    def tearDown(self):
        self.secret.stop()

    def test_ticket_requires_matching_claim(self):
        ticket = issue_ticket(
            "user-1",
            "resource",
            60,
            entity="item-1",
            sessionId="session-1",
        )
        self.assertEqual(
            read_ticket(ticket, "resource", {"entity": "item-1"})["uid"],
            "user-1",
        )
        with self.assertRaises(HTTPException):
            read_ticket(ticket, "resource", {"entity": "item-2"})

    def test_reserved_claims_and_excessive_ttl_are_rejected(self):
        with self.assertRaises(ValueError):
            issue_ticket("user-1", "resource", 60, uid="user-2")
        with self.assertRaises(ValueError):
            issue_ticket("user-1", "socket", 61)

    def test_malformed_ticket_is_rejected(self):
        for value in ("", "no-dot", "a.b.c", "a." + "x" * 5000):
            with self.subTest(value=value[:20]), self.assertRaises(HTTPException):
                read_ticket(value, "resource")


class ClientSessionCookieTest(unittest.TestCase):
    def test_loopback_http_cookie_is_scoped_by_port(self):
        for host in (
            "localhost:9098",
            "127.0.0.1:9098",
            "127.42.0.1:9098",
            "[::1]:9098",
        ):
            with self.subTest(host=host):
                request = _request(host)
                self.assertFalse(client_auth.cookie_secure(request))
                self.assertEqual(
                    client_auth.session_cookie_name(request),
                    "zenstream-session-9098",
                )
        self.assertEqual(
            client_auth.session_cookie_name(_request("localhost:9099")),
            "zenstream-session-9099",
        )

    def test_loopback_default_port_and_trailing_dot_are_supported(self):
        request = _request("localhost.")
        self.assertFalse(client_auth.cookie_secure(request))
        self.assertEqual(
            client_auth.session_cookie_name(request), "zenstream-session-80"
        )

    def test_https_and_non_loopback_http_keep_production_cookie(self):
        for request in (_request("localhost:9098", "https"), _request("example.test")):
            with self.subTest(url=str(request.url)):
                self.assertTrue(client_auth.cookie_secure(request))
                self.assertEqual(
                    client_auth.session_cookie_name(request),
                    "__Host-zenstream-session",
                )

    def test_legacy_loopback_cookie_is_accepted_during_migration(self):
        account = {"id": "user-1", "username": "viewer"}
        account_model = MagicMock()
        account_model.authenticate_token.return_value = account
        request = _request("127.0.0.1:9098", cookie="zenstream-session=legacy-token")

        with patch.object(client_auth, "Account", return_value=account_model):
            authenticated, token = client_auth.require_account(request)

        self.assertEqual(authenticated, account)
        self.assertEqual(token, "legacy-token")
        account_model.authenticate_token.assert_called_once_with("legacy-token")


class BrowserOriginTest(unittest.TestCase):
    def test_public_web_url_is_an_additive_normalized_origin(self):
        with patch.dict(
            os.environ,
            {
                "CORS_ORIGINS": "https://extra.example, https://zenstream.example/",
                "ZENSTREAM_PUBLIC_WEB_URL": "HTTPS://ZenStream.Example/",
            },
        ):
            origins = client_auth.browser_origins()

        self.assertIn("https://extra.example", origins)
        self.assertIn("https://zenstream.example", origins)
        self.assertEqual(origins.count("https://zenstream.example"), 1)
        self.assertIn("http://localhost:3000", origins)


if __name__ == "__main__":
    unittest.main()
