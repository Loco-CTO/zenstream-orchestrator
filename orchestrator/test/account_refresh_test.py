import unittest
from unittest.mock import patch

from app.database import DatabaseHandler
from app.models.account import Account, RefreshTokenError


class AccountRefreshTokenTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT,password TEXT,password_scheme TEXT,disabled INTEGER DEFAULT 0)",
            "CREATE TABLE user_sessions(id TEXT PRIMARY KEY,user_id TEXT,token_hash TEXT UNIQUE,expires_at TEXT,created_at TEXT,last_seen_at TEXT,device_id TEXT,access_expires_at TEXT,refresh_family_id TEXT,revoked_at TEXT)",
            "CREATE TABLE user_refresh_tokens(id TEXT PRIMARY KEY,session_id TEXT,user_id TEXT,family_id TEXT,token_hash TEXT UNIQUE,created_at TEXT,expires_at TEXT,used_at TEXT,revoked_at TEXT,replaced_by_id TEXT)",
        ):
            self.db.execute(statement)
        self.db.execute(
            "INSERT INTO users VALUES(?,?,?,?,0)",
            ("user-1", "viewer", "hash", "sha256"),
        )
        self.account = Account.__new__(Account)
        self.account.db = self.db

    def tearDown(self):
        self.db.close()

    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_rotation_invalidates_old_access_and_reuse_revokes_only_family(
        self, _avatar_version
    ):
        first = self.account.create_session("user-1", supports_refresh=True)
        second = self.account.create_session("user-1", supports_refresh=True)

        rotated = self.account.refresh_session(first["refreshToken"])
        self.assertNotEqual(rotated["token"], first["token"])
        self.assertNotEqual(rotated["refreshToken"], first["refreshToken"])
        self.assertIsNotNone(self.account.authenticate_token(rotated["token"]))
        self.assertIsNone(self.account.authenticate_token(first["token"]))
        self.assertIsNotNone(self.account.authenticate_token(second["token"]))

        with self.assertRaises(RefreshTokenError):
            self.account.refresh_session(first["refreshToken"])

        self.assertIsNone(self.account.authenticate_token(rotated["token"]))
        self.assertIsNotNone(self.account.authenticate_token(second["token"]))

    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_legacy_session_can_be_upgraded_without_a_mass_logout(
        self, _avatar_version
    ):
        legacy = self.account.create_session("user-1")
        upgraded = self.account.upgrade_legacy_session(legacy["token"])

        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded["user"]["id"], "user-1")
        self.assertIsNone(self.account.authenticate_token(legacy["token"]))
        self.assertIsNotNone(self.account.authenticate_token(upgraded["token"]))
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id=?", ("user-1",)
            )[0][0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
