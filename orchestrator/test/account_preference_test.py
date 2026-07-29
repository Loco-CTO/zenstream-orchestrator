import unittest

from app.database import DatabaseHandler
from app.models.account_preference import AccountPreference
from app.models.subtitle_style import DEFAULT_SUBTITLE_STYLE, validate_subtitle_style


class AccountPreferenceTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE account_preferences(user_id TEXT PRIMARY KEY,subtitle_renderer TEXT NOT NULL DEFAULT 'native',subtitle_font_family TEXT NOT NULL DEFAULT 'sans',subtitle_bold INTEGER NOT NULL DEFAULT 0,subtitle_text_scale REAL NOT NULL DEFAULT 100,subtitle_font_color TEXT NOT NULL DEFAULT '#ffffff',subtitle_border_size REAL NOT NULL DEFAULT 0,subtitle_border_color TEXT NOT NULL DEFAULT '#000000',subtitle_background_color TEXT NOT NULL DEFAULT '#000000',subtitle_background_opacity REAL NOT NULL DEFAULT 0)"
        )
        self.preference = AccountPreference.__new__(AccountPreference)
        self.preference.user_id = "user"
        self.preference.db = self.db

    def tearDown(self):
        self.db.close()

    def test_defaults_to_native_renderer_and_persists_overlay(self):
        self.assertEqual(self.preference.subtitle_style(), DEFAULT_SUBTITLE_STYLE)
        updated = self.preference.set_subtitle_style(
            {**DEFAULT_SUBTITLE_STYLE, "renderer": "overlay"}
        )
        self.assertEqual(updated["renderer"], "overlay")
        self.assertEqual(self.preference.subtitle_style()["renderer"], "overlay")

    def test_rejects_unknown_subtitle_renderer(self):
        with self.assertRaises(ValueError):
            validate_subtitle_style({**DEFAULT_SUBTITLE_STYLE, "renderer": "custom"})
