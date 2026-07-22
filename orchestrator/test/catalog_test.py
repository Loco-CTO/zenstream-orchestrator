import hashlib
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.catalog import Catalog
from app.database import DatabaseHandler
from app.models.account import Account
from app.models.metadata import IMAGE_LANGUAGE_SCHEMA


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        statements = [
            "CREATE TABLE users(id TEXT UNIQUE,username TEXT UNIQUE,password TEXT,password_scheme TEXT,disabled INTEGER DEFAULT 0)",
            "CREATE TABLE user_sessions(id TEXT PRIMARY KEY,user_id TEXT,token_hash TEXT UNIQUE,expires_at TEXT,created_at TEXT,last_seen_at TEXT)",
            "CREATE TABLE libraries(id TEXT PRIMARY KEY,name TEXT,type TEXT,scan_state TEXT,last_scan_finished_at TEXT,directory TEXT)",
            "CREATE TABLE user_library_access(user_id TEXT,library_id TEXT,created_at TEXT,PRIMARY KEY(user_id,library_id))",
            "CREATE TABLE library_entities(id TEXT PRIMARY KEY,library_id TEXT,parent_id TEXT,entity_type TEXT,relative_path TEXT,season_number INTEGER,episode_number INTEGER,episode_end_number INTEGER,track_number INTEGER,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE entity_provider_ids(entity_id TEXT,provider TEXT,identifier_type TEXT,provider_id TEXT,is_primary INTEGER)",
            "CREATE TABLE metadata_cache(provider TEXT,entity_type TEXT,provider_id TEXT,locale TEXT,payload TEXT,fetched_at TEXT,expires_at TEXT)",
            "CREATE TABLE user_item_state(user_id TEXT,entity_id TEXT,favorite INTEGER,played INTEGER,play_count INTEGER,position_seconds REAL,duration_seconds REAL,last_played_at TEXT,updated_at TEXT,PRIMARY KEY(user_id,entity_id))",
        ]
        for statement in statements:
            self.db.execute(statement)
        self.db.execute("INSERT INTO libraries VALUES('allowed','Allowed','movies','ready',NULL,NULL)")
        self.db.execute("INSERT INTO libraries VALUES('hidden','Hidden','movies','ready',NULL,NULL)")

    def tearDown(self):
        self.db.close()

    def account(self):
        value = Account.__new__(Account)
        value.db = self.db
        return value

    def catalog(self):
        value = Catalog.__new__(Catalog)
        value.db = self.db
        return value

    def seed_item(self):
        self.db.execute("INSERT INTO library_entities VALUES('movie','allowed',NULL,'movie','Movie',NULL,NULL,NULL,NULL,'2026','2026')")
        self.db.execute("INSERT INTO entity_provider_ids VALUES('movie','tmdb','movie','10',1)")
        for locale, payload in {
            "en": {"title": "English", "overview": "English overview", "originalLanguage": "ja", "images": [{"type": "Primary", "url": "en.jpg", "language": "en", "provider": "tmdb"}]},
            "ja": {"title": "Japanese", "originalLanguage": "ja", "images": [{"type": "Primary", "url": "neutral.jpg", "language": None, "provider": "tmdb"}]},
        }.items():
            payload["_imageLanguageSchema"] = IMAGE_LANGUAGE_SCHEMA
            self.db.execute("INSERT INTO metadata_cache VALUES('tmdb','movie','10',?,?,?,?)", (locale, json.dumps(payload), "now", "later"))

    def test_argon_session_revocation_and_legacy_password_upgrade(self):
        account = self.account()
        created = account.create("local", "password-123")
        session = account.create_session(created["id"])
        self.assertEqual(account.authenticate_token(session["token"])["id"], created["id"])
        account.revoke(session["token"])
        self.assertIsNone(account.authenticate_token(session["token"]))

        legacy_id = "legacy-id"
        self.db.execute("INSERT INTO users VALUES(?,?,?,?,0)", (legacy_id, "legacy", hashlib.sha256(b"legacy-pass").hexdigest(), "sha256"))
        self.assertEqual(account.authenticate_password("legacy", "legacy-pass")["id"], legacy_id)
        self.assertEqual(self.db.execute("SELECT password_scheme FROM users WHERE id=?", (legacy_id,))[0][0], "argon2id")

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en", "ja"])
    def test_permissions_and_field_level_language_fallback(self, _languages):
        account = self.account().create("viewer", "password-123")
        self.db.execute("INSERT INTO user_library_access VALUES(?,?,?)", (account["id"], "allowed", "now"))
        self.seed_item()
        metadata = self.catalog().metadata(account["id"], "movie", "ja")["metadata"]
        self.assertEqual(metadata["title"], "Japanese")
        self.assertEqual(metadata["overview"], "English overview")
        self.assertEqual(metadata["images"]["Primary"]["language"], None)
        with self.assertRaises(HTTPException) as hidden:
            self.catalog().require_library(account["id"], "hidden")
        self.assertEqual(hidden.exception.status_code, 404)
        with self.assertRaises(HTTPException) as unsupported:
            self.catalog().metadata(account["id"], "movie", "fr")
        self.assertEqual(unsupported.exception.status_code, 400)

    @patch("app.catalog.MetadataLanguageSettings.get", return_value=["en", "ja"])
    def test_progress_marks_played_at_ninety_percent(self, _languages):
        account = self.account().create("progress", "password-123")
        self.db.execute("INSERT INTO user_library_access VALUES(?,?,?)", (account["id"], "allowed", "now"))
        self.seed_item()
        state = self.catalog().update_state(account["id"], "movie", {"positionSeconds": 90, "durationSeconds": 100})
        self.assertTrue(state["played"])
        self.assertEqual(state["playCount"], 1)
        self.assertEqual(state["positionSeconds"], 0)


if __name__ == "__main__":
    unittest.main()
