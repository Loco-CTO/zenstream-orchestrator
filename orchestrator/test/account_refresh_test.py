import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.database import DatabaseHandler
from app.models.account import Account, RefreshTokenError


class AccountRefreshTokenTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        for statement in (
            "CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT,password TEXT,password_scheme TEXT,disabled INTEGER DEFAULT 0)",
            "CREATE TABLE user_sessions(id TEXT PRIMARY KEY,user_id TEXT,token_hash TEXT UNIQUE,expires_at TEXT,created_at TEXT,last_seen_at TEXT,device_id TEXT,access_expires_at TEXT,refresh_family_id TEXT,revoked_at TEXT)",
            "CREATE TABLE user_refresh_tokens(id TEXT PRIMARY KEY,session_id TEXT,user_id TEXT,family_id TEXT,token_hash TEXT UNIQUE,created_at TEXT,expires_at TEXT,used_at TEXT,revoked_at TEXT,replaced_by_id TEXT,rotation_attempt_id TEXT,rotation_response_ciphertext TEXT)",
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

    @patch.dict("os.environ", {"SECRET_KEY": "refresh-attempt-test-secret"})
    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_rotation_invalidates_old_access_and_reuse_revokes_only_family(
        self, _avatar_version
    ):
        first = self.account.create_session("user-1", supports_refresh=True)
        second = self.account.create_session("user-1", supports_refresh=True)

        rotated = self.account.refresh_session(
            first["refreshToken"],
            refresh_attempt_id="99e8a7d4-1f18-4ec7-82d5-e98bcf16252d",
        )
        self.assertNotEqual(rotated["token"], first["token"])
        self.assertNotEqual(rotated["refreshToken"], first["refreshToken"])
        self.assertIsNotNone(self.account.authenticate_token(rotated["token"]))
        self.assertIsNone(self.account.authenticate_token(first["token"]))
        self.assertIsNotNone(self.account.authenticate_token(second["token"]))

        with self.assertRaises(RefreshTokenError):
            self.account.refresh_session(
                first["refreshToken"],
                refresh_attempt_id="b2dd3ad0-f199-4203-8aa2-8506273a3971",
            )

        self.assertIsNone(self.account.authenticate_token(rotated["token"]))
        self.assertIsNotNone(self.account.authenticate_token(second["token"]))

    @patch.dict("os.environ", {"SECRET_KEY": "refresh-attempt-test-secret"})
    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_same_refresh_attempt_returns_original_rotated_pair(
        self, _avatar_version
    ):
        first = self.account.create_session("user-1", supports_refresh=True)
        attempt_id = "5e0cf88a-6f67-467d-8d3c-4f338680f4f1"

        rotated = self.account.refresh_session(
            first["refreshToken"], refresh_attempt_id=attempt_id
        )
        retried = self.account.refresh_session(
            first["refreshToken"], refresh_attempt_id=attempt_id
        )
        stored_ciphertext = self.db.read_execute(
            "SELECT rotation_response_ciphertext FROM user_refresh_tokens WHERE token_hash=?",
            (hashlib.sha256(first["refreshToken"].encode()).hexdigest(),),
        )[0][0]

        self.assertEqual(retried, rotated)
        self.assertNotIn(rotated["token"], stored_ciphertext)
        self.assertNotIn(rotated["refreshToken"], stored_ciphertext)
        self.assertIsNotNone(self.account.authenticate_token(rotated["token"]))

    @patch.dict("os.environ", {"SECRET_KEY": "refresh-attempt-test-secret"})
    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_same_attempt_cannot_recover_credentials_after_session_revocation(
        self, _avatar_version
    ):
        first = self.account.create_session("user-1", supports_refresh=True)
        unrelated = self.account.create_session("user-1", supports_refresh=True)
        attempt_id = "fac8ce4d-d3c9-4a3d-9889-5449337e2854"
        rotated = self.account.refresh_session(
            first["refreshToken"], refresh_attempt_id=attempt_id
        )
        self.db.execute(
            "UPDATE user_sessions SET revoked_at=? WHERE id=?",
            ("2026-01-01T00:00:00+00:00", rotated["sessionId"]),
        )

        with self.assertRaises(RefreshTokenError):
            self.account.refresh_session(
                first["refreshToken"], refresh_attempt_id=attempt_id
            )

        self.assertIsNone(self.account.authenticate_token(rotated["token"]))
        self.assertIsNotNone(self.account.authenticate_token(unrelated["token"]))

    @patch.dict("os.environ", {"SECRET_KEY": "refresh-attempt-test-secret"})
    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_concurrent_retries_for_same_attempt_return_one_rotation(
        self, _avatar_version
    ):
        first = self.account.create_session("user-1", supports_refresh=True)
        attempt_id = "8472bdb9-a47a-4457-b854-21be651c2acf"

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(
                executor.map(
                    lambda _: self.account.refresh_session(
                        first["refreshToken"], refresh_attempt_id=attempt_id
                    ),
                    range(5),
                )
            )

        self.assertTrue(all(result == results[0] for result in results))
        self.assertIsNotNone(self.account.authenticate_token(results[0]["token"]))
        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM user_refresh_tokens WHERE session_id=? AND used_at IS NULL",
                (results[0]["sessionId"],),
            )[0][0],
            1,
        )

    @patch.dict("os.environ", {"SECRET_KEY": "refresh-attempt-test-secret"})
    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_recovered_response_expires_with_consumed_refresh_token(
        self, _avatar_version
    ):
        first = self.account.create_session("user-1", supports_refresh=True)
        attempt_id = "d2b95d8f-9492-4585-a08f-e454f1a6ea90"
        rotated = self.account.refresh_session(
            first["refreshToken"], refresh_attempt_id=attempt_id
        )
        self.db.execute(
            "UPDATE user_refresh_tokens SET expires_at=? WHERE token_hash=?",
            (
                "2000-01-01T00:00:00+00:00",
                hashlib.sha256(first["refreshToken"].encode()).hexdigest(),
            ),
        )

        with self.assertRaises(RefreshTokenError):
            self.account.refresh_session(
                first["refreshToken"], refresh_attempt_id=attempt_id
            )

        self.assertIsNotNone(self.account.authenticate_token(rotated["token"]))

    @patch.dict("os.environ", {"SECRET_KEY": "refresh-attempt-test-secret"})
    @patch("app.avatar.UserAvatarStore.version", return_value=None)
    def test_expired_refresh_attempt_response_is_removed_by_existing_retention(
        self, _avatar_version
    ):
        first = self.account.create_session("user-1", supports_refresh=True)
        attempt_id = "94e88bd2-70f1-40b2-9292-077056f74d49"
        rotated = self.account.refresh_session(
            first["refreshToken"], refresh_attempt_id=attempt_id
        )
        self.db.execute(
            "UPDATE user_refresh_tokens SET expires_at=? WHERE token_hash=?",
            (
                "2000-01-01T00:00:00+00:00",
                hashlib.sha256(first["refreshToken"].encode()).hexdigest(),
            ),
        )
        with patch("app.models.account.Config") as config:
            config.return_value.database = self.db
            Account.cleanup_expired_sessions()

        self.assertEqual(
            self.db.read_execute(
                "SELECT COUNT(*) FROM user_refresh_tokens WHERE token_hash=?",
                (hashlib.sha256(first["refreshToken"].encode()).hexdigest(),),
            )[0][0],
            0,
        )
        self.assertIsNotNone(self.account.authenticate_token(rotated["token"]))

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
