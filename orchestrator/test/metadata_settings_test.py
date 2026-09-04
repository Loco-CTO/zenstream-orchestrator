import unittest

from app.database import DatabaseHandler
from app.models.metadata import MetadataLanguageSettings, MetadataRefreshSettings


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


class MetadataRefreshSettingsTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE metadata_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)"
        )
        self.settings = MetadataRefreshSettings(self.db)

    def tearDown(self):
        self.db.close()

    def test_defaults_cover_each_video_type_and_artwork_bucket(self):
        value = self.settings.get()

        self.assertEqual(
            set(value["itemTypes"]), {"movie", "series", "season", "episode"}
        )
        self.assertEqual(
            set(value["itemTypes"]["episode"]["artwork"]),
            {"Primary", "Backdrop", "Logo", "Banner"},
        )
        self.assertEqual(value["itemTypes"]["episode"]["cutoffDays"], 14)
        self.assertEqual(value["itemTypes"]["series"]["statusAfterDays"], 180)

    def test_update_normalizes_partial_item_settings_atomically(self):
        value = self.settings.update(
            {
                "seriesBlockList": "  Example  ",
                "itemTypes": {
                    "movie": {
                        "enabled": False,
                        "documentMaxAgeDays": 30,
                        "artwork": {"Backdrop": {"enabled": True, "maxAgeDays": 2}},
                    }
                },
            }
        )

        self.assertEqual(value["seriesBlockList"], "Example")
        self.assertFalse(value["itemTypes"]["movie"]["enabled"])
        self.assertEqual(value["itemTypes"]["movie"]["documentMaxAgeDays"], 30)
        self.assertTrue(value["itemTypes"]["movie"]["artwork"]["Backdrop"]["enabled"])
        self.assertEqual(self.settings.get(), value)

    def test_rejects_invalid_values_and_unknown_keys(self):
        with self.assertRaises(ValueError):
            self.settings.update({"pretend": 1})
        with self.assertRaises(ValueError):
            self.settings.update({"itemTypes": {"movie": {"minimumProviderIds": -1}}})
        with self.assertRaises(ValueError):
            self.settings.update({"itemTypes": {"music": {}}})
        with self.assertRaises(ValueError):
            self.settings.update(
                {"itemTypes": {"movie": {"artwork": {"Poster": {}}}}}
            )

    def test_malformed_persisted_value_returns_safe_defaults(self):
        self.db.execute(
            "INSERT INTO metadata_settings VALUES(?,?,?)",
            ("metadata_refresh_settings", "not-json", "now"),
        )

        self.assertFalse(self.settings.get()["pretend"])
        self.assertEqual(self.settings.get()["itemTypes"]["movie"]["cutoffDays"], -1)


if __name__ == "__main__":
    unittest.main()
