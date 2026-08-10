import asyncio
import unittest
from unittest.mock import MagicMock, patch

from api.zenstream import application_routes as app_module
from fastapi import HTTPException
from starlette.requests import Request


def _request(method="GET", origin="https://example.test", cookie=True):
    headers = [(b"host", b"example.test")]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if cookie:
        headers.append(
            (b"cookie", b"__Host-zenstream-admin=server-owned-session")
        )
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/api/admin/profile",
            "query_string": b"",
            "headers": headers,
            "server": ("example.test", 443),
        }
    )


class AdminProfileRouteTest(unittest.TestCase):
    def test_cookie_session_derives_identity_and_ignores_username_header(self):
        authenticated = MagicMock(username="root")
        with patch.object(
            app_module.Admin, "from_token", return_value=authenticated
        ) as from_token:
            username, token = app_module._admin_request(
                _request(), "attacker-controlled", "header-session"
            )
        self.assertEqual((username, token), ("root", "server-owned-session"))
        from_token.assert_called_once_with("server-owned-session")

    def test_cookie_authenticated_mutation_rejects_cross_origin_request(self):
        with self.assertRaises(HTTPException) as raised:
            app_module._admin_request(
                _request(method="POST", origin="https://attacker.test")
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_root_boundary_rejects_non_root_administrator(self):
        admin = MagicMock()
        admin.profile.return_value = {
            "username": "operator",
            "is_root": False,
            "disabled": False,
        }
        with (
            patch.object(
                app_module,
                "_admin_request",
                return_value=("operator", "server-owned-session"),
            ),
            patch.object(app_module, "Admin", return_value=admin),
        ):
            with self.assertRaises(HTTPException) as raised:
                app_module._root_admin_request(_request())
        self.assertEqual(raised.exception.status_code, 403)

    def test_get_profile_uses_authenticated_administrator(self):
        admin = MagicMock()
        admin.profile.return_value = {
            "username": "root",
            "is_root": True,
            "disabled": False,
        }
        with (
            patch.object(app_module, "_admin_headers", return_value=("root", "token")),
            patch.object(app_module, "Admin", return_value=admin),
        ):
            result = asyncio.run(
                app_module.admin_profile(Username="root", TOKEN="token")
            )
        self.assertEqual(result["username"], "root")
        admin.profile.assert_called_once_with()

    def test_patch_profile_preserves_current_session_token(self):
        admin = MagicMock()
        admin.update_profile.return_value = {"username": "renamed", "token": "token"}
        with (
            patch.object(app_module, "_admin_headers", return_value=("root", "token")),
            patch.object(app_module, "Admin", return_value=admin),
        ):
            result = asyncio.run(
                app_module.admin_update_profile(
                    New_Username="renamed",
                    New_Password="new-password",
                    Username="root",
                    TOKEN="token",
                )
            )
        self.assertEqual(result, {"username": "renamed"})
        admin.update_profile.assert_called_once_with("renamed", "new-password", "token")

    def test_patch_profile_returns_bad_request_for_invalid_update(self):
        admin = MagicMock()
        admin.update_profile.side_effect = ValueError(
            "Password must be at least 8 characters."
        )
        with (
            patch.object(app_module, "_admin_headers", return_value=("root", "token")),
            patch.object(app_module, "Admin", return_value=admin),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    app_module.admin_update_profile(
                        New_Username=None,
                        New_Password="short",
                        Username="root",
                        TOKEN="token",
                    )
                )
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
