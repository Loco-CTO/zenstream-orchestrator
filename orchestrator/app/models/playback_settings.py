import os
from datetime import datetime, timezone

from app.config import Config


DEFAULT_MAX_TRANSCODES = 2
DEFAULT_MAX_TRANSCODES_PER_USER = 1
MAX_ALLOWED_TRANSCODES = 2048


def _environment_value(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


class PlaybackSettings:
    def __init__(self, db=None):
        self.db = db or Config().database

    @staticmethod
    def normalize(max_transcodes, max_transcodes_per_user) -> dict[str, int]:
        values = {
            "maxTranscodes": max_transcodes,
            "maxTranscodesPerUser": max_transcodes_per_user,
        }
        normalized = {}
        for name, value in values.items():
            try:
                integer = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a whole number.") from error
            if integer < 1 or integer > MAX_ALLOWED_TRANSCODES:
                raise ValueError(
                    f"{name} must be between 1 and {MAX_ALLOWED_TRANSCODES}."
                )
            normalized[name] = integer
        if normalized["maxTranscodesPerUser"] > normalized["maxTranscodes"]:
            raise ValueError("maxTranscodesPerUser cannot exceed maxTranscodes.")
        return normalized

    def get(self) -> dict[str, int]:
        defaults = {
            "maxTranscodes": _environment_value(
                "MAX_TRANSCODES", DEFAULT_MAX_TRANSCODES
            ),
            "maxTranscodesPerUser": _environment_value(
                "MAX_TRANSCODES_PER_USER", DEFAULT_MAX_TRANSCODES_PER_USER
            ),
        }
        if defaults["maxTranscodesPerUser"] > defaults["maxTranscodes"]:
            defaults["maxTranscodesPerUser"] = defaults["maxTranscodes"]
        rows = self.db.execute(
            "SELECT max_transcodes,max_transcodes_per_user FROM playback_settings WHERE id=1"
        )
        if not isinstance(rows, list) or not rows:
            return defaults
        try:
            return self.normalize(rows[0][0], rows[0][1])
        except (IndexError, TypeError, ValueError):
            return defaults

    def set(self, max_transcodes, max_transcodes_per_user) -> dict[str, int]:
        values = self.normalize(max_transcodes, max_transcodes_per_user)
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO playback_settings(id,max_transcodes,max_transcodes_per_user,updated_at) VALUES(1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET max_transcodes=excluded.max_transcodes, "
            "max_transcodes_per_user=excluded.max_transcodes_per_user,updated_at=excluded.updated_at",
            (
                values["maxTranscodes"],
                values["maxTranscodesPerUser"],
                now,
            ),
        )
        return values
