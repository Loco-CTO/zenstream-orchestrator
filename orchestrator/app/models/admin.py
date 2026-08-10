import hashlib
import secrets
from datetime import datetime, timedelta

from app.config import Config


class Admin:
    """Local orchestrator administrator accounts and sessions."""

    def __init__(self, username: str):
        self.username = username
        self._db = Config().database

    @staticmethod
    def hash_password(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

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
            "INSERT INTO admins (username, password, is_root, disabled) VALUES (?, ?, 1, 0)",
            (username, cls.hash_password(password)),
        )
        return username, password

    def login(self, password: str) -> str | bool:
        row = self._db.execute(
            "SELECT password FROM admins WHERE username = ? AND disabled = 0",
            (self.username,),
        )
        if not row or row[0][0] != self.hash_password(password):
            return False
        token = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO admin_sessions (username, token, expiration) VALUES (?, ?, ?)",
            (self.username, token, str(datetime.now() + timedelta(days=7))),
        )
        return token

    def authenticate(self, token: str) -> bool:
        reader = getattr(self._db, "read_execute", self._db.execute)
        return bool(
            reader(
                "SELECT 1 FROM admin_sessions s JOIN admins a ON a.username = s.username "
                "WHERE s.username = ? AND s.token = ? AND a.disabled = 0",
                (self.username, token),
            )
        )

    def logout(self, token: str) -> bool:
        return bool(
            self._db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
        )

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
            self._db.execute(
                "INSERT INTO admins (username, password, is_root, disabled) VALUES (?, ?, 0, 0)",
                (username.strip(), self.hash_password(password)),
            )
            return True
        except Exception:
            return False

    def set_disabled(self, username: str, disabled: bool) -> bool:
        return bool(
            self._db.execute(
                "UPDATE admins SET disabled = ? WHERE username = ? AND is_root = 0",
                (int(disabled), username),
            )
        )

    def rotate_password(self, username: str, password: str) -> bool:
        return bool(
            self._db.execute(
                "UPDATE admins SET password = ?, disabled = 0 WHERE username = ?",
                (self.hash_password(password), username),
            )
        )

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
                        "UPDATE admins SET password = ? WHERE username = ?",
                        (self.hash_password(new_password), target),
                    )
                    cursor.execute(
                        "DELETE FROM admin_sessions WHERE username = ? AND token != ?",
                        (target, current_token),
                    )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise ValueError("Username is already in use.") from error
            raise
        return {"username": target, "token": current_token}
