from __future__ import annotations

import json
import math
from fractions import Fraction

MIN_EFFECTIVE_VIDEO_DURATION_SECONDS = 0.1


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _tag_duration_seconds(value: object) -> float | None:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (float(part) for part in parts)
    except (TypeError, ValueError):
        return None
    duration = hours * 3600.0 + minutes * 60.0 + seconds
    return duration if math.isfinite(duration) else None


def probe_streams(payload: object) -> list[dict] | None:
    if isinstance(payload, dict):
        value = payload
    else:
        try:
            value = json.loads(payload or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(value, dict):
        return None
    streams = value.get("streams")
    if not isinstance(streams, list):
        return None
    parsed = [stream for stream in streams if isinstance(stream, dict)]
    return parsed or None


def stream_duration_seconds(
    stream: dict, fallback: float | None = None
) -> float | None:
    duration = _finite_float(stream.get("duration"))
    if duration is not None:
        return max(0.0, duration)
    duration_ts = _finite_float(stream.get("duration_ts"))
    time_base = stream.get("time_base")
    if duration_ts is not None and time_base:
        try:
            duration = duration_ts * float(Fraction(str(time_base)))
        except (TypeError, ValueError, ZeroDivisionError):
            duration = None
        if duration is not None and math.isfinite(duration):
            return max(0.0, duration)
    tags = stream.get("tags")
    if isinstance(tags, dict):
        duration = _tag_duration_seconds(tags.get("DURATION") or tags.get("duration"))
        if duration is not None:
            return max(0.0, duration)
    return fallback


def stream_start_seconds(stream: dict) -> float:
    return max(0.0, _finite_float(stream.get("start_time")) or 0.0)


def stream_end_seconds(stream: dict, fallback: float | None = None) -> float | None:
    duration = stream_duration_seconds(stream)
    if duration is None:
        return fallback
    return stream_start_seconds(stream) + duration


def _is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_attached_picture(stream: dict) -> bool:
    disposition = stream.get("disposition")
    if isinstance(disposition, dict) and _is_enabled(disposition.get("attached_pic")):
        return True
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return False
    mimetype = str(tags.get("MIMETYPE") or tags.get("mimetype") or "").lower()
    filename = str(tags.get("FILENAME") or tags.get("filename") or "").lower()
    return mimetype.startswith("image/") or filename.endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
    )


def is_usable_video_stream(
    stream: dict, container_duration_seconds: float | None = None
) -> bool:
    if str(stream.get("codec_type") or "").lower() != "video":
        return False
    if is_attached_picture(stream):
        return False
    width = _finite_float(stream.get("width"))
    height = _finite_float(stream.get("height"))
    if (width is not None and width <= 0) or (height is not None and height <= 0):
        return False
    duration = stream_duration_seconds(stream, container_duration_seconds)
    return duration is None or duration > MIN_EFFECTIVE_VIDEO_DURATION_SECONDS


def select_usable_video_stream(
    streams: list[dict], container_duration_seconds: float | None = None
) -> dict | None:
    return next(
        (
            stream
            for stream in streams
            if is_usable_video_stream(stream, container_duration_seconds)
        ),
        None,
    )


def first_audio_stream(streams: list[dict]) -> dict | None:
    return next(
        (
            stream
            for stream in streams
            if str(stream.get("codec_type") or "").lower() == "audio"
        ),
        None,
    )


def stream_index(stream: dict | None) -> int | None:
    if stream is None:
        return None
    value = _finite_float(stream.get("index"))
    if value is None or not value.is_integer() or value < 0:
        return None
    return int(value)
