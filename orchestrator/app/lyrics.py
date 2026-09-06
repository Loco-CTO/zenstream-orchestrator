from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path


_TIMESTAMP_RE = re.compile(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]")
_METADATA_RE = re.compile(r"^\[[A-Za-z][A-Za-z0-9_-]*:.*\]$")


def _timestamp(minutes: str, seconds: str, fraction: str | None) -> float:
    value = float(minutes) * 60.0 + float(seconds)
    if fraction:
        value += int(fraction.ljust(3, "0")) / 1000.0
    return max(0.0, value)


def _clean_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value or "").strip()


def _plain_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not _METADATA_RE.match(line.strip())
    ]


def _timed_result(
    values: Iterable[tuple[float, str]], duration_seconds: float | None = None
) -> dict | None:
    timed = [
        (float(start), text.strip(), index)
        for index, (start, text) in enumerate(values)
        if text and float(start) >= 0
    ]
    if not timed:
        return None
    timed.sort(key=lambda value: (value[0], value[2]))
    lines = []
    for index, (start, text, _order) in enumerate(timed):
        next_start = timed[index + 1][0] if index + 1 < len(timed) else None
        end = next_start
        if end is None or end <= start:
            end = (
                duration_seconds
                if duration_seconds is not None and duration_seconds > start
                else start + 8.0
            )
        lines.append(
            {
                "text": text,
                "startSeconds": round(start, 3),
                "endSeconds": round(max(end, start + 0.5), 3),
            }
        )
    return {"timed": True, "lines": lines}


def parse_lyrics_text(text: str, duration_seconds: float | None = None) -> dict | None:
    """Normalize LRC-like text into the client lyric payload shape."""
    timed: list[tuple[float, str]] = []
    for raw_line in text.replace("\ufeff", "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        stamps = _TIMESTAMP_RE.findall(line)
        lyric = re.sub(r"\[[^\]]+\]", "", line).strip()
        if stamps and lyric:
            for minutes, seconds, fraction in stamps:
                timed.append((_timestamp(minutes, seconds, fraction), lyric))

    normalized = _timed_result(timed, duration_seconds)
    if normalized:
        return normalized

    lines = _plain_lines(text.replace("\ufeff", ""))
    if not lines:
        return None
    return {"timed": False, "lines": [{"text": line} for line in lines]}


def _tag_values(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _embedded_candidate(
    parsed: dict | None, language: str | None, order: int
) -> dict | None:
    if not parsed:
        return None
    return {
        "source": "embedded",
        "timed": bool(parsed["timed"]),
        "language": language or None,
        "lines": parsed["lines"],
        "_order": order,
    }


def embedded_lyrics(path: Path, duration_seconds: float | None = None) -> list[dict]:
    """Read common embedded unsynchronized and synchronized lyric tags."""
    try:
        from mutagen import File

        audio = File(path, easy=False)
        tags = getattr(audio, "tags", None) if audio is not None else None
        if not tags:
            return []
    except Exception:
        return []

    candidates: list[dict] = []
    seen: set[tuple[bool, str, str | None]] = set()
    order = 0
    for frame in getattr(tags, "values", lambda: [])():
        frame_id = str(getattr(frame, "FrameID", "")).upper()
        if frame_id == "SYLT":
            pairs: list[tuple[float, str]] = []
            for value in getattr(frame, "text", []) or []:
                if not isinstance(value, (list, tuple)) or len(value) < 2:
                    continue
                lyric = _clean_text(value[0])
                try:
                    timestamp = float(value[1]) / 1000.0
                except (TypeError, ValueError):
                    continue
                if lyric:
                    pairs.append((timestamp, lyric))
            parsed = _timed_result(pairs, duration_seconds)
            candidate = _embedded_candidate(
                parsed, _clean_text(getattr(frame, "lang", None)), order
            )
            order += 1
            if candidate:
                key = (True, repr(candidate["lines"]), candidate["language"])
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)
        elif frame_id == "USLT":
            text = _clean_text(getattr(frame, "text", ""))
            parsed = parse_lyrics_text(text, duration_seconds)
            candidate = _embedded_candidate(
                parsed, _clean_text(getattr(frame, "lang", None)), order
            )
            order += 1
            if candidate:
                key = (candidate["timed"], repr(candidate["lines"]), candidate["language"])
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)

    for raw_key, raw_value in getattr(tags, "items", lambda: [])():
        key = str(raw_key).casefold()
        if "lyric" not in key and not key.endswith("lyr"):
            continue
        for value in _tag_values(raw_value):
            text = _clean_text(value)
            if not text:
                continue
            parsed = parse_lyrics_text(text, duration_seconds)
            candidate = _embedded_candidate(parsed, None, order)
            order += 1
            if not candidate:
                continue
            key_value = (candidate["timed"], repr(candidate["lines"]), None)
            if key_value in seen:
                continue
            seen.add(key_value)
            candidates.append(candidate)
    return candidates


def choose_lyrics(candidates: Iterable[dict]) -> dict | None:
    values = [candidate for candidate in candidates if candidate.get("lines")]
    if not values:
        return None
    selected = min(
        values,
        key=lambda candidate: (
            0 if candidate.get("timed") else 1,
            0 if candidate.get("source") == "embedded" else 1,
            int(candidate.get("_order", 0)),
        ),
    )
    return {
        "source": selected.get("source", "sidecar"),
        "timed": bool(selected.get("timed")),
        "language": selected.get("language") or None,
        "lines": selected.get("lines") or [],
    }


def lyrics_to_vtt(text: str, duration_seconds: float | None = None) -> str:
    parsed = parse_lyrics_text(text, duration_seconds)
    if not parsed:
        return "WEBVTT\n\n"
    if not parsed["timed"]:
        end = duration_seconds if duration_seconds and duration_seconds > 0 else 359999.0
        lines = "\n".join(line["text"] for line in parsed["lines"])
        return f"WEBVTT\n\n1\n00:00:00.000 --> {_vtt_time(end)}\n{lines}\n"
    cues = []
    for index, line in enumerate(parsed["lines"], start=1):
        cues.append(
            f"{index}\n{_vtt_time(line['startSeconds'])} --> "
            f"{_vtt_time(line['endSeconds'])}\n{line['text']}\n"
        )
    return "WEBVTT\n\n" + "\n".join(cues)


def _vtt_time(seconds: float) -> str:
    hours, remainder = divmod(max(0.0, float(seconds)), 3600)
    minutes, remainder = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{remainder:06.3f}"
