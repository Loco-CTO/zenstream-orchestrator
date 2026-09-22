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


class RefreshTokenError(ValueError):
    pass


class Account:
    SESSION_DAYS = 90
    LEGACY_SESSION_DAYS = 7
    ACCESS_TOKEN_MINUTES = 15
    REFRESH_TOKEN_DAYS = 30
    MAX_PENDING_SESSION_TOUCHES = 10_000
    _pending_session_touches: dict[str, str] = {}
    _session_touch_deadlines: dict[str, float] = {}
    _session_touch_lock = threading.Lock()

    def __init__(self):
        self.db = Config().database

    def _session_columns(self) -> set[str]:
        try:
            return {
                row[1]
                for row in self.db.read_execute("PRAGMA table_info(user_sessions)")
            }
        except Exception:
            return set()

    def _supports_refresh_schema(self) -> bool:
        columns = self._session_columns()
        if not {
            "access_expires_at",
            "refresh_family_id",
            "revoked_at",
        }.issubset(columns):
            return False
        try:
            return bool(
                self.db.read_execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("user_refresh_tokens",),
                )
            )
        except Exception:
            return False

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
        supports_refresh: bool = False,
    ) -> dict:
        token = secrets.token_urlsafe(48)
        session_id = str(uuid.uuid4())
        now = _now()
        refresh_schema = self._supports_refresh_schema()
        refresh_enabled = supports_refresh and refresh_schema
        expires = now + timedelta(
            days=self.SESSION_DAYS if refresh_enabled else self.LEGACY_SESSION_DAYS
        )
        access_expires = (
            now + timedelta(minutes=self.ACCESS_TOKEN_MINUTES)
            if refresh_enabled
            else expires
        )
        refresh_token = None
        refresh_expires = None
        family_id = None
        if refresh_enabled:
            refresh_token = secrets.token_urlsafe(48)
            refresh_expires = now + timedelta(days=self.REFRESH_TOKEN_DAYS)
            family_id = str(uuid.uuid4())
            with self.db.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO user_sessions(id,user_id,token_hash,expires_at,created_at,last_seen_at,access_expires_at,refresh_family_id,revoked_at) VALUES(?,?,?,?,?,?,?,?,NULL)",
                    (
                        session_id,
                        user_id,
                        _token_hash(token),
                        _iso(expires),
                        _iso(now),
                        _iso(now),
                        _iso(access_expires),
                        family_id,
                    ),
                )
                cursor.execute(
                    "INSERT INTO user_refresh_tokens(id,session_id,user_id,family_id,token_hash,created_at,expires_at,used_at,revoked_at,replaced_by_id) VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                    (
                        str(uuid.uuid4()),
                        session_id,
                        user_id,
                        family_id,
                        _token_hash(refresh_token),
                        _iso(now),
                        _iso(refresh_expires),
                    ),
                )
        elif refresh_schema:
            self.db.execute(
                "INSERT INTO user_sessions(id,user_id,token_hash,expires_at,created_at,last_seen_at,access_expires_at,refresh_family_id,revoked_at) VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                (
                    session_id,
                    user_id,
                    _token_hash(token),
                    _iso(expires),
                    _iso(now),
                    _iso(now),
                    _iso(access_expires),
                ),
            )
        else:
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
        result = {
            "token": token,
            "expiresAt": _iso(access_expires),
            "expiresIn": max(1, int((access_expires - now).total_seconds())),
            "sessionId": session_id,
        }
        if refresh_token and refresh_expires:
            result.update(
                {
                    "refreshToken": refresh_token,
                    "refreshExpiresAt": _iso(refresh_expires),
                    "refreshExpiresIn": max(
                        1, int((refresh_expires - now).total_seconds())
                    ),
                    "sessionExpiresAt": _iso(expires),
                }
            )
        return result

    def authenticate_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = _iso()
        if self._supports_refresh_schema():
            rows = self.db.read_execute(
                "SELECT u.id,u.username,u.password,u.password_scheme,COALESCE(u.disabled,0),s.id "
                "FROM user_sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.access_expires_at>? AND s.expires_at>? "
                "AND s.revoked_at IS NULL AND COALESCE(u.disabled,0)=0",
                (_token_hash(token), now, now),
            )
        else:
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

    def session_id_for_token(
        self, token: str | None, *, require_access: bool = True
    ) -> str | None:
        if not token:
            return None
        if self._supports_refresh_schema():
            expiry_column = "access_expires_at" if require_access else "expires_at"
            rows = self.db.read_execute(
                f"SELECT id FROM user_sessions WHERE token_hash=? AND {expiry_column}>? "
                "AND expires_at>? AND revoked_at IS NULL",
                (_token_hash(token), _iso(), _iso()),
            )
        else:
            rows = self.db.read_execute(
                "SELECT id FROM user_sessions WHERE token_hash=? AND expires_at>?",
                (_token_hash(token), _iso()),
            )
        return rows[0][0] if rows else None

    def session_is_valid(self, session_id: str, user_id: str) -> bool:
        if not session_id or not user_id:
            return False
        if self._supports_refresh_schema():
            return bool(
                self.db.read_execute(
                    "SELECT 1 FROM user_sessions WHERE id=? AND user_id=? "
                    "AND expires_at>? AND revoked_at IS NULL",
                    (session_id, user_id, _iso()),
                )
            )
        return bool(
            self.db.read_execute(
                "SELECT 1 FROM user_sessions WHERE id=? AND user_id=? AND expires_at>?",
                (session_id, user_id, _iso()),
            )
        )

    def _session_response(
        self,
        *,
        token: str,
        refresh_token: str,
        session_id: str,
        access_expires: datetime,
        refresh_expires: datetime,
        session_expires: datetime,
        now: datetime,
        user: dict,
    ) -> dict:
        return {
            "token": token,
            "expiresAt": _iso(access_expires),
            "expiresIn": max(1, int((access_expires - now).total_seconds())),
            "sessionId": session_id,
            "refreshToken": refresh_token,
            "refreshExpiresAt": _iso(refresh_expires),
            "refreshExpiresIn": max(1, int((refresh_expires - now).total_seconds())),
            "sessionExpiresAt": _iso(session_expires),
            "user": user,
        }

    def refresh_session(
        self,
        refresh_token: str,
        device_metadata: dict | None = None,
        ip_address: str | None = None,
    ) -> dict:
        del device_metadata, ip_address
        if not refresh_token or not self._supports_refresh_schema():
            raise RefreshTokenError("Refresh token is invalid.")
        now = _now()
        now_iso = _iso(now)
        reused = False
        with self.db.transaction() as cursor:
            row = cursor.execute(
                "SELECT r.id,r.session_id,r.user_id,r.family_id,r.expires_at,r.used_at,r.revoked_at,"
                "s.expires_at,s.revoked_at,u.id,u.username,u.password,u.password_scheme,"
                "COALESCE(u.disabled,0) "
                "FROM user_refresh_tokens r "
                "JOIN user_sessions s ON s.id=r.session_id "
                "JOIN users u ON u.id=r.user_id "
                "WHERE r.token_hash=?",
                (_token_hash(refresh_token),),
            ).fetchone()
            if row is None:
                raise RefreshTokenError("Refresh token is invalid.")
            if row[5] is not None:
                cursor.execute(
                    "UPDATE user_refresh_tokens SET revoked_at=? "
                    "WHERE family_id=? AND revoked_at IS NULL",
                    (now_iso, row[3]),
                )
                cursor.execute(
                    "UPDATE user_sessions SET revoked_at=? "
                    "WHERE id=? AND revoked_at IS NULL",
                    (now_iso, row[1]),
                )
                reused = True
            elif (
                row[6] is not None
                or row[8] is not None
                or row[13]
                or row[4] <= now_iso
                or row[7] <= now_iso
            ):
                raise RefreshTokenError("Refresh token is invalid.")
            else:
                access = secrets.token_urlsafe(48)
                replacement = secrets.token_urlsafe(48)
                replacement_id = str(uuid.uuid4())
                access_expires = now + timedelta(minutes=self.ACCESS_TOKEN_MINUTES)
                refresh_expires = now + timedelta(days=self.REFRESH_TOKEN_DAYS)
                cursor.execute(
                    "UPDATE user_refresh_tokens SET used_at=?,replaced_by_id=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL",
                    (now_iso, replacement_id, row[0]),
                )
                if cursor.rowcount != 1:
                    raise RefreshTokenError("Refresh token is invalid.")
                cursor.execute(
                    "UPDATE user_sessions SET token_hash=?,access_expires_at=?,last_seen_at=? "
                    "WHERE id=? AND revoked_at IS NULL",
                    (_token_hash(access), _iso(access_expires), now_iso, row[1]),
                )
                if cursor.rowcount != 1:
                    raise RefreshTokenError("Refresh token is invalid.")
                cursor.execute(
                    "INSERT INTO user_refresh_tokens(id,session_id,user_id,family_id,token_hash,created_at,expires_at,used_at,revoked_at,replaced_by_id) VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                    (
                        replacement_id,
                        row[1],
                        row[2],
                        row[3],
                        _token_hash(replacement),
                        now_iso,
                        _iso(refresh_expires),
                    ),
                )
                user_row = row[9:14]
        if reused:
            raise RefreshTokenError("Refresh token is invalid.")
        user = self.public(user_row)
        return self._session_response(
            token=access,
            refresh_token=replacement,
            session_id=row[1],
            access_expires=access_expires,
            refresh_expires=refresh_expires,
            session_expires=datetime.fromisoformat(row[7]),
            now=now,
            user=user,
        )

    def upgrade_legacy_session(
        self,
        token: str,
        device_metadata: dict | None = None,
        ip_address: str | None = None,
    ) -> dict | None:
        del device_metadata, ip_address
        if not token or not self._supports_refresh_schema():
            return None
        now = _now()
        now_iso = _iso(now)
        with self.db.transaction() as cursor:
            row = cursor.execute(
                "SELECT s.id,s.user_id,s.expires_at,s.refresh_family_id,s.revoked_at,"
                "u.id,u.username,u.password,u.password_scheme,COALESCE(u.disabled,0) "
                "FROM user_sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.access_expires_at>? AND s.expires_at>?",
                (_token_hash(token), now_iso, now_iso),
            ).fetchone()
            if row is None or row[4] is not None or row[9]:
                return None
            if row[3] is not None:
                return None
            access = secrets.token_urlsafe(48)
            replacement = secrets.token_urlsafe(48)
            family_id = str(uuid.uuid4())
            refresh_id = str(uuid.uuid4())
            session_expires = now + timedelta(days=self.SESSION_DAYS)
            access_expires = now + timedelta(minutes=self.ACCESS_TOKEN_MINUTES)
            refresh_expires = now + timedelta(days=self.REFRESH_TOKEN_DAYS)
            cursor.execute(
                "UPDATE user_sessions SET token_hash=?,expires_at=?,access_expires_at=?,refresh_family_id=?,last_seen_at=? WHERE id=? AND revoked_at IS NULL",
                (
                    _token_hash(access),
                    _iso(session_expires),
                    _iso(access_expires),
                    family_id,
                    now_iso,
                    row[0],
                ),
            )
            if cursor.rowcount != 1:
                return None
            cursor.execute(
                "INSERT INTO user_refresh_tokens(id,session_id,user_id,family_id,token_hash,created_at,expires_at,used_at,revoked_at,replaced_by_id) VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (
                    refresh_id,
                    row[0],
                    row[1],
                    family_id,
                    _token_hash(replacement),
                    now_iso,
                    _iso(refresh_expires),
                ),
            )
            user_row = row[5:10]
        user = self.public(user_row)
        return self._session_response(
            token=access,
            refresh_token=replacement,
            session_id=row[0],
            access_expires=access_expires,
            refresh_expires=refresh_expires,
            session_expires=session_expires,
            now=now,
            user=user,
        )

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
                    key for key in cls._pending_session_touches if key != session_id
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
        try:
            db.execute("DELETE FROM user_refresh_tokens WHERE expires_at<=?", (_iso(),))
        except Exception as error:
            if "no such table: user_refresh_tokens" not in str(error):
                raise
        return len(expired)

    def revoke(self, token: str) -> None:
        rows = self.db.read_execute(
            "SELECT id FROM user_sessions WHERE token_hash=?", (_token_hash(token),)
        )
        if self._supports_refresh_schema():
            now = _iso()
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE user_sessions SET revoked_at=? WHERE token_hash=?",
                    (now, _token_hash(token)),
                )
                cursor.execute(
                    "UPDATE user_refresh_tokens SET revoked_at=? "
                    "WHERE session_id IN (SELECT id FROM user_sessions WHERE token_hash=?) "
                    "AND revoked_at IS NULL",
                    (now, _token_hash(token)),
                )
        else:
            self.db.execute(
                "DELETE FROM user_sessions WHERE token_hash=?", (_token_hash(token),)
            )
        self._forget_session_ids(row[0] for row in rows)

    def revoke_user(self, user_id: str) -> None:
        rows = self.db.read_execute(
            "SELECT id FROM user_sessions WHERE user_id=?", (user_id,)
        )
        if self._supports_refresh_schema():
            now = _iso()
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE user_sessions SET revoked_at=? WHERE user_id=?",
                    (now, user_id),
                )
                cursor.execute(
                    "UPDATE user_refresh_tokens SET revoked_at=? "
                    "WHERE user_id=? AND revoked_at IS NULL",
                    (now, user_id),
                )
        else:
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
            if self._supports_refresh_schema():
                revoked_at = _iso()
                cursor.execute(
                    "UPDATE user_sessions SET revoked_at=? WHERE user_id=?",
                    (revoked_at, user_id),
                )
                cursor.execute(
                    "UPDATE user_refresh_tokens SET revoked_at=? "
                    "WHERE user_id=? AND revoked_at IS NULL",
                    (revoked_at, user_id),
                )
            else:
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
