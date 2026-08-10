import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.config import Config


class Invite:
    def __init__(self):
        self._db = Config()._database

    def validate(self, inviteid: str) -> bool:
        """
        Validate an invite URL.

        Args:
            inviteid (str): The invite URL to validate

        Returns:
            bool: True if invite exists, False otherwise
        """
        digest = hashlib.sha256(inviteid.encode("utf-8")).hexdigest()
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(invites)")}
        expiry = ",expires_at" if "expires_at" in columns else ""
        result = self._db.execute(
            f"SELECT 1{expiry} FROM invites WHERE url IN (?, ?) LIMIT 1",
            (inviteid, digest),
        )
        if result and len(result[0]) > 1 and result[0][1]:
            try:
                return datetime.fromisoformat(result[0][1]) > datetime.now(timezone.utc)
            except ValueError:
                return False
        return bool(result)

    def generate(self) -> str:
        """
        Generate a new invite URL.

        Returns:
            str: The invite URL
        """
        inviteid = secrets.token_urlsafe(48)
        digest = hashlib.sha256(inviteid.encode("utf-8")).hexdigest()
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(invites)")}
        if "expires_at" in columns:
            expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            self._db.execute("INSERT INTO invites(url,expires_at) VALUES (?,?)", (digest, expires))
        else:
            self._db.execute("INSERT INTO invites(url) VALUES (?)", (digest,))
        return inviteid

    def delete(self, inviteid: str) -> bool:
        """
        Delete an invite URL.

        Args:
            inviteid (str): The invite URL to delete

        Returns:
            bool: True if deletion was successful, False otherwise
        """
        digest = hashlib.sha256(inviteid.encode("utf-8")).hexdigest()
        with self._db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM invites WHERE url IN (?, ?)", (inviteid, digest)
            )
            return cursor.rowcount == 1

    consume = delete
