"""ZenStream user accounts, bearer sessions, and library grants."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import Config


_hasher = PasswordHasher()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Account:
    SESSION_DAYS = 7

    def __init__(self):
        self.db = Config().database

    def _row(self, *, user_id: str | None = None, username: str | None = None):
        if user_id:
            rows = self.db.execute(
                "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE id=?",
                (user_id,),
            )
        else:
            rows = self.db.execute(
                "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE username=?",
                ((username or "").strip(),),
            )
        return rows[0] if rows else None

    @staticmethod
    def _public(row) -> dict:
        return {"id": row[0], "username": row[1], "disabled": bool(row[4])}

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
        return self._public(self._row(user_id=user_id))

    def authenticate_password(self, username: str, password: str) -> dict | None:
        row = self._row(username=username)
        if not row or row[4]:
            return None
        scheme = row[3] or "sha256"
        valid = False
        if scheme == "argon2id":
            try:
                valid = _hasher.verify(row[2], password)
            except (VerifyMismatchError, InvalidHashError):
                valid = False
        else:
            valid = secrets.compare_digest(
                row[2], hashlib.sha256(password.encode("utf-8")).hexdigest()
            )
        if not valid:
            return None
        if scheme != "argon2id" or _hasher.check_needs_rehash(row[2]):
            self.db.execute(
                "UPDATE users SET password=?,password_scheme='argon2id' WHERE id=?",
                (_hasher.hash(password), row[0]),
            )
            row = self._row(user_id=row[0])
        return self._public(row)

    def create_session(self, user_id: str) -> dict:
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
        return {"token": token, "expiresAt": _iso(expires)}

    def authenticate_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = _iso()
        self.db.execute("DELETE FROM user_sessions WHERE expires_at<=?", (now,))
        rows = self.db.execute(
            "SELECT u.id,u.username,u.password,u.password_scheme,COALESCE(u.disabled,0),s.id "
            "FROM user_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND COALESCE(u.disabled,0)=0",
            (_token_hash(token), now),
        )
        if not rows:
            return None
        self.db.execute(
            "UPDATE user_sessions SET last_seen_at=? WHERE id=?", (now, rows[0][5])
        )
        return self._public(rows[0])

    def revoke(self, token: str) -> None:
        self.db.execute(
            "DELETE FROM user_sessions WHERE token_hash=?", (_token_hash(token),)
        )

    def revoke_user(self, user_id: str) -> None:
        self.db.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))

    def list(self) -> list[dict]:
        values = []
        for row in self.db.execute(
            "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users ORDER BY username"
        ):
            value = self._public(row)
            value["libraryIds"] = self.library_ids(row[0])
            values.append(value)
        return values

    def set_password(self, user_id: str, password: str) -> dict:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if not self._row(user_id=user_id):
            raise KeyError("User not found.")
        self.db.execute(
            "UPDATE users SET password=?,password_scheme='argon2id',disabled=0 WHERE id=?",
            (_hasher.hash(password), user_id),
        )
        self.revoke_user(user_id)
        return self._public(self._row(user_id=user_id))

    def set_disabled(self, user_id: str, disabled: bool) -> dict:
        if not self._row(user_id=user_id):
            raise KeyError("User not found.")
        self.db.execute(
            "UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id)
        )
        if disabled:
            self.revoke_user(user_id)
        return self._public(self._row(user_id=user_id))

    def delete(self, user_id: str) -> bool:
        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM users WHERE id=?", (user_id,))
            return cursor.rowcount == 1

    def library_ids(self, user_id: str) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
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
