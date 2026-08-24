import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _binary_request(
    body: bytes,
    *,
    content_type: str = "image/png",
    declared_length: int | None = None,
    method: str = "POST",
    path: str = "/api/account/avatar",
) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    length = len(body) if declared_length is None else declared_length
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "query_string": b"cropX=0&cropY=0&cropSize=100&rotation=0",
            "headers": [
                (b"host", b"example.test"),
                (b"content-type", content_type.encode("ascii")),
                (b"content-length", str(length).encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
        },
        receive,
    )


class CatalogSocketEventTest(unittest.TestCase):
    def test_scan_events_are_coalesced_but_idle_transition_is_sent(self):
        previous = {"library-1": ("library-1", "ready", "before", 1, "root-1")}
        last_scan_sent = {}
        scanning = {
            "id": "library-1",
            "scanState": "scanning",
            "lastScanFinishedAt": "before",
            "catalogGeneration": 2,
            "lastRootEntityId": "root-2",
        }

        first = client_routes._catalog_update_event(
            previous, scanning, 100.0, last_scan_sent
        )
        self.assertEqual(first["reason"], "scan")
        previous["library-1"] = (
            scanning["id"],
            scanning["scanState"],
            scanning["lastScanFinishedAt"],
            scanning["catalogGeneration"],
            scanning["lastRootEntityId"],
        )
        self.assertIsNone(
            client_routes._catalog_update_event(
                previous,
                {**scanning, "catalogGeneration": 3},
                102.0,
                last_scan_sent,
            )
        )

        ready = {**scanning, "scanState": "ready", "catalogGeneration": 4}
        terminal = client_routes._catalog_update_event(
            previous, ready, 102.0, last_scan_sent
        )
        self.assertEqual(terminal["reason"], "refresh")


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


class ClientAvatarRouteTest(unittest.TestCase):
    def test_upload_uses_raw_body_and_pixel_crop_in_control_lane(self):
        request = _binary_request(b"avatar-bytes")
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            patch.object(
                client_routes,
                "run_control",
                new=AsyncMock(return_value="new-version"),
            ) as control,
        ):
            response = asyncio.run(
                client_routes.upload_avatar(request, 12.5, 24.5, 80.0, 90)
            )

        self.assertEqual(response, {"avatarVersion": "new-version"})
        control.assert_awaited_once()
        arguments = control.await_args.args
        self.assertIs(arguments[0], client_routes._save_avatar)
        self.assertEqual(arguments[1:3], ("user-1", b"avatar-bytes"))
        self.assertEqual(arguments[4].crop_x, 12.5)
        self.assertEqual(arguments[4].crop_y, 24.5)
        self.assertEqual(arguments[4].crop_size, 80.0)
        self.assertEqual(arguments[4].rotation, 90)

    def test_upload_rejects_oversized_content_length_before_processing(self):
        request = _binary_request(
            b"small", declared_length=client_routes.AVATAR_MAX_BYTES + 1
        )
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            self.assertRaises(client_routes.HTTPException) as raised,
        ):
            asyncio.run(client_routes.upload_avatar(request, 0.0, 0.0, 100.0, 0))
        self.assertEqual(raised.exception.status_code, 413)

    def test_processing_errors_are_client_errors_and_remove_is_authenticated(self):
        request = _binary_request(b"avatar-bytes")
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            patch.object(
                client_routes,
                "run_control",
                new=AsyncMock(side_effect=client_routes.AvatarUnsupportedError("bad")),
            ),
            self.assertRaises(client_routes.HTTPException) as raised,
        ):
            asyncio.run(client_routes.upload_avatar(request, 0.0, 0.0, 100.0, 0))
        self.assertEqual(raised.exception.status_code, 415)

        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            patch.object(
                client_routes,
                "run_control",
                new=AsyncMock(),
            ) as control,
        ):
            response = asyncio.run(client_routes.delete_avatar(request))
        self.assertEqual(response, {"avatarVersion": None})
        control.assert_awaited_once_with(client_routes._remove_avatar, "user-1")

    def test_avatar_delivery_is_authenticated_and_versioned_cacheable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "avatar.webp"
            path.write_bytes(b"webp")
            request = _binary_request(
                b"",
                method="GET",
                path="/api/users/user-1/avatar",
            )
            request.scope["query_string"] = b"v=version-1"
            with (
                patch.object(
                    client_routes,
                    "_require_access",
                    new=AsyncMock(return_value={"id": "viewer"}),
                ) as access,
                patch.object(
                    client_routes,
                    "run_control",
                    new=AsyncMock(return_value=(path, "version-1", "webp")),
                ),
            ):
                response = asyncio.run(client_routes.user_avatar("user-1", request))
            self.assertEqual(response.media_type, "image/webp")
            self.assertIn("immutable", response.headers["cache-control"])
            access.assert_awaited_once_with(request)

    def test_admin_avatar_delivery_uses_administrator_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "avatar.webp"
            path.write_bytes(b"webp")
            request = _binary_request(
                b"",
                method="GET",
                path="/api/admin/users/user-1/avatar",
            )
            request.scope["query_string"] = b"v=version-1"
            with (
                patch.object(
                    client_routes,
                    "run_auth",
                    new=AsyncMock(return_value="root"),
                ) as auth,
                patch.object(
                    client_routes,
                    "run_control",
                    new=AsyncMock(return_value=(path, "version-1", "webp")),
                ) as control,
            ):
                response = asyncio.run(
                    client_routes.admin_user_avatar("user-1", request)
                )
            self.assertEqual(response.media_type, "image/webp")
            self.assertIn("immutable", response.headers["cache-control"])
            auth.assert_awaited_once_with(
                client_routes.authenticate_admin_request,
                request,
            )
            control.assert_awaited_once_with(
                client_routes._resolve_avatar,
                "user-1",
                "version-1",
            )

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


class ClientPasswordRouteTest(unittest.TestCase):
    def test_change_password_authenticates_runs_in_auth_lane_and_clears_cookies(self):
        request = _json_request(
            {
                "currentPassword": "current-password",
                "newPassword": "new-password",
                "confirmNewPassword": "new-password",
            },
            path="/api/account/password",
        )
        account_model = MagicMock()
        account_model.change_password = MagicMock()
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ) as require_account,
            patch.object(client_routes, "Account", return_value=account_model),
            patch.object(client_routes, "run_auth", new=AsyncMock()) as auth,
        ):
            response = asyncio.run(client_routes.change_password(request))

        self.assertEqual(response.status_code, 204)
        require_account.assert_awaited_once_with(request)
        auth.assert_awaited_once_with(
            account_model.change_password,
            "user-1",
            "current-password",
            "new-password",
            "new-password",
        )
        cookies = [
            value.decode("latin-1")
            for name, value in response.raw_headers
            if name.lower() == b"set-cookie"
        ]
        self.assertTrue(
            any(value.startswith('__Host-zenstream-session=""') for value in cookies)
        )
        self.assertTrue(
            any(value.startswith('zenstream-session=""') for value in cookies)
        )

    def test_change_password_maps_invalid_input_to_bad_request(self):
        request = _json_request(
            {
                "currentPassword": "wrong",
                "newPassword": "new-password",
                "confirmNewPassword": "new-password",
            },
            path="/api/account/password",
        )
        account_model = MagicMock()
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            patch.object(client_routes, "Account", return_value=account_model),
            patch.object(
                client_routes,
                "run_auth",
                new=AsyncMock(side_effect=ValueError("Current password is incorrect.")),
            ),
            self.assertRaises(client_routes.HTTPException) as raised,
        ):
            asyncio.run(client_routes.change_password(request))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(str(raised.exception.detail), "Current password is incorrect.")


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
                "images": {
                    "Primary": {"url": "/primary"},
                    "Backdrop": {"url": "/backdrop"},
                },
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
                client_routes,
                "issue_ticket",
                side_effect=["resource-ticket", "artwork-ticket"],
            ) as ticket_issuer,
        ):
            response = asyncio.run(client_routes.auth_bootstrap(request))

        self.assertEqual(response["resourceTicket"], "resource-ticket")
        self.assertEqual(response["artworkTicket"], "artwork-ticket")
        self.assertEqual(response["metadataLanguage"], {"language": "ja"})
        self.assertEqual(response["languages"], ["en", "ja"])
        self.assertEqual(ticket_issuer.call_count, 2)
        self.assertEqual(
            ticket_issuer.call_args_list[0].args,
            ("user-1", "resource", client_routes.RESOURCE_TICKET_TTL_SECONDS),
        )
        self.assertEqual(
            ticket_issuer.call_args_list[0].kwargs, {"sessionId": "session"}
        )
        self.assertEqual(
            ticket_issuer.call_args_list[1].args,
            ("user-1", "artwork", client_routes.ARTWORK_TICKET_TTL_SECONDS),
        )
        self.assertEqual(
            ticket_issuer.call_args_list[1].kwargs, {"sessionId": "session"}
        )

    def test_artwork_ticket_route_returns_a_session_bound_ticket(self):
        request = _json_request({}, method="GET", path="/api/auth/artwork-ticket")
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(
                    return_value=({"id": "user-1", "username": "viewer"}, "token")
                ),
            ),
            patch.object(client_routes, "session_id_for_token", return_value="session"),
            patch.object(
                client_routes, "issue_ticket", return_value="artwork-ticket"
            ) as ticket_issuer,
        ):
            response = asyncio.run(client_routes.artwork_ticket(request))

        self.assertEqual(response, {
            "ticket": "artwork-ticket",
            "expiresIn": client_routes.ARTWORK_TICKET_TTL_SECONDS,
        })
        ticket_issuer.assert_called_once_with(
            "user-1",
            "artwork",
            client_routes.ARTWORK_TICKET_TTL_SECONDS,
            sessionId="session",
        )

    def test_playback_access_refresh_uses_authenticated_foreground_work(self):
        request = _json_request(
            {"sourceId": "source-1", "sessionId": "playback-session-1"},
            path="/api/playback/items/entity-1/access",
        )
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(
                    return_value=({"id": "user-1", "username": "viewer"}, "token")
                ),
            ),
            patch.object(
                client_routes,
                "session_id_for_token",
                return_value="auth-session-1",
            ),
            patch.object(
                client_routes,
                "run_foreground",
                new=AsyncMock(return_value={"ticket": "renewed-ticket"}),
            ) as foreground,
        ):
            response = asyncio.run(
                client_routes.refresh_playback_access("entity-1", request)
            )

        self.assertEqual(response, {"ticket": "renewed-ticket"})
        self.assertEqual(
            foreground.await_args.args[0], client_routes.media.refresh_access
        )
        self.assertEqual(
            foreground.await_args.args[1:],
            (
                "user-1",
                "entity-1",
                "source-1",
                "playback-session-1",
                "auth-session-1",
            ),
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
            (client_routes.set_watch_history, "/api/preferences/watch-history"),
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

    def test_watch_history_preference_routes_use_the_control_lane(self):
        preference = MagicMock()
        request = _json_request(
            {"enabled": False},
            method="PATCH",
            path="/api/preferences/watch-history",
        )
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            patch.object(client_routes, "AccountPreference", return_value=preference),
            patch.object(
                client_routes,
                "run_control",
                new=AsyncMock(return_value={"enabled": False}),
            ) as control,
        ):
            response = asyncio.run(client_routes.set_watch_history(request))

        self.assertEqual(response, {"enabled": False})
        control.assert_awaited_once_with(preference.set_watch_history, False)

    def test_clear_watch_history_returns_no_content_through_the_control_lane(self):
        request = _json_request({}, method="DELETE", path="/api/account/watch-history")
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            patch.object(
                client_routes,
                "run_control",
                new=AsyncMock(),
            ) as control,
        ):
            response = asyncio.run(client_routes.clear_watch_history(request))

        self.assertIsNone(response)
        control.assert_awaited_once_with(
            client_routes.catalog.clear_watch_history, "user-1"
        )

    def test_progress_route_is_separate_from_explicit_state(self):
        request = _json_request(
            {"positionSeconds": 12, "durationSeconds": 100},
            method="PATCH",
            path="/api/catalog/items/item-1/progress",
        )
        with (
            patch.object(
                client_routes,
                "_require_account",
                new=AsyncMock(return_value=({"id": "user-1"}, "token")),
            ),
            patch.object(
                client_routes,
                "run_control",
                new=AsyncMock(return_value={"played": False}),
            ) as control,
        ):
            response = asyncio.run(
                client_routes.update_item_progress("item-1", request)
            )

        self.assertEqual(response, {"played": False})
        control.assert_awaited_once_with(
            client_routes.catalog.update_progress,
            "user-1",
            "item-1",
            {"positionSeconds": 12, "durationSeconds": 100},
        )


if __name__ == "__main__":
    unittest.main()
