import unittest

from app.database import DatabaseHandler
from app.models.metadata import MetadataLanguageSettings


class MetadataLanguageSettingsTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE metadata_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)"
        )
        self.db.execute("CREATE TABLE account_preferences(metadata_language TEXT)")
        self.settings = MetadataLanguageSettings.__new__(MetadataLanguageSettings)
        self.settings.db = self.db

    def tearDown(self):
        self.db.close()

    def test_default_is_disabled_and_update_is_atomic(self):
        self.assertEqual(
            self.settings.get_settings(),
            {"locales": ["en"], "preferNoLanguageForBackdrop": False},
        )
        self.assertEqual(
            self.settings.update(["en", "ja"], True),
            {"locales": ["en", "ja"], "preferNoLanguageForBackdrop": True},
        )
        self.assertEqual(
            self.db.execute("SELECT key FROM metadata_settings ORDER BY key"),
            [("locales",), ("prefer_no_language_for_backdrop",)],
        )

    def test_omitted_option_preserves_current_value(self):
        self.settings.update(["en"], True)
        self.settings.update(["en", "de"])
        self.assertTrue(self.settings.prefer_no_language_for_backdrop())

    def test_option_requires_a_json_boolean(self):
        with self.assertRaises(ValueError):
            self.settings.update(["en"], 1)
        with self.assertRaises(ValueError):
            self.settings.update(["en"], None)


if __name__ == "__main__":
    unittest.main()
