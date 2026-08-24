from __future__ import annotations

from app.config import Config
from app.language_registry import (
    language_options,
    normalize_metadata_locale,
    normalize_track_language,
)
from app.models.metadata import MetadataLanguageSettings


SUPPORTED_LOCALES = frozenset({"en", "ja"})


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
        rows = self.db.read_execute(
            "SELECT locale FROM account_preferences WHERE user_id=?", (self.user_id,)
        )
        return rows[0][0] if rows and rows[0][0] in SUPPORTED_LOCALES else "en"

    def set_locale(self, locale: str) -> str:
        if locale not in SUPPORTED_LOCALES:
            raise ValueError("Unsupported locale.")
        self._ensure()
        self.db.execute(
            "UPDATE account_preferences SET locale=? WHERE user_id=?",
            (locale, self.user_id),
        )
        return locale

    def watch_history(self) -> dict:
        rows = self.db.read_execute(
            "SELECT watch_history_enabled FROM account_preferences WHERE user_id=?",
            (self.user_id,),
        )
        return {"enabled": bool(rows[0][0]) if rows else True}

    def set_watch_history(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("Watch history enabled must be a boolean.")
        self._ensure()
        self.db.execute(
            "UPDATE account_preferences SET watch_history_enabled=? WHERE user_id=?",
            (int(enabled), self.user_id),
        )
        return self.watch_history()

    @staticmethod
    def _automatic(interface_locale: str, configured: list[str]) -> str:
        if not configured:
            return "en"
        if interface_locale in configured:
            return interface_locale
        base = interface_locale.lower().split("-", 1)[0]
        match = next(
            (value for value in configured if value.lower().split("-", 1)[0] == base),
            None,
        )
        return match or configured[0]

    def metadata_language(self) -> dict:
        configured = MetadataLanguageSettings().get()
        rows = self.db.read_execute(
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
        normalized = (
            normalize_metadata_locale(language) if language is not None else None
        )
        if normalized is not None and normalized not in configured:
            raise ValueError("Metadata language is not configured.")
        self._ensure()
        self.db.execute(
            "UPDATE account_preferences SET metadata_language=? WHERE user_id=?",
            (normalized, self.user_id),
        )
        return self.metadata_language()

    def _track_language_sets(self) -> tuple[set[str], set[str]]:
        rows = self.db.read_execute(
            "SELECT DISTINCT m.track_type,m.language "
            "FROM media_track_languages m "
            "JOIN media_files f ON f.id=m.media_file_id "
            "JOIN library_entities e ON e.id=f.entity_id "
            "JOIN user_library_access a ON a.library_id=e.library_id "
            "WHERE a.user_id=? "
            "UNION "
            "SELECT DISTINCT 'subtitle',f.language "
            "FROM media_files f "
            "JOIN library_entities e ON e.id=f.entity_id "
            "JOIN user_library_access a ON a.library_id=e.library_id "
            "WHERE a.user_id=? AND f.role='subtitle' AND f.language IS NOT NULL",
            (self.user_id, self.user_id),
        )
        audio: set[str] = set()
        subtitles: set[str] = set()
        for track_type, raw_language in rows:
            language = normalize_track_language(raw_language)
            if not language:
                continue
            if track_type == "audio":
                audio.add(language)
            elif track_type == "subtitle":
                subtitles.add(language)
        return audio, subtitles

    def playback(self) -> dict:
        audio_languages, subtitle_languages = self._track_language_sets()
        rows = self.db.read_execute(
            "SELECT audio_language,subtitle_language "
            "FROM account_preferences WHERE user_id=?",
            (self.user_id,),
        )
        audio_value = rows[0][0] if rows else None
        subtitle_value = rows[0][1] if rows else None
        if audio_value not in audio_languages:
            audio_value = None
        if subtitle_value != "off" and subtitle_value not in subtitle_languages:
            subtitle_value = None
        labels = {
            str(option["value"]): str(option["label"])
            for option in language_options(self.locale())
            if option.get("tracks")
        }
        return {
            "audioLanguage": audio_value,
            "subtitleLanguage": subtitle_value,
            "audioLanguages": [
                {"value": value, "label": labels.get(value, value)}
                for value in sorted(audio_languages)
            ],
            "subtitleLanguages": [
                {"value": value, "label": labels.get(value, value)}
                for value in sorted(subtitle_languages)
            ],
        }

    def set_playback(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("Playback preferences must be an object.")
        audio_languages, subtitle_languages = self._track_language_sets()
        current = self.playback()
        audio_value = value.get("audioLanguage", current["audioLanguage"])
        subtitle_value = value.get("subtitleLanguage", current["subtitleLanguage"])
        if audio_value is not None:
            audio_value = normalize_track_language(audio_value)
            if audio_value not in audio_languages:
                raise ValueError("Audio language is not available.")
        if subtitle_value not in {None, "off"}:
            subtitle_value = normalize_track_language(subtitle_value)
            if subtitle_value not in subtitle_languages:
                raise ValueError("Subtitle language is not available.")
        self._ensure()
        self.db.execute(
            "UPDATE account_preferences SET audio_language=?,subtitle_language=? WHERE user_id=?",
            (audio_value, subtitle_value, self.user_id),
        )
        return self.playback()
