from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from app.config import Config
from argon2 import PasswordHasher

_hasher = PasswordHasher()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Invite:
    """Hashed registration invites and their registration grants."""

    def __init__(self):
        self._db = Config().database

    @staticmethod
    def _valid_expiry(expires_at: str | None) -> bool:
        if not expires_at:
            return True
        try:
            return datetime.fromisoformat(expires_at) > _now()
        except ValueError:
            return False

    @classmethod
    def _usable(cls, max_uses: int | None, used_uses: int, expires_at: str | None):
        return cls._valid_expiry(expires_at) and (
            max_uses is None or used_uses < max_uses
        )

    def validate(self, token: str) -> bool:
        token = str(token or "").strip()
        if not token:
            return False
        rows = self._db.read_execute(
            "SELECT max_uses,used_uses,expires_at FROM invites WHERE url IN (?,?) LIMIT 1",
            (token, _digest(token)),
        )
        if not rows:
            return False
        max_uses, used_uses, expires_at = rows[0]
        return self._usable(max_uses, int(used_uses or 0), expires_at)

    def create(
        self,
        library_ids: list[str] | None = None,
        max_uses: int | None = 1,
        expires_in_seconds: int | None = 7 * 24 * 60 * 60,
    ) -> dict:
        requested_libraries = list(
            dict.fromkeys(str(value) for value in (library_ids or []))
        )
        if max_uses is not None and (not isinstance(max_uses, int) or max_uses < 1):
            raise ValueError("maxUses must be a positive integer or null.")
        if expires_in_seconds is not None and (
            not isinstance(expires_in_seconds, int) or expires_in_seconds < 1
        ):
            raise ValueError("expiresInSeconds must be a positive integer or null.")

        invite_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(48)
        expires_at = (
            _iso(_now() + timedelta(seconds=expires_in_seconds))
            if expires_in_seconds is not None
            else None
        )
        with self._db.transaction() as cursor:
            if requested_libraries:
                placeholders = ",".join("?" for _ in requested_libraries)
                found = {
                    row[0]
                    for row in cursor.execute(
                        f"SELECT id FROM libraries WHERE id IN ({placeholders})",
                        requested_libraries,
                    ).fetchall()
                }
                if found != set(requested_libraries):
                    raise ValueError("One or more libraries do not exist.")
            cursor.execute(
                "INSERT INTO invites(id,url,max_uses,used_uses,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (invite_id, _digest(token), max_uses, 0, expires_at, _iso()),
            )
            cursor.executemany(
                "INSERT INTO invite_library_access(invite_id,library_id) VALUES(?,?)",
                [(invite_id, library_id) for library_id in requested_libraries],
            )
        return {
            "inviteId": invite_id,
            "token": token,
            "maxUses": max_uses,
            "usedUses": 0,
            "expiresAt": expires_at,
            "libraryIds": requested_libraries,
        }

    def list(self) -> list[dict]:
        rows = self._db.read_execute(
            "SELECT i.id,i.url,i.max_uses,i.used_uses,i.expires_at,i.created_at,"
            "a.library_id,l.name "
            "FROM invites i "
            "LEFT JOIN invite_library_access a ON a.invite_id=i.id "
            "LEFT JOIN libraries l ON l.id=a.library_id "
            "ORDER BY i.created_at DESC,i.id,a.library_id"
        )
        values: dict[str, dict] = {}
        for (
            invite_id,
            token_hash,
            max_uses,
            used_uses,
            expires_at,
            created_at,
            library_id,
            library_name,
        ) in rows:
            value = values.setdefault(
                invite_id,
                {
                    "id": invite_id,
                    "tokenFingerprint": str(token_hash)[:12],
                    "maxUses": max_uses,
                    "usedUses": int(used_uses or 0),
                    "expiresAt": expires_at,
                    "createdAt": created_at,
                    "libraryIds": [],
                    "libraries": [],
                },
            )
            if library_id:
                value["libraryIds"].append(library_id)
                value["libraries"].append(
                    {"id": library_id, "name": library_name or library_id}
                )
        for value in values.values():
            if not self._usable(
                value["maxUses"], value["usedUses"], value["expiresAt"]
            ):
                value["status"] = (
                    "expired"
                    if not self._valid_expiry(value["expiresAt"])
                    else "exhausted"
                )
            else:
                value["status"] = "active"
        return list(values.values())

    def delete(self, invite_id: str) -> bool:
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM invites WHERE id=?", (str(invite_id).strip(),))
            return cursor.rowcount == 1

    def register(self, token: str, username: str, password: str) -> dict:
        token = str(token or "").strip()
        username = str(username or "").strip()
        password = str(password or "")
        if not username or len(password) < 8:
            raise ValueError(
                "A username and password of at least 8 characters are required."
            )
        if not token:
            raise PermissionError("Invalid invite.")

        with self._db.transaction() as cursor:
            row = cursor.execute(
                "SELECT id,max_uses,used_uses,expires_at FROM invites WHERE url=? LIMIT 1",
                (_digest(token),),
            ).fetchone()
            if not row:
                raise PermissionError("Invalid invite.")
            invite_id, max_uses, used_uses, expires_at = row
            used_uses = int(used_uses or 0)
            if not self._usable(max_uses, used_uses, expires_at):
                raise PermissionError("Invalid invite.")
            library_ids = [
                value[0]
                for value in cursor.execute(
                    "SELECT library_id FROM invite_library_access "
                    "WHERE invite_id=? ORDER BY library_id",
                    (invite_id,),
                ).fetchall()
            ]
            user_id = str(uuid.uuid4())
            try:
                cursor.execute(
                    "INSERT INTO users(id,username,password,password_scheme,disabled) "
                    "VALUES(?,?,?,?,0)",
                    (user_id, username, _hasher.hash(password), "argon2id"),
                )
            except Exception as error:
                raise ValueError("Username is already in use.") from error
            cursor.executemany(
                "INSERT INTO user_library_access(user_id,library_id,created_at) "
                "VALUES(?,?,?)",
                [(user_id, library_id, _iso()) for library_id in library_ids],
            )
            session_token = secrets.token_urlsafe(48)
            session_id = str(uuid.uuid4())
            session_expires = _now() + timedelta(days=7)
            cursor.execute(
                "INSERT INTO user_sessions(id,user_id,token_hash,expires_at,created_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    session_id,
                    user_id,
                    _digest(session_token),
                    _iso(session_expires),
                    _iso(),
                    _iso(),
                ),
            )
            cursor.execute(
                "UPDATE invites SET used_uses=? WHERE id=?",
                (used_uses + 1, invite_id),
            )
        return {
            "user": {"id": user_id, "username": username, "disabled": False},
            "sessionToken": session_token,
            "sessionExpiresAt": _iso(session_expires),
        }
