import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from api.zenstream.locale import LocalePreference
from app.models.preference import (
    DEFAULT_SUBTITLE_STYLE,
    UserPreference,
    _validate_subtitle_style,
)
from jellyfin.api_service import _build_auth_header, authenticated_user_id


class FakeDatabase:
    def __init__(self):
        self.values = {}

    def execute(self, query, params):
        if query.lstrip().startswith("SELECT"):
            locale = self.values.get(params[0])
            return [(locale,)] if locale else []
        self.values[params[0]] = params[1]
        return []


class FakeSubtitleDatabase:
    def __init__(self):
        self.values = {}

    def execute(self, query, params):
        if query.lstrip().startswith("SELECT"):
            style = self.values.get(params[0])
            if not style:
                return []
            return [
                (
                    style["fontFamily"],
                    int(style["bold"]),
                    style["textScale"],
                    style["fontColor"],
                    style["borderSize"],
                    style["borderColor"],
                    style["backgroundColor"],
                    style["backgroundOpacity"],
                )
            ]
        self.values[params[0]] = {
            "fontFamily": params[1],
            "bold": bool(params[2]),
            "textScale": params[3],
            "fontColor": params[4],
            "borderSize": params[5],
            "borderColor": params[6],
            "backgroundColor": params[7],
            "backgroundOpacity": params[8],
        }
        return []


class PreferenceModelTests(unittest.TestCase):
    def setUp(self):
        self.preference = UserPreference.__new__(UserPreference)
        self.preference.jellyfin_user_id = "user-a"
        self.preference._db = FakeDatabase()

    def test_defaults_to_english_and_upserts_japanese(self):
        self.assertEqual(self.preference.get_locale(), "en")
        self.assertEqual(self.preference.set_locale("ja"), "ja")
        self.assertEqual(self.preference.get_locale(), "ja")

    def test_rejects_unsupported_locale(self):
        with self.assertRaises(ValueError):
            self.preference.set_locale("fr")

    def test_keeps_users_isolated(self):
        shared_database = FakeDatabase()
        first = UserPreference.__new__(UserPreference)
        first.jellyfin_user_id, first._db = "user-a", shared_database
        second = UserPreference.__new__(UserPreference)
        second.jellyfin_user_id, second._db = "user-b", shared_database
        first.set_locale("ja")
        self.assertEqual(first.get_locale(), "ja")
        self.assertEqual(second.get_locale(), "en")

    def test_persists_subtitle_font_family_with_the_style(self):
        self.preference._db = FakeSubtitleDatabase()
        style = {**DEFAULT_SUBTITLE_STYLE, "fontFamily": "serif", "textScale": 125}
        self.assertEqual(self.preference.get_subtitle_style(), DEFAULT_SUBTITLE_STYLE)
        self.assertEqual(self.preference.set_subtitle_style(style), style)
        self.assertEqual(self.preference.get_subtitle_style(), style)

    def test_rejects_unknown_subtitle_font_family(self):
        with self.assertRaises(ValueError):
            _validate_subtitle_style({"fontFamily": "comic-sans"})


class JellyfinAuthenticationTests(unittest.TestCase):
    def test_builds_jellyfin_media_browser_header(self):
        headers = _build_auth_header("token-value")
        self.assertEqual(headers["X-Emby-Token"], "token-value")
        self.assertIn('Token="token-value"', headers["Authorization"])
        self.assertIn('Client="ZenStream Orchestrator"', headers["Authorization"])

    @patch("jellyfin.api_service.requests.get")
    def test_derives_user_id_from_jellyfin(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"Id": "jellyfin-user"}
        with patch.dict(os.environ, {"JELLYFIN_URL": "https://jellyfin.example/"}):
            self.assertEqual(authenticated_user_id("token-value"), "jellyfin-user")
        get.assert_called_once_with(
            "https://jellyfin.example/Users/Me",
            headers=_build_auth_header("token-value"),
            timeout=5,
        )


class LocaleEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    @patch("api.zenstream.locale.UserPreference")
    @patch("api.zenstream.locale.authenticated_user_id", return_value="derived-user")
    def test_reads_locale_for_token_derived_user(self, _, preference):
        preference.return_value.get_locale.return_value = "ja"
        with self.app.test_request_context(
            "/api/preferences/locale",
            headers={"X-Jellyfin-Token": "valid"},
        ):
            body, status = LocalePreference().get()
        self.assertEqual((body, status), ({"locale": "ja"}, 200))
        preference.assert_called_once_with("derived-user")

    def test_requires_token(self):
        with self.app.test_request_context("/api/preferences/locale"):
            body, status = LocalePreference().get()
        self.assertEqual(status, 401)
        self.assertIn("authentication", body["message"].lower())

    @patch("api.zenstream.locale.authenticated_user_id", return_value=None)
    def test_rejects_invalid_token(self, _):
        with self.app.test_request_context(
            "/api/preferences/locale",
            headers={"X-Jellyfin-Token": "invalid"},
        ):
            _, status = LocalePreference().get()
        self.assertEqual(status, 401)

    @patch("api.zenstream.locale.authenticated_user_id", return_value="derived-user")
    def test_rejects_invalid_locale(self, _):
        with self.app.test_request_context(
            "/api/preferences/locale",
            method="PATCH",
            headers={"X-Jellyfin-Token": "valid"},
            json={"locale": "fr"},
        ):
            _, status = LocalePreference().patch()
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
