from app.config import Config

import re


SUPPORTED_LOCALES = {"en", "ja"}
DEFAULT_SUBTITLE_STYLE = {
    "textScale": 100,
    "fontColor": "#ffffff",
    "borderSize": 0,
    "borderColor": "#000000",
    "backgroundColor": "#000000",
    "backgroundOpacity": 0,
}
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


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

    def get_subtitle_style(self) -> dict:
        rows = self._db.execute(
            "SELECT subtitle_text_scale, subtitle_font_color, subtitle_border_size, subtitle_border_color, subtitle_background_color, subtitle_background_opacity FROM user_preferences WHERE jellyfin_user_id = ?",
            (self.jellyfin_user_id,),
        )
        if not rows:
            return dict(DEFAULT_SUBTITLE_STYLE)
        row = rows[0]
        return {
            "textScale": row[0], "fontColor": row[1], "borderSize": row[2],
            "borderColor": row[3], "backgroundColor": row[4], "backgroundOpacity": row[5],
        }

    def set_subtitle_style(self, style: dict) -> dict:
        normalized = _validate_subtitle_style(style)
        self._db.execute(
            """INSERT INTO user_preferences (jellyfin_user_id, subtitle_text_scale, subtitle_font_color, subtitle_border_size, subtitle_border_color, subtitle_background_color, subtitle_background_opacity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(jellyfin_user_id) DO UPDATE SET subtitle_text_scale=excluded.subtitle_text_scale, subtitle_font_color=excluded.subtitle_font_color, subtitle_border_size=excluded.subtitle_border_size, subtitle_border_color=excluded.subtitle_border_color, subtitle_background_color=excluded.subtitle_background_color, subtitle_background_opacity=excluded.subtitle_background_opacity""",
            (self.jellyfin_user_id, normalized["textScale"], normalized["fontColor"], normalized["borderSize"], normalized["borderColor"], normalized["backgroundColor"], normalized["backgroundOpacity"]),
        )
        return normalized


def _validate_subtitle_style(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Subtitle style must be an object.")
    result = dict(DEFAULT_SUBTITLE_STYLE)
    for key in result:
        if key in value:
            result[key] = value[key]
    if not isinstance(result["textScale"], (int, float)) or not 50 <= result["textScale"] <= 200:
        raise ValueError("textScale must be between 50 and 200.")
    if not isinstance(result["borderSize"], (int, float)) or not 0 <= result["borderSize"] <= 8:
        raise ValueError("borderSize must be between 0 and 8.")
    if not isinstance(result["backgroundOpacity"], (int, float)) or not 0 <= result["backgroundOpacity"] <= 100:
        raise ValueError("backgroundOpacity must be between 0 and 100.")
    for key in ("fontColor", "borderColor", "backgroundColor"):
        if not isinstance(result[key], str) or not _HEX_COLOR.fullmatch(result[key]):
            raise ValueError(f"{key} must be a six-digit hex color.")
    return result
