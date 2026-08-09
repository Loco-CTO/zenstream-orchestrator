import os
from datetime import datetime, timezone

from app.config import Config


DEFAULT_MAX_TRANSCODES = 0
DEFAULT_MAX_TRANSCODES_PER_USER = 0
MAX_ALLOWED_TRANSCODES = 64
DEFAULT_TRICKPLAY_FRAME_WIDTH = 320
DEFAULT_TRICKPLAY_FRAME_HEIGHT = 180
DEFAULT_TRICKPLAY_INTERVAL_SECONDS = 10
DEFAULT_TRICKPLAY_WORKERS = 1
MIN_TRICKPLAY_FRAME_WIDTH = 160
MAX_TRICKPLAY_FRAME_WIDTH = 640
MIN_TRICKPLAY_INTERVAL_SECONDS = 1
MAX_TRICKPLAY_INTERVAL_SECONDS = 60


def _environment_value(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= MAX_ALLOWED_TRANSCODES else default


class PlaybackSettings:
    def __init__(self, db=None):
        self.db = db or Config().database

    @staticmethod
    def normalize(
        max_transcodes,
        max_transcodes_per_user,
        trickplay_frame_width=DEFAULT_TRICKPLAY_FRAME_WIDTH,
        trickplay_frame_height=DEFAULT_TRICKPLAY_FRAME_HEIGHT,
        trickplay_interval_seconds=DEFAULT_TRICKPLAY_INTERVAL_SECONDS,
        trickplay_workers=DEFAULT_TRICKPLAY_WORKERS,
    ) -> dict[str, int]:
        values = {
            "maxTranscodes": max_transcodes,
            "maxTranscodesPerUser": max_transcodes_per_user,
            "trickplayFrameWidth": trickplay_frame_width,
            "trickplayFrameHeight": trickplay_frame_height,
            "trickplayIntervalSeconds": trickplay_interval_seconds,
            "trickplayWorkers": trickplay_workers,
        }
        normalized = {}
        for name, value in values.items():
            try:
                integer = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a whole number.") from error
            if name in {"maxTranscodes", "maxTranscodesPerUser"} and (
                integer < 0 or integer > MAX_ALLOWED_TRANSCODES
            ):
                raise ValueError(
                    f"{name} must be between 0 and {MAX_ALLOWED_TRANSCODES}."
                )
            if name == "trickplayWorkers" and not 1 <= integer <= MAX_ALLOWED_TRANSCODES:
                raise ValueError(
                    f"{name} must be between 1 and {MAX_ALLOWED_TRANSCODES}."
                )
            normalized[name] = integer
        if (
            normalized["maxTranscodes"] > 0
            and normalized["maxTranscodesPerUser"] > 0
            and normalized["maxTranscodesPerUser"] > normalized["maxTranscodes"]
        ):
            raise ValueError("maxTranscodesPerUser cannot exceed maxTranscodes.")
        width = normalized["trickplayFrameWidth"]
        if width < MIN_TRICKPLAY_FRAME_WIDTH or width > MAX_TRICKPLAY_FRAME_WIDTH:
            raise ValueError("trickplay frame width must be between 160 and 640.")
        if width % 16:
            raise ValueError("trickplay frame width must be divisible by 16.")
        if normalized["trickplayFrameHeight"] != width * 9 // 16:
            raise ValueError("trickplay frame height must equal the derived 16:9 height.")
        interval = normalized["trickplayIntervalSeconds"]
        if interval < MIN_TRICKPLAY_INTERVAL_SECONDS or interval > MAX_TRICKPLAY_INTERVAL_SECONDS:
            raise ValueError("trickplay interval must be between 1 and 60 seconds.")
        return normalized

    def get(self) -> dict[str, int]:
        defaults = {
            "maxTranscodes": _environment_value(
                "MAX_TRANSCODES", DEFAULT_MAX_TRANSCODES
            ),
            "maxTranscodesPerUser": _environment_value(
                "MAX_TRANSCODES_PER_USER", DEFAULT_MAX_TRANSCODES_PER_USER
            ),
            "trickplayFrameWidth": DEFAULT_TRICKPLAY_FRAME_WIDTH,
            "trickplayFrameHeight": DEFAULT_TRICKPLAY_FRAME_HEIGHT,
            "trickplayIntervalSeconds": DEFAULT_TRICKPLAY_INTERVAL_SECONDS,
            "trickplayWorkers": DEFAULT_TRICKPLAY_WORKERS,
        }
        if (
            defaults["maxTranscodes"] > 0
            and defaults["maxTranscodesPerUser"] > 0
            and defaults["maxTranscodesPerUser"] > defaults["maxTranscodes"]
        ):
            defaults["maxTranscodesPerUser"] = defaults["maxTranscodes"]
        rows = self.db.execute(
            "SELECT max_transcodes,max_transcodes_per_user,trickplay_frame_width,trickplay_frame_height,trickplay_interval_seconds,trickplay_workers FROM playback_settings WHERE id=1"
        )
        if not isinstance(rows, list) or not rows:
            return defaults
        try:
            return self.normalize(rows[0][0], rows[0][1], rows[0][2], rows[0][3], rows[0][4], rows[0][5])
        except (IndexError, TypeError, ValueError):
            return defaults

    def set(
        self,
        max_transcodes,
        max_transcodes_per_user,
        trickplay_frame_width=DEFAULT_TRICKPLAY_FRAME_WIDTH,
        trickplay_frame_height=DEFAULT_TRICKPLAY_FRAME_HEIGHT,
        trickplay_interval_seconds=DEFAULT_TRICKPLAY_INTERVAL_SECONDS,
        trickplay_workers=DEFAULT_TRICKPLAY_WORKERS,
    ) -> dict[str, int]:
        values = self.normalize(
            max_transcodes,
            max_transcodes_per_user,
            trickplay_frame_width,
            trickplay_frame_height,
            trickplay_interval_seconds,
            trickplay_workers,
        )
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO playback_settings(id,max_transcodes,max_transcodes_per_user,trickplay_frame_width,trickplay_frame_height,trickplay_interval_seconds,trickplay_workers,updated_at) VALUES(1,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET max_transcodes=excluded.max_transcodes, "
            "max_transcodes_per_user=excluded.max_transcodes_per_user,"
            "trickplay_frame_width=excluded.trickplay_frame_width,"
            "trickplay_frame_height=excluded.trickplay_frame_height,"
            "trickplay_interval_seconds=excluded.trickplay_interval_seconds,"
            "trickplay_workers=excluded.trickplay_workers,updated_at=excluded.updated_at",
            (
                values["maxTranscodes"],
                values["maxTranscodesPerUser"],
                values["trickplayFrameWidth"],
                values["trickplayFrameHeight"],
                values["trickplayIntervalSeconds"],
                values["trickplayWorkers"],
                now,
            ),
        )
        return values
