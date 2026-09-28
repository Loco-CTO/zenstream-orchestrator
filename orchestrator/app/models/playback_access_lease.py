from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

PLAYBACK_ACCESS_LEASE_TTL_SECONDS = 15 * 60
PLAYBACK_ACCESS_LEASE_PREFIX = "pl1_"


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PlaybackAccessLeaseStore:
    """Persist only hashes of stable, revocable playback resource handles."""

    def __init__(self, db):
        self.db = db

    @staticmethod
    def new_token() -> str:
        return PLAYBACK_ACCESS_LEASE_PREFIX + secrets.token_urlsafe(32)

    def create(
        self,
        token: str,
        user_id: str,
        auth_session_id: str,
        entity_id: str,
        source_id: str,
        playback_session_id: str | None = None,
    ) -> str:
        if not token.startswith(PLAYBACK_ACCESS_LEASE_PREFIX):
            raise ValueError("Invalid playback access lease token.")
        now = datetime.now(timezone.utc)
        expires_at = _iso(now + timedelta(seconds=PLAYBACK_ACCESS_LEASE_TTL_SECONDS))
        self.db.execute(
            "INSERT INTO playback_access_leases "
            "(id,token_hash,user_id,auth_session_id,entity_id,source_id,playback_session_id,created_at,expires_at,revoked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (
                str(uuid.uuid4()),
                _token_hash(token),
                user_id,
                auth_session_id,
                entity_id,
                source_id,
                playback_session_id,
                _iso(now),
                expires_at,
            ),
        )
        return expires_at

    def _row(self, token: str):
        rows = self.db.read_execute(
            "SELECT id,user_id,auth_session_id,entity_id,source_id,playback_session_id,expires_at,revoked_at "
            "FROM playback_access_leases WHERE token_hash=?",
            (_token_hash(token),),
        )
        return rows[0] if rows else None

    def validate(
        self,
        token: str,
        *,
        user_id: str | None = None,
        auth_session_id: str | None = None,
        entity_id: str | None = None,
        source_id: str | None = None,
        playback_session_id: str | None = None,
    ) -> dict:
        row = self._row(token)
        now = _iso()
        if not row or row[7] is not None or row[6] <= now:
            raise HTTPException(401, "Invalid or expired playback access lease.")
        lease = {
            "id": row[0],
            "userId": row[1],
            "authSessionId": row[2],
            "entityId": row[3],
            "sourceId": row[4],
            "playbackSessionId": row[5],
            "expiresAt": row[6],
        }
        if (
            (user_id is not None and lease["userId"] != user_id)
            or (auth_session_id is not None and lease["authSessionId"] != auth_session_id)
            or (entity_id is not None and lease["entityId"] != entity_id)
            or (source_id is not None and lease["sourceId"] != source_id)
            or lease["playbackSessionId"] != playback_session_id
        ):
            raise HTTPException(401, "Invalid or expired playback access lease.")

        from app.models.account import Account

        account = Account()
        if not account.session_is_valid(lease["authSessionId"], lease["userId"]):
            self.revoke_auth_session(lease["authSessionId"])
            raise HTTPException(401, "Invalid or expired playback access lease.")
        users = self.db.read_execute(
            "SELECT id,username,password,password_scheme,COALESCE(disabled,0) FROM users WHERE id=?",
            (lease["userId"],),
        )
        if not users or users[0][4]:
            raise HTTPException(401, "Account is unavailable.")

        if lease["playbackSessionId"] is not None:
            sessions = self.db.read_execute(
                "SELECT user_id,entity_id,source_id,state FROM playback_sessions WHERE id=?",
                (lease["playbackSessionId"],),
            )
            if (
                not sessions
                or sessions[0][0] != lease["userId"]
                or sessions[0][1] != lease["entityId"]
                or sessions[0][2] != lease["sourceId"]
                or sessions[0][3] in {"stopping", "failed", "expired"}
            ):
                self.revoke_playback_session(lease["playbackSessionId"])
                raise HTTPException(401, "Invalid or expired playback access lease.")
        return account.public(users[0])

    def renew(
        self,
        token: str,
        *,
        user_id: str,
        auth_session_id: str,
        entity_id: str,
        source_id: str,
        playback_session_id: str | None,
    ) -> str:
        self.validate(
            token,
            user_id=user_id,
            entity_id=entity_id,
            source_id=source_id,
            playback_session_id=playback_session_id,
        )
        row = self._row(token)
        if not row or row[2] != auth_session_id:
            raise HTTPException(401, "Invalid or expired playback access lease.")
        expires_at = _iso(
            datetime.now(timezone.utc)
            + timedelta(seconds=PLAYBACK_ACCESS_LEASE_TTL_SECONDS)
        )
        self.db.execute(
            "UPDATE playback_access_leases SET expires_at=? WHERE id=? AND revoked_at IS NULL",
            (expires_at, row[0]),
        )
        return expires_at

    def revoke_auth_session(self, auth_session_id: str) -> int:
        now = _iso()
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE playback_access_leases SET revoked_at=? WHERE auth_session_id=? AND revoked_at IS NULL",
                (now, auth_session_id),
            )
            return max(0, int(cursor.rowcount or 0))

    def revoke_playback_session(self, playback_session_id: str) -> int:
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE playback_access_leases SET revoked_at=? WHERE playback_session_id=? AND revoked_at IS NULL",
                (_iso(), playback_session_id),
            )
            return max(0, int(cursor.rowcount or 0))

    def cleanup_expired(self, retention_days: int = 30) -> int:
        now = datetime.now(timezone.utc)
        revoked_cutoff = _iso(now - timedelta(days=max(1, retention_days)))
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM playback_access_leases WHERE expires_at<=? OR revoked_at<=?",
                (_iso(now), revoked_cutoff),
            )
            return max(0, int(cursor.rowcount or 0))
