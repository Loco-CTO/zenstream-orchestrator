import sqlite3
import unittest

from app.models.admin import Admin


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE admins (username TEXT PRIMARY KEY, password TEXT, is_root INTEGER, disabled INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE admin_sessions (username TEXT, token TEXT, expiration TEXT)"
        )
        self.admin = Admin.__new__(Admin)
        self.admin.username = "root"
        self.admin._db = self

    def execute(self, query, params=()):
        cursor = self.db.execute(query, params)
        rows = cursor.fetchall()
        self.db.commit()
        return rows

    def test_login_and_session_authentication_are_local(self):
        self.db.execute(
            "INSERT INTO admins VALUES (?, ?, 1, 0)",
            ("root", Admin.hash_password("secret")),
        )
        token = self.admin.login("secret")
        self.assertTrue(token)
        self.assertTrue(self.admin.authenticate(token))
        self.assertFalse(self.admin.authenticate("jellyfin-token"))

    def test_disabled_admin_cannot_login(self):
        self.db.execute(
            "INSERT INTO admins VALUES (?, ?, 0, 1)",
            ("root", Admin.hash_password("secret")),
        )
        self.assertFalse(self.admin.login("secret"))


if __name__ == "__main__":
    unittest.main()
