import unittest

from app.database import DatabaseHandler
from app.models.account_preference import AccountPreference


class AccountPreferenceTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.execute(
            "CREATE TABLE account_preferences(user_id TEXT PRIMARY KEY,locale TEXT,audio_language TEXT,subtitle_language TEXT,watch_history_enabled INTEGER NOT NULL DEFAULT 1)"
        )
        self.db.execute("CREATE TABLE libraries(id TEXT PRIMARY KEY)")
        self.db.execute(
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT)"
        )
        self.db.execute(
            "CREATE TABLE media_files(id TEXT PRIMARY KEY,entity_id TEXT,role TEXT,language TEXT)"
        )
        self.db.execute(
            "CREATE TABLE media_track_languages(media_file_id TEXT,track_type TEXT,language TEXT)"
        )
        self.preference = AccountPreference.__new__(AccountPreference)
        self.preference.user_id = "user"
        self.preference.db = self.db

    def tearDown(self):
        self.db.close()

    def test_watch_history_defaults_enabled_and_persists(self):
        self.assertEqual(self.preference.watch_history(), {"enabled": True})
        self.assertEqual(self.preference.set_watch_history(False), {"enabled": False})
        self.assertEqual(self.preference.watch_history(), {"enabled": False})

    def test_watch_history_rejects_non_boolean_values(self):
        with self.assertRaises(ValueError):
            self.preference.set_watch_history("false")

    def test_playback_languages_are_permission_filtered_and_persisted(self):
        self.db.execute("INSERT INTO libraries(id) VALUES('allowed'),('hidden')")
        self.db.execute(
            "INSERT INTO library_entities(id,library_id) VALUES('entity-a','allowed'),('entity-b','hidden')"
        )
        self.db.execute(
            "INSERT INTO user_library_access(user_id,library_id) VALUES('user','allowed')"
        )
        self.db.execute(
            "INSERT INTO media_files(id,entity_id,role,language) VALUES('sidecar-a','entity-a','subtitle','ja'),('sidecar-und','entity-a','subtitle','und'),('lyrics-a','entity-a','lyrics','fr'),('sidecar-b','entity-b','subtitle','fr')"
        )
        self.db.execute(
            "INSERT INTO media_track_languages(media_file_id,track_type,language) VALUES('sidecar-a','audio','en'),('sidecar-a','subtitle','ja'),('sidecar-a','audio','und'),('sidecar-a','subtitle','xx'),('sidecar-b','audio','fr')"
        )

        value = self.preference.playback()
        self.assertEqual([item["value"] for item in value["audioLanguages"]], ["en"])
        self.assertEqual([item["value"] for item in value["subtitleLanguages"]], ["ja"])

        saved = self.preference.set_playback(
            {"audioLanguage": "en", "subtitleLanguage": "ja"}
        )
        self.assertEqual(saved["audioLanguage"], "en")
        self.assertEqual(saved["subtitleLanguage"], "ja")
        with self.assertRaises(ValueError):
            self.preference.set_playback({"audioLanguage": "fr"})
