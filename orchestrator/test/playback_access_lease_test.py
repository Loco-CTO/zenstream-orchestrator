import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models.playback_access_lease import PlaybackAccessLeaseStore
from fastapi import HTTPException


class LeaseDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY, username TEXT, password TEXT,
                password_scheme TEXT, disabled INTEGER
            );
            CREATE TABLE user_sessions (
                id TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT, revoked_at TEXT
            );
            CREATE TABLE playback_sessions (
                id TEXT PRIMARY KEY, user_id TEXT, entity_id TEXT,
                source_id TEXT, state TEXT
            );
            CREATE TABLE playback_access_leases (
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, user_id TEXT,
                auth_session_id TEXT, entity_id TEXT, source_id TEXT,
                playback_session_id TEXT, created_at TEXT, expires_at TEXT,
                revoked_at TEXT
            );
            INSERT INTO users VALUES ('user-1','alice','hash','argon2id',0);
            INSERT INTO user_sessions VALUES ('auth-1','user-1','2999-01-01T00:00:00+00:00',NULL);
            INSERT INTO playback_sessions VALUES ('worker-1','user-1','entity-1','source-1','ready');
            """
        )

    def execute(self, query, params=None):
        cursor = self.connection.execute(query, params or ())
        self.connection.commit()
        return cursor.fetchall()

    def read_execute(self, query, params=None):
        return self.connection.execute(query, params or ()).fetchall()

    @contextmanager
    def transaction(self):
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


class PlaybackAccessLeaseTest(unittest.TestCase):
    def setUp(self):
        self.db = LeaseDatabase()
        self.store = PlaybackAccessLeaseStore(self.db)
        self.account = MagicMock()
        self.account.session_is_valid.return_value = True
        self.account.public.side_effect = lambda row: {"id": row[0], "username": row[1]}
        self.account_patch = patch(
            "app.models.account.Account", return_value=self.account
        )
        self.account_patch.start()

    def tearDown(self):
        self.account_patch.stop()
        self.db.connection.close()

    def create_lease(self, playback_session_id=None):
        token = self.store.new_token()
        self.store.create(
            token,
            "user-1",
            "auth-1",
            "entity-1",
            "source-1",
            playback_session_id,
        )
        return token

    def test_lease_is_hash_only_and_renewal_extends_the_same_token(self):
        token = self.create_lease()
        self.db.execute(
            "UPDATE playback_access_leases SET expires_at=?",
            ((datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),),
        )
        before = self.store._row(token)
        renewed = self.store.renew(
            token,
            user_id="user-1",
            auth_session_id="auth-1",
            entity_id="entity-1",
            source_id="source-1",
            playback_session_id=None,
        )

        after = self.store._row(token)
        self.assertEqual(after[0], before[0])
        self.assertNotEqual(after[1], token)
        self.assertGreater(renewed, before[6])
        self.assertEqual(after[6], renewed)
        self.assertEqual(self.store.validate(token)["id"], "user-1")

    def test_renewal_rejects_a_different_auth_session(self):
        token = self.create_lease()

        with self.assertRaises(HTTPException) as error:
            self.store.renew(
                token,
                user_id="user-1",
                auth_session_id="auth-2",
                entity_id="entity-1",
                source_id="source-1",
                playback_session_id=None,
            )

        self.assertEqual(error.exception.status_code, 401)

    def test_cleanup_removes_expired_leases_and_keeps_active_leases(self):
        active_token = self.create_lease()
        expired_token = self.create_lease()
        self.db.execute(
            "UPDATE playback_access_leases SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", self.store._row(expired_token)[0]),
        )

        self.assertEqual(self.store.cleanup_expired(), 1)
        self.assertIsNone(self.store._row(expired_token))
        self.assertIsNotNone(self.store._row(active_token))

    def test_lease_rejects_wrong_user_entity_and_source(self):
        token = self.create_lease()
        for scope in (
            {"user_id": "user-2"},
            {"entity_id": "entity-2"},
            {"source_id": "source-2"},
        ):
            with self.subTest(scope=scope), self.assertRaises(HTTPException) as error:
                self.store.validate(token, **scope)
            self.assertEqual(error.exception.status_code, 401)

    def test_expired_lease_is_rejected(self):
        token = self.create_lease()
        self.db.execute(
            "UPDATE playback_access_leases SET expires_at=?",
            ("2000-01-01T00:00:00+00:00",),
        )

        with self.assertRaises(HTTPException) as error:
            self.store.validate(token)

        self.assertEqual(error.exception.status_code, 401)

    def test_revoked_auth_session_rejects_and_revokes_lease(self):
        token = self.create_lease()
        self.account.session_is_valid.return_value = False

        with self.assertRaises(HTTPException) as error:
            self.store.validate(token)

        self.assertEqual(error.exception.status_code, 401)
        self.assertIsNotNone(self.store._row(token)[7])

    def test_stopped_transcode_session_rejects_and_revokes_lease(self):
        token = self.create_lease("worker-1")
        self.db.execute(
            "UPDATE playback_sessions SET state='stopping' WHERE id='worker-1'"
        )

        with self.assertRaises(HTTPException) as error:
            self.store.validate(token, playback_session_id="worker-1")

        self.assertEqual(error.exception.status_code, 401)
        self.assertIsNotNone(self.store._row(token)[7])

    def test_wrong_transcode_session_is_rejected(self):
        token = self.create_lease("worker-1")

        with self.assertRaises(HTTPException) as error:
            self.store.validate(token, playback_session_id="worker-2")

        self.assertEqual(error.exception.status_code, 401)
