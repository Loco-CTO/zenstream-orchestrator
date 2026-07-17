from datetime import datetime, timedelta
from hashlib import sha256
from app.config import Config
from app.modules.token import Token


class User:
    def __init__(self, username: str, password: str = None):
        self.username = username
        self.password = sha256(password.encode()).hexdigest() if password else None
        self._db = Config()._database

    @staticmethod
    def hash_password(password: str) -> str:
        return sha256(password.encode("utf-8")).hexdigest()

    def authenticate(self, token: str) -> bool:
        """Authenticate user with token"""
        self._db.execute(
            """
            DELETE FROM client_secrets
            WHERE username = ?
            AND datetime(expiration) < datetime('now')
        """,
            (self.username,),
        )

        check = self._db.execute(
            "SELECT client_secret FROM client_secrets WHERE username = ?",
            (self.username,),
        )

        for i in check:
            if i[0] == token:
                return True

        return False

    def register(self, inviteid: str) -> tuple[bool, bool]:
        """Register new user with invite"""
        if not self._db.execute("SELECT * FROM invites WHERE url = ?", (inviteid,)):
            return False, True

        try:
            self._db.execute(
                "INSERT INTO users (username, password, disabled) VALUES (?, ?, 0)",
                (self.username, self.password),
            )
            return True, False
        except Exception:
            return False, False

    def login(self, password: str) -> str | bool:
        """Login user and return token"""
        check = self._db.execute(
            "SELECT * FROM users WHERE username = ? AND password = ? AND (disabled = 0 OR disabled IS NULL)",
            (self.username, password),
        )

        if check:
            self._db.execute(
                """
            DELETE FROM client_secrets
            WHERE username = ?
            AND datetime(expiration) < datetime('now')
            """,
                (self.username,),
            )

            token = Token.generate_token()

            self._db.execute(
                "INSERT INTO client_secrets VALUES (?, ?, ?)",
                (self.username, token, str(datetime.now() + timedelta(days=7))),
            )
            return token
        return False

    @classmethod
    def list_accounts(cls) -> list[dict]:
        db = Config().database
        return [{"username": row[0], "disabled": bool(row[1])} for row in db.execute("SELECT username, COALESCE(disabled, 0) FROM users ORDER BY username")]

    @classmethod
    def set_disabled_account(cls, username: str, disabled: bool) -> bool:
        db = Config().database
        changed = db.execute("UPDATE users SET disabled = ? WHERE username = ?", (int(disabled), username))
        if disabled:
            db.execute("DELETE FROM client_secrets WHERE username = ?", (username,))
        return bool(changed)

    @classmethod
    def reset_password(cls, username: str, password: str) -> bool:
        db = Config().database
        changed = db.execute("UPDATE users SET password = ?, disabled = 0 WHERE username = ?", (cls.hash_password(password), username))
        db.execute("DELETE FROM client_secrets WHERE username = ?", (username,))
        return bool(changed)

    @classmethod
    def delete_account(cls, username: str) -> bool:
        db = Config().database
        changed = db.execute("DELETE FROM users WHERE username = ?", (username,))
        db.execute("DELETE FROM client_secrets WHERE username = ?", (username,))
        return bool(changed)

    def logout(self, token: str) -> bool:
        """Logout user by removing token"""
        operation = self._db.execute(
            "DELETE FROM client_secrets WHERE client_secret = ?",
            (token,),
        )

        if operation:
            return True
        return False

    def info(self) -> dict:
        """Return user info"""
        try:
            data = self._db.execute(
            "SELECT * FROM users WHERE username = ?",
                (self.username,),
            )
            return {
                "username": data[0][0],
                "password": data[0][1],
                "disabled": bool(data[0][2]) if len(data[0]) > 2 else False,
            }
        except Exception:
            return {}
