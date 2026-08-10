import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.config import Config
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


_hasher = PasswordHasher()
ADMIN_SESSION_COOKIE = "__Host-zenstream-admin"


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Admin:
    """Local orchestrator administrator accounts and sessions."""

    def __init__(self, username: str):
        self.username = username
        self._db = Config().database

    @staticmethod
    def hash_password(password: str) -> str:
        return _hasher.hash(password)

    @staticmethod
    def _password_valid(stored: str, scheme: str, password: str) -> bool:
        if scheme == "argon2id":
            try:
                return _hasher.verify(stored, password)
            except (VerifyMismatchError, InvalidHashError):
                return False
        return secrets.compare_digest(
            stored, hashlib.sha256(password.encode("utf-8")).hexdigest()
        )

    @classmethod
    def bootstrap(cls) -> tuple[str, str] | None:
        db = Config().database
        if db.execute("SELECT username FROM admins LIMIT 1"):
            return None
        username = "root"
        while db.execute("SELECT username FROM admins WHERE username = ?", (username,)):
            username = f"root-{secrets.token_hex(2)}"
        password = secrets.token_urlsafe(18)
        db.execute(
            "INSERT INTO admins (username, password, is_root, disabled, password_scheme) VALUES (?, ?, 1, 0, 'argon2id')",
            (username, cls.hash_password(password)),
        )
        return username, password

    def login(self, password: str) -> str | bool:
        row = self._db.execute(
            "SELECT password,COALESCE(password_scheme,'sha256') FROM admins WHERE username = ? AND disabled = 0",
            (self.username,),
        )
        if not row or not self._password_valid(row[0][0], row[0][1], password):
            return False
        token = secrets.token_urlsafe(32)
        expires_at = _iso(datetime.now(timezone.utc) + timedelta(days=7))
        with self._db.transaction() as cursor:
            if row[0][1] != "argon2id" or _hasher.check_needs_rehash(row[0][0]):
                cursor.execute(
                    "UPDATE admins SET password=?,password_scheme='argon2id' WHERE username=?",
                    (self.hash_password(password), self.username),
                )
            cursor.execute(
                "INSERT INTO admin_sessions (username, token_hash, expires_at) VALUES (?, ?, ?)",
                (self.username, _token_hash(token), expires_at),
            )
        return token

    def authenticate(self, token: str) -> bool:
        return self._username_for_token(self._db, token) == self.username

    @staticmethod
    def _username_for_token(db, token: str | None) -> str | None:
        if not isinstance(token, str) or not token:
            return None
        reader = getattr(db, "read_execute", db.execute)
        rows = reader(
            "SELECT s.username FROM admin_sessions s JOIN admins a ON a.username=s.username "
            "WHERE s.token_hash=? AND s.expires_at>? AND a.disabled=0",
            (_token_hash(token), _iso()),
        )
        return rows[0][0] if rows else None

    @classmethod
    def from_token(cls, token: str | None) -> "Admin | None":
        if not isinstance(token, str) or not token:
            return None
        db = Config().database
        username = cls._username_for_token(db, token)
        return cls(username) if username else None

    def logout(self, token: str) -> bool:
        with self._db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?", (_token_hash(token),)
            )
            return cursor.rowcount > 0

    def list_accounts(self) -> list[dict]:
        reader = getattr(self._db, "read_execute", self._db.execute)
        return [
            {"username": row[0], "is_root": bool(row[1]), "disabled": bool(row[2])}
            for row in reader(
                "SELECT username, is_root, disabled FROM admins ORDER BY username"
            )
        ]

    def create(self, username: str, password: str) -> bool:
        try:
            with self._db.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO admins (username, password, is_root, disabled, password_scheme) VALUES (?, ?, 0, 0, 'argon2id')",
                    (username.strip(), self.hash_password(password)),
                )
                return cursor.rowcount == 1
        except Exception:
            return False

    def set_disabled(self, username: str, disabled: bool) -> bool:
        with self._db.transaction() as cursor:
            cursor.execute(
                "UPDATE admins SET disabled = ? WHERE username = ? AND is_root = 0",
                (int(disabled), username),
            )
            changed = cursor.rowcount == 1
            if changed and disabled:
                cursor.execute(
                    "DELETE FROM admin_sessions WHERE username = ?", (username,)
                )
        return changed

    def rotate_password(self, username: str, password: str) -> bool:
        with self._db.transaction() as cursor:
            cursor.execute(
                "UPDATE admins SET password = ?, password_scheme='argon2id', disabled = 0 WHERE username = ?",
                (self.hash_password(password), username),
            )
            changed = cursor.rowcount == 1
            if changed:
                cursor.execute(
                    "DELETE FROM admin_sessions WHERE username = ?", (username,)
                )
        return changed

    def profile(self) -> dict | None:
        reader = getattr(self._db, "read_execute", self._db.execute)
        rows = reader(
            "SELECT username, is_root, disabled FROM admins WHERE username = ?",
            (self.username,),
        )
        if not rows:
            return None
        return {
            "username": rows[0][0],
            "is_root": bool(rows[0][1]),
            "disabled": bool(rows[0][2]),
        }

    def update_profile(
        self, new_username: str | None, new_password: str | None, current_token: str
    ) -> dict:
        target = (new_username or self.username).strip()
        if not target:
            raise ValueError("Username cannot be empty.")
        if new_password is not None and len(new_password) < 8:
            raise ValueError("Password must be at least 8 characters.")
        try:
            with self._db.transaction() as cursor:
                if target != self.username:
                    cursor.execute(
                        "UPDATE admins SET username = ? WHERE username = ?",
                        (target, self.username),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Username is already in use.")
                    cursor.execute(
                        "UPDATE admin_sessions SET username = ? WHERE username = ?",
                        (target, self.username),
                    )
                if new_password is not None:
                    cursor.execute(
                        "UPDATE admins SET password = ?,password_scheme='argon2id' WHERE username = ?",
                        (self.hash_password(new_password), target),
                    )
                    cursor.execute(
                        "DELETE FROM admin_sessions WHERE username = ? AND token_hash != ?",
                        (target, _token_hash(current_token)),
                    )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise ValueError("Username is already in use.") from error
            raise
        return {"username": target, "token": current_token}
