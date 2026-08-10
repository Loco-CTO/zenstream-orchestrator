import hashlib
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.models.admin import Admin


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE admins (username TEXT PRIMARY KEY, password TEXT, is_root INTEGER, disabled INTEGER, password_scheme TEXT)"
        )
        self.db.execute(
            "CREATE TABLE admin_sessions (username TEXT, token_hash TEXT, expires_at TEXT)"
        )
        self.admin = Admin.__new__(Admin)
        self.admin.username = "root"
        self.admin._db = self

    def execute(self, query, params=()):
        cursor = self.db.execute(query, params)
        rows = cursor.fetchall()
        self.db.commit()
        return rows

    @contextmanager
    def transaction(self):
        cursor = self.db.cursor()
        try:
            yield cursor
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            cursor.close()

    def test_login_and_session_authentication_are_local(self):
        self.db.execute(
            "INSERT INTO admins VALUES (?, ?, 1, 0, 'argon2id')",
            ("root", Admin.hash_password("secret")),
        )
        token = self.admin.login("secret")
        self.assertTrue(token)
        self.assertTrue(self.admin.authenticate(token))
        self.assertFalse(self.admin.authenticate("jellyfin-token"))
        stored = self.db.execute("SELECT token_hash FROM admin_sessions").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(stored, hashlib.sha256(token.encode()).hexdigest())

    def test_disabled_admin_cannot_login(self):
        self.db.execute(
            "INSERT INTO admins VALUES (?, ?, 0, 1, 'argon2id')",
            ("root", Admin.hash_password("secret")),
        )
        self.assertFalse(self.admin.login("secret"))

    def test_expired_session_is_rejected(self):
        self.db.execute(
            "INSERT INTO admins VALUES (?, ?, 1, 0, 'argon2id')",
            ("root", Admin.hash_password("secret")),
        )
        token = self.admin.login("secret")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.db.execute("UPDATE admin_sessions SET expires_at=?", (expired,))
        self.assertFalse(self.admin.authenticate(token))

    def test_legacy_password_is_upgraded_after_login(self):
        legacy = hashlib.sha256(b"secret").hexdigest()
        self.db.execute(
            "INSERT INTO admins VALUES (?, ?, 1, 0, 'sha256')", ("root", legacy)
        )
        self.assertTrue(self.admin.login("secret"))
        password, scheme = self.db.execute(
            "SELECT password,password_scheme FROM admins WHERE username='root'"
        ).fetchone()
        self.assertEqual(scheme, "argon2id")
        self.assertNotEqual(password, legacy)

    def test_disabling_admin_revokes_sessions(self):
        self.db.execute(
            "INSERT INTO admins VALUES (?, ?, 0, 0, 'argon2id')",
            ("root", Admin.hash_password("secret")),
        )
        token = self.admin.login("secret")
        self.assertTrue(self.admin.set_disabled("root", True))
        self.assertFalse(self.admin.authenticate(token))
        self.assertEqual(self.db.execute("SELECT * FROM admin_sessions").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
