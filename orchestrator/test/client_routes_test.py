import asyncio
import json
import tempfile
import unittest
from pathlib import Path
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


class ClientCatalogPerformanceRouteTest(unittest.TestCase):
    def test_card_view_is_additive_and_does_not_mutate_legacy_payload(self):
        item = {
            "id": "episode-1",
            "name": "Episode 1",
            "type": "episode",
            "metadata": {
                "title": "Episode 1",
                "overview": "Long synopsis",
                "genres": ["Drama"],
                "images": {
                    "Primary": {"url": "/primary"},
                    "Backdrop": {"url": "/backdrop"},
                },
                "credits": {"cast": [{"name": "Actor"}], "crew": []},
            },
            "userState": {"favorite": False},
        }
        payload = {
            "items": [item, {**item, "id": "episode-2"}],
            "total": 2,
        }

        self.assertIs(client_routes._catalog_response(payload, None), payload)
        compact = client_routes._catalog_response(payload, "card", limit=1)

        self.assertEqual(compact["total"], 2)
        self.assertEqual([value["id"] for value in compact["items"]], ["episode-1"])
        self.assertEqual(
            compact["items"][0]["metadata"],
            {
                "title": "Episode 1",
                "genres": ["Drama"],
                "images": {"Primary": {"url": "/primary"}},
            },
        )
        self.assertIn("overview", item["metadata"])
        self.assertIn("Backdrop", item["metadata"]["images"])

    def test_home_limit_applies_to_each_nested_catalog_row(self):
        def item(entity_id: str) -> dict:
            return {"id": entity_id, "metadata": {}, "userState": {}}

        payload = {
            "latestItems": [item("one"), item("two")],
            "libraryRows": [
                {"libraryId": "library", "items": [item("three"), item("four")]}
            ],
        }

        compact = client_routes._catalog_response(payload, "card", limit=1)

        self.assertEqual([value["id"] for value in compact["latestItems"]], ["one"])
        self.assertEqual(
            [value["id"] for value in compact["libraryRows"][0]["items"]],
            ["three"],
        )

    def test_invalid_catalog_view_is_rejected(self):
        with self.assertRaises(client_routes.HTTPException) as raised:
            client_routes._catalog_response({}, "summary")
        self.assertEqual(raised.exception.status_code, 400)

    def test_bootstrap_returns_session_and_preferences_from_foreground_workers(self):
        request = _json_request({}, method="GET", path="/api/auth/bootstrap")
        preference = MagicMock()
        preference.locale.return_value = "en-GB"
        preference.metadata_language.return_value = {"language": "ja"}
        preference.subtitle_style.return_value = {"fontSize": 32}
        languages = MagicMock()
        languages.get.return_value = ["en", "ja"]
        with (
            patch.object(
                client_routes,
                "require_account",
                return_value=({"id": "user-1", "username": "viewer"}, "token"),
            ),
            patch.object(client_routes, "session_id_for_token", return_value="session"),
            patch.object(client_routes, "AccountPreference", return_value=preference),
            patch.object(
                client_routes, "MetadataLanguageSettings", return_value=languages
            ),
            patch.object(
                client_routes, "issue_ticket", return_value="resource-ticket"
            ) as ticket_issuer,
        ):
            response = asyncio.run(client_routes.auth_bootstrap(request))

        self.assertEqual(response["resourceTicket"], "resource-ticket")
        self.assertEqual(response["metadataLanguage"], {"language": "ja"})
        self.assertEqual(response["languages"], ["en", "ja"])
        ticket_issuer.assert_called_once_with(
            "user-1",
            "resource",
            client_routes.RESOURCE_TICKET_TTL_SECONDS,
            sessionId="session",
        )

    def test_versioned_cached_image_uses_stored_version_without_rehashing(self):
        request = _json_request(
            {},
            method="GET",
            path="/api/catalog/items/item/images/Primary",
        )
        request.scope["query_string"] = b"language=en&v=stored-version"
        database = MagicMock()

        def execute(query, _params=None):
            if "SELECT directory FROM libraries" in query:
                return [(None,)]
            if "FROM catalog_artwork_selection" in query:
                return [(str(image_path), "stored-version")]
            raise AssertionError(query)

        database.execute.side_effect = execute
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "image.webp"
            image_path.write_bytes(b"webp")
            with (
                patch.object(
                    client_routes,
                    "account_from_access",
                    return_value={"id": "user-1"},
                ),
                patch.object(
                    client_routes.catalog,
                    "require_entity",
                    return_value=("item", "library", None, "movie"),
                ),
                patch.object(client_routes.catalog, "_has_table", return_value=True),
                patch.object(client_routes.catalog, "db", database),
                patch.object(
                    client_routes.hashlib,
                    "sha256",
                    side_effect=AssertionError("request-time image hash"),
                ),
            ):
                response = asyncio.run(
                    client_routes.item_image("item", "Primary", request, "en")
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response.headers["cache-control"])


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
                with (
                    self.subTest(path=path),
                    self.assertRaisesRegex(
                        client_routes.HTTPException, "A JSON object is required"
                    ) as raised,
                ):
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
