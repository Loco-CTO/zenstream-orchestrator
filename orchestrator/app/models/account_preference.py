"""Preferences owned by Orchestrator accounts."""

from __future__ import annotations

from app.config import Config
from app.models.metadata import MetadataLanguageSettings, normalize_metadata_locale
from app.models.subtitle_style import DEFAULT_SUBTITLE_STYLE, SUPPORTED_LOCALES, validate_subtitle_style


class AccountPreference:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.db = Config().database

    def _ensure(self) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO account_preferences(user_id) VALUES(?)",
            (self.user_id,),
        )

    def locale(self) -> str:
        rows = self.db.execute("SELECT locale FROM account_preferences WHERE user_id=?", (self.user_id,))
        return rows[0][0] if rows and rows[0][0] in SUPPORTED_LOCALES else "en"

    def set_locale(self, locale: str) -> str:
        if locale not in SUPPORTED_LOCALES:
            raise ValueError("Unsupported locale.")
        self._ensure()
        self.db.execute("UPDATE account_preferences SET locale=? WHERE user_id=?", (locale, self.user_id))
        return locale

    @staticmethod
    def _automatic(interface_locale: str, configured: list[str]) -> str:
        if interface_locale in configured:
            return interface_locale
        base = interface_locale.lower().split("-", 1)[0]
        match = next((value for value in configured if value.lower().split("-", 1)[0] == base), None)
        return match or "en"

    def metadata_language(self) -> dict:
        configured = MetadataLanguageSettings().get()
        rows = self.db.execute(
            "SELECT metadata_language,locale FROM account_preferences WHERE user_id=?",
            (self.user_id,),
        )
        explicit = rows[0][0] if rows else None
        locale = rows[0][1] if rows else "en"
        if explicit not in configured:
            explicit = None
        return {
            "mode": "explicit" if explicit else "auto",
            "language": explicit or self._automatic(locale, configured),
        }

    def set_metadata_language(self, language: str | None) -> dict:
        configured = MetadataLanguageSettings().get()
        normalized = normalize_metadata_locale(language) if language is not None else None
        if normalized is not None and normalized not in configured:
            raise ValueError("Metadata language is not configured.")
        self._ensure()
        self.db.execute(
            "UPDATE account_preferences SET metadata_language=? WHERE user_id=?",
            (normalized, self.user_id),
        )
        return self.metadata_language()

    def subtitle_style(self) -> dict:
        rows = self.db.execute(
            "SELECT subtitle_font_family,subtitle_bold,subtitle_text_scale,subtitle_font_color,subtitle_border_size,subtitle_border_color,subtitle_background_color,subtitle_background_opacity FROM account_preferences WHERE user_id=?",
            (self.user_id,),
        )
        if not rows:
            return dict(DEFAULT_SUBTITLE_STYLE)
        row = rows[0]
        return {
            "fontFamily": row[0], "bold": bool(row[1]), "textScale": row[2],
            "fontColor": row[3], "borderSize": row[4], "borderColor": row[5],
            "backgroundColor": row[6], "backgroundOpacity": row[7],
        }

    def set_subtitle_style(self, value: dict) -> dict:
        style = validate_subtitle_style(value)
        self._ensure()
        self.db.execute(
            "UPDATE account_preferences SET subtitle_font_family=?,subtitle_bold=?,subtitle_text_scale=?,subtitle_font_color=?,subtitle_border_size=?,subtitle_border_color=?,subtitle_background_color=?,subtitle_background_opacity=? WHERE user_id=?",
            (style["fontFamily"], int(style["bold"]), style["textScale"], style["fontColor"], style["borderSize"], style["borderColor"], style["backgroundColor"], style["backgroundOpacity"], self.user_id),
        )
        return style
