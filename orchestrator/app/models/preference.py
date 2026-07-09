from app.config import Config


SUPPORTED_LOCALES = {"en", "ja"}


class UserPreference:
    def __init__(self, jellyfin_user_id: str):
        self.jellyfin_user_id = jellyfin_user_id
        self._db = Config().database

    def get_locale(self) -> str:
        rows = self._db.execute(
            "SELECT locale FROM user_preferences WHERE jellyfin_user_id = ?",
            (self.jellyfin_user_id,),
        )
        return rows[0][0] if rows else "en"

    def set_locale(self, locale: str) -> str:
        if locale not in SUPPORTED_LOCALES:
            raise ValueError("Unsupported locale.")
        self._db.execute(
            """
            INSERT INTO user_preferences (jellyfin_user_id, locale)
            VALUES (?, ?)
            ON CONFLICT(jellyfin_user_id) DO UPDATE SET locale = excluded.locale
            """,
            (self.jellyfin_user_id, locale),
        )
        return locale
