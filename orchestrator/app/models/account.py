from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.config import Config
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Account:
    SESSION_DAYS = 7
    MAX_PENDING_SESSION_TOUCHES = 10_000
    _pending_session_touches: dict[str, str] = {}
    _session_touch_deadlines: dict[str, float] = {}
    _session_touch_lock = threading.Lock()

    def __init__(self):
        self.db = Config().database

    def _row(
        self,
        *,
        user_id: str | None = None,
        username: str | None = None,
        read_only: bool = False,
    ):
        execute = self.db.read_execute if read_only else self.db.execute
        if user_id:
            rows = execute(
                "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE id=?",
                (user_id,),
            )
        else:
            rows = execute(
                "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE username=?",
                ((username or "").strip(),),
            )
        return rows[0] if rows else None

    @staticmethod
    def _public(row) -> dict:
        return {"id": row[0], "username": row[1], "disabled": bool(row[4])}

    def public(self, row) -> dict:
        value = self._public(row)
        from app.avatar import UserAvatarStore

        value["avatarVersion"] = UserAvatarStore(self.db).version(row[0])
        return value

    def create(self, username: str, password: str) -> dict:
        username = username.strip()
        if not username or len(password) < 8:
            raise ValueError(
                "A username and password of at least 8 characters are required."
            )
        user_id = str(uuid.uuid4())
        try:
            with self.db.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO users(id,username,password,password_scheme,disabled) VALUES(?,?,?,?,0)",
                    (user_id, username, _hasher.hash(password), "argon2id"),
                )
        except Exception as error:
            raise ValueError("Username is already in use.") from error
        return self.public(self._row(user_id=user_id, read_only=True))

    @staticmethod
    def _password_matches(row, password: str) -> bool:
        scheme = row[3] or "sha256"
        if scheme == "argon2id":
            try:
                return _hasher.verify(row[2], password)
            except (VerifyMismatchError, InvalidHashError):
                return False
        return secrets.compare_digest(
            row[2], hashlib.sha256(password.encode("utf-8")).hexdigest()
        )

    def authenticate_password(self, username: str, password: str) -> dict | None:
        row = self._row(username=username, read_only=True)
        if not row or row[4]:
            return None
        scheme = row[3] or "sha256"
        valid = self._password_matches(row, password)
        if not valid:
            return None
        if scheme != "argon2id" or _hasher.check_needs_rehash(row[2]):
            self.db.execute(
                "UPDATE users SET password=?,password_scheme='argon2id' WHERE id=?",
                (_hasher.hash(password), row[0]),
            )
            row = self._row(user_id=row[0], read_only=True)
        return self.public(row)

    def create_session(
        self,
        user_id: str,
        device_metadata: dict | None = None,
        ip_address: str | None = None,
    ) -> dict:
        token = secrets.token_urlsafe(48)
        session_id = str(uuid.uuid4())
        now = _now()
        expires = now + timedelta(days=self.SESSION_DAYS)
        self.db.execute(
            "INSERT INTO user_sessions(id,user_id,token_hash,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (
                session_id,
                user_id,
                _token_hash(token),
                _iso(expires),
                _iso(now),
                _iso(now),
            ),
        )
        # Device rows are additive to the bearer-session contract.  The
        # lightweight account fixtures used by older tests may not have the
        # viewer tables yet, so the store safely reports unavailable there.
        from app.models.playback_viewer import PlaybackViewerStore

        PlaybackViewerStore(self.db).ensure_device(
            user_id,
            device_metadata,
            ip_address,
            session_id,
        )
        # Return the opaque session identifier alongside the bearer so
        # short-lived resource/socket tickets can be bound to this login.
        # Clients may ignore the field; it is never accepted as caller
        # identity on its own.
        return {"token": token, "expiresAt": _iso(expires), "sessionId": session_id}

    def authenticate_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = _iso()
        rows = self.db.read_execute(
            "SELECT u.id,u.username,u.password,u.password_scheme,COALESCE(u.disabled,0),s.id "
            "FROM user_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND COALESCE(u.disabled,0)=0",
            (_token_hash(token), now),
        )
        if not rows:
            return None
        self._queue_session_touch(rows[0][5], now)
        return self.public(rows[0])

    @classmethod
    def _queue_session_touch(cls, session_id: str, seen_at: str) -> None:
        now = time.monotonic()
        with cls._session_touch_lock:
            if now < cls._session_touch_deadlines.get(session_id, 0):
                return
            cls._session_touch_deadlines[session_id] = now + 600
            cls._pending_session_touches[session_id] = seen_at
            if len(cls._pending_session_touches) > cls.MAX_PENDING_SESSION_TOUCHES:
                oldest = next(
                    key
                    for key in cls._pending_session_touches
                    if key != session_id
                )
                cls._pending_session_touches.pop(oldest, None)
                cls._session_touch_deadlines.pop(oldest, None)

    @classmethod
    def _forget_session_ids(cls, session_ids) -> None:
        with cls._session_touch_lock:
            for session_id in session_ids:
                cls._pending_session_touches.pop(session_id, None)
                cls._session_touch_deadlines.pop(session_id, None)

    def _session_ids_for_user(self, user_id: str) -> list[str]:
        try:
            return [
                row[0]
                for row in self.db.read_execute(
                    "SELECT id FROM user_sessions WHERE user_id=?", (user_id,)
                )
            ]
        except Exception as error:
            # Keep account deletion/password workflows compatible with the
            # minimal pre-session databases used by migration/fixture code.
            if "no such table: user_sessions" in str(error):
                return []
            raise

    @classmethod
    def flush_session_activity(cls, limit: int = 100) -> int:
        with cls._session_touch_lock:
            pending = list(cls._pending_session_touches.items())[:limit]
            for session_id, _ in pending:
                cls._pending_session_touches.pop(session_id, None)
                cls._session_touch_deadlines.pop(session_id, None)
        if not pending:
            return 0
        db = Config().database
        try:
            with db.transaction() as cursor:
                cursor.executemany(
                    "UPDATE user_sessions SET last_seen_at=? WHERE id=?",
                    [(seen_at, session_id) for session_id, seen_at in pending],
                )
        except Exception:
            # Do not lose a touch when a maintenance transaction is briefly
            # unavailable.  The deadline is also restored so the next
            # authenticated request can enqueue it again.
            with cls._session_touch_lock:
                retry_at = time.monotonic() + 600
                for session_id, seen_at in pending:
                    cls._pending_session_touches.setdefault(session_id, seen_at)
                    cls._session_touch_deadlines.setdefault(session_id, retry_at)
            raise
        return len(pending)

    @classmethod
    def cleanup_expired_sessions(cls) -> int:
        db = Config().database
        expired = [
            row[0]
            for row in db.read_execute(
                "SELECT id FROM user_sessions WHERE expires_at<=?", (_iso(),)
            )
        ]
        if expired:
            db.execute("DELETE FROM user_sessions WHERE expires_at<=?", (_iso(),))
            cls._forget_session_ids(expired)
        return len(expired)

    def revoke(self, token: str) -> None:
        rows = self.db.read_execute(
            "SELECT id FROM user_sessions WHERE token_hash=?", (_token_hash(token),)
        )
        self.db.execute(
            "DELETE FROM user_sessions WHERE token_hash=?", (_token_hash(token),)
        )
        self._forget_session_ids(row[0] for row in rows)

    def revoke_user(self, user_id: str) -> None:
        rows = self.db.read_execute(
            "SELECT id FROM user_sessions WHERE user_id=?", (user_id,)
        )
        self.db.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
        self._forget_session_ids(row[0] for row in rows)

    def list(self) -> list[dict]:
        values = []
        for row in self.db.read_execute(
            "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users ORDER BY username"
        ):
            value = self.public(row)
            value["libraryIds"] = self.library_ids(row[0])
            values.append(value)
        return values

    def set_password(self, user_id: str, password: str) -> dict:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if not self._row(user_id=user_id, read_only=True):
            raise KeyError("User not found.")
        self.db.execute(
            "UPDATE users SET password=?,password_scheme='argon2id',disabled=0 WHERE id=?",
            (_hasher.hash(password), user_id),
        )
        self.revoke_user(user_id)
        return self.public(self._row(user_id=user_id, read_only=True))

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        confirm_new_password: str,
    ) -> None:
        if len(new_password) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if new_password != confirm_new_password:
            raise ValueError("New passwords do not match.")

        row = self._row(user_id=user_id, read_only=True)
        if not row:
            raise KeyError("User not found.")
        if not current_password or not self._password_matches(row, current_password):
            raise ValueError("Current password is incorrect.")

        password_hash = _hasher.hash(new_password)
        session_ids = self._session_ids_for_user(user_id)
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE users SET password=?,password_scheme='argon2id',disabled=0 WHERE id=?",
                (password_hash, user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("User not found.")
            cursor.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
        self._forget_session_ids(session_ids)

    def set_disabled(self, user_id: str, disabled: bool) -> dict:
        if not self._row(user_id=user_id, read_only=True):
            raise KeyError("User not found.")
        self.db.execute(
            "UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id)
        )
        if disabled:
            self.revoke_user(user_id)
        return self.public(self._row(user_id=user_id, read_only=True))

    def delete(self, user_id: str) -> bool:
        from app.avatar import UserAvatarStore

        avatar_store = UserAvatarStore(self.db)
        avatar_record = avatar_store.record_for_cleanup(user_id)
        session_ids = self._session_ids_for_user(user_id)
        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM users WHERE id=?", (user_id,))
            deleted = cursor.rowcount == 1
        if deleted:
            self._forget_session_ids(session_ids)
            avatar_store.remove_path_for_deleted_user(user_id, avatar_record)
        return deleted

    def library_ids(self, user_id: str) -> list[str]:
        return [
            row[0]
            for row in self.db.read_execute(
                "SELECT library_id FROM user_library_access WHERE user_id=? ORDER BY library_id",
                (user_id,),
            )
        ]

    def set_library_ids(self, user_id: str, library_ids: list[str]) -> list[str]:
        if not self._row(user_id=user_id):
            raise KeyError("User not found.")
        requested = list(dict.fromkeys(str(value) for value in library_ids))
        if requested:
            found = {
                row[0]
                for row in self.db.execute(
                    f"SELECT id FROM libraries WHERE id IN ({','.join('?' for _ in requested)})",
                    requested,
                )
            }
            if found != set(requested):
                raise ValueError("One or more libraries do not exist.")
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM user_library_access WHERE user_id=?", (user_id,)
            )
            cursor.executemany(
                "INSERT INTO user_library_access(user_id,library_id,created_at) VALUES(?,?,?)",
                [(user_id, library_id, _iso()) for library_id in requested],
            )
        return self.library_ids(user_id)
