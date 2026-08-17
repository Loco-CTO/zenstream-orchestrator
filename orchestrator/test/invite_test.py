import unittest
from datetime import datetime, timedelta, timezone

from app.database import DatabaseHandler
from app.models.invite import Invite


class InviteTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.db.write_many(
            [
                (
                    "CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,password TEXT NOT NULL,password_scheme TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0)",
                    (),
                ),
                (
                    "CREATE TABLE user_sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL,last_seen_at TEXT NOT NULL)",
                    (),
                ),
                ("CREATE TABLE libraries(id TEXT PRIMARY KEY,name TEXT NOT NULL)", ()),
                (
                    "CREATE TABLE invites(id TEXT PRIMARY KEY,url TEXT UNIQUE NOT NULL,max_uses INTEGER,used_uses INTEGER NOT NULL,expires_at TEXT,created_at TEXT NOT NULL)",
                    (),
                ),
                (
                    "CREATE TABLE invite_library_access(invite_id TEXT NOT NULL,library_id TEXT NOT NULL,PRIMARY KEY(invite_id,library_id))",
                    (),
                ),
                (
                    "CREATE TABLE user_library_access(user_id TEXT NOT NULL,library_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(user_id,library_id))",
                    (),
                ),
            ]
        )
        self.db.write_many(
            [("INSERT INTO libraries(id,name) VALUES(?,?)", ("movies", "Movies"))]
        )
        self.invites = Invite.__new__(Invite)
        self.invites._db = self.db

    def tearDown(self):
        self.db.close()

    def test_registration_assigns_libraries_and_consumes_finite_invite(self):
        created = self.invites.create(["movies"], max_uses=1, expires_in_seconds=None)
        self.assertTrue(self.invites.validate(created["token"]))

        result = self.invites.register(created["token"], "alice", "password123")

        self.assertEqual(result["user"]["username"], "alice")
        self.assertFalse(self.invites.validate(created["token"]))
        self.assertEqual(
            self.db.execute("SELECT library_id FROM user_library_access"),
            [("movies",)],
        )
        self.assertEqual(self.db.execute("SELECT used_uses FROM invites"), [(1,)])

    def test_unlimited_invite_can_register_multiple_accounts(self):
        created = self.invites.create([], max_uses=None, expires_in_seconds=None)

        self.invites.register(created["token"], "alice", "password123")
        self.invites.register(created["token"], "bob", "password123")

        self.assertTrue(self.invites.validate(created["token"]))
        self.assertEqual(self.db.execute("SELECT used_uses FROM invites"), [(2,)])

    def test_duplicate_username_does_not_consume_invite(self):
        created = self.invites.create([], max_uses=2, expires_in_seconds=None)
        self.invites.register(created["token"], "alice", "password123")

        with self.assertRaises(ValueError):
            self.invites.register(created["token"], "alice", "password456")

        self.assertEqual(self.db.execute("SELECT used_uses FROM invites"), [(1,)])
        self.assertTrue(self.invites.validate(created["token"]))

    def test_expired_invite_is_not_usable_and_revoke_deletes_it(self):
        created = self.invites.create([], max_uses=1, expires_in_seconds=None)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.db.execute("UPDATE invites SET expires_at=?", (expired,))
        self.assertFalse(self.invites.validate(created["token"]))

        self.assertTrue(self.invites.delete(created["inviteId"]))
        self.assertEqual(self.db.execute("SELECT * FROM invites"), [])
        self.assertEqual(self.db.execute("SELECT * FROM invite_library_access"), [])


if __name__ == "__main__":
    unittest.main()
