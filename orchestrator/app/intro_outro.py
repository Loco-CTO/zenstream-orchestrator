from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.config import Config
from app.logging_config import get_logger
from app.playback import PLAYABLE_ROLE, ffmpeg_path
from fastapi import HTTPException

logger = get_logger("intro_outro")
SAMPLE_SECONDS = 4096.0 / 11025.0 / 3.0
MIN_MATCH_DENSITY = 0.55
DEFAULTS = {
    "scanOnAdded": True,
    "analysisPercent": 25,
    "analysisLengthLimitMinutes": 10,
    "scanIntroduction": True,
    "scanCredits": True,
    "minimumIntroDuration": 15,
    "maximumIntroDuration": 120,
    "minimumCreditsDuration": 15,
    "maximumCreditsAnalysisSeconds": 450,
    "maximumFingerprintPointDifferences": 6,
    "maximumTimeSkipSeconds": 3.5,
    "invertedIndexShift": 2,
    "introOutroWorkers": 1,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_settings(values: dict | None = None) -> dict:
    values = {**DEFAULTS, **(values or {})}

    def integer(key: str, minimum: int, maximum: int):
        try:
            return max(minimum, min(maximum, int(values[key])))
        except (TypeError, ValueError):
            return DEFAULTS[key]

    def decimal(key: str, minimum: float, maximum: float):
        try:
            return max(minimum, min(maximum, float(values[key])))
        except (TypeError, ValueError):
            return DEFAULTS[key]

    result = {
        "scanOnAdded": bool(values["scanOnAdded"]),
        "analysisPercent": integer("analysisPercent", 1, 50),
        "analysisLengthLimitMinutes": integer("analysisLengthLimitMinutes", 1, 60),
        "scanIntroduction": bool(values["scanIntroduction"]),
        "scanCredits": bool(values["scanCredits"]),
        "minimumIntroDuration": integer("minimumIntroDuration", 1, 600),
        "maximumIntroDuration": integer("maximumIntroDuration", 1, 600),
        "minimumCreditsDuration": integer("minimumCreditsDuration", 1, 1800),
        "maximumCreditsAnalysisSeconds": integer(
            "maximumCreditsAnalysisSeconds", 15, 1800
        ),
        "maximumFingerprintPointDifferences": integer(
            "maximumFingerprintPointDifferences", 0, 32
        ),
        "maximumTimeSkipSeconds": decimal("maximumTimeSkipSeconds", 0.1, 10),
        "invertedIndexShift": integer("invertedIndexShift", 0, 8),
        "introOutroWorkers": integer("introOutroWorkers", 1, 64),
    }
    result["maximumIntroDuration"] = max(
        result["minimumIntroDuration"], result["maximumIntroDuration"]
    )
    result["maximumCreditsAnalysisSeconds"] = max(
        result["minimumCreditsDuration"], result["maximumCreditsAnalysisSeconds"]
    )
    return result


def analysis_key(settings: dict) -> str:
    data = {
        key: settings[key]
        for key in (
            "analysisPercent",
            "analysisLengthLimitMinutes",
            "scanIntroduction",
            "scanCredits",
            "maximumCreditsAnalysisSeconds",
        )
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class IntroOutroStore:
    def __init__(self, db=None):
        self.db = db or Config().database

    @staticmethod
    def source_key(quick_fingerprint, size, modified_ns) -> str:
        return str(quick_fingerprint or f"{int(size or 0)}:{int(modified_ns or 0)}")

    def available(self) -> bool:
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            "intro_outro_settings",
            "intro_outro_assets",
            "intro_outro_segments",
        }.issubset(tables)

    def settings(self) -> dict:
        if not self.available():
            return dict(DEFAULTS)
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(intro_outro_settings)")
        }
        names = [
            ("scanOnAdded", "scan_on_added"),
            ("analysisPercent", "analysis_percent"),
            ("analysisLengthLimitMinutes", "analysis_length_limit_minutes"),
            ("scanIntroduction", "scan_introduction"),
            ("scanCredits", "scan_credits"),
            ("minimumIntroDuration", "minimum_intro_duration"),
            ("maximumIntroDuration", "maximum_intro_duration"),
            ("minimumCreditsDuration", "minimum_credits_duration"),
            ("maximumCreditsAnalysisSeconds", "maximum_credits_analysis_seconds"),
            (
                "maximumFingerprintPointDifferences",
                "maximum_fingerprint_point_differences",
            ),
            ("maximumTimeSkipSeconds", "maximum_time_skip_seconds"),
            ("invertedIndexShift", "inverted_index_shift"),
            ("introOutroWorkers", "intro_outro_workers"),
        ]
        selected = [(key, column) for key, column in names if column in columns]
        rows = self.db.execute(
            "SELECT "
            + ",".join(column for _, column in selected)
            + " FROM intro_outro_settings WHERE id=1"
        )
        values = dict(DEFAULTS)
        if rows:
            values.update({key: value for (key, _), value in zip(selected, rows[0])})
        return normalize_settings(values)

    def update_settings(self, values: dict) -> dict:
        normalized = normalize_settings({**self.settings(), **values})
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(intro_outro_settings)")
        }
        mappings = [
            ("scanOnAdded", "scan_on_added"),
            ("analysisPercent", "analysis_percent"),
            ("analysisLengthLimitMinutes", "analysis_length_limit_minutes"),
            ("scanIntroduction", "scan_introduction"),
            ("scanCredits", "scan_credits"),
            ("minimumIntroDuration", "minimum_intro_duration"),
            ("maximumIntroDuration", "maximum_intro_duration"),
            ("minimumCreditsDuration", "minimum_credits_duration"),
            ("maximumCreditsAnalysisSeconds", "maximum_credits_analysis_seconds"),
            (
                "maximumFingerprintPointDifferences",
                "maximum_fingerprint_point_differences",
            ),
            ("maximumTimeSkipSeconds", "maximum_time_skip_seconds"),
            ("invertedIndexShift", "inverted_index_shift"),
            ("introOutroWorkers", "intro_outro_workers"),
        ]
        selected = [(key, column) for key, column in mappings if column in columns]
        self.db.execute(
            "INSERT INTO intro_outro_settings(id,"
            + ",".join(column for _, column in selected)
            + ",updated_at) VALUES(1,"
            + ",".join("?" for _ in selected)
            + ",?) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ",".join(f"{column}=excluded.{column}" for _, column in selected)
            + ",updated_at=excluded.updated_at",
            [
                int(normalized[key])
                if isinstance(normalized[key], bool)
                else normalized[key]
                for key, _ in selected
            ]
            + [now()],
        )
        return normalized

    def clear_segments(self) -> int:
        rows = self.db.execute("SELECT COUNT(*) FROM intro_outro_segments")
        count = int(rows[0][0] or 0) if rows else 0
        self.db.execute("DELETE FROM intro_outro_segments")
        return count

    def queue_pending(
        self, library_id: str | None = None, settings: dict | None = None
    ) -> int:
        if not self.available():
            return 0
        settings = settings or self.settings()
        fingerprint_settings_key = analysis_key(settings)
        params: list[object] = [PLAYABLE_ROLE]
        scope = ""
        if library_id:
            scope = " AND episode.library_id=?"
            params.append(library_id)
        rows = self.db.execute(
            "SELECT f.id,episode.id,season.id,f.quick_fingerprint,f.size,f.modified_ns "
            "FROM media_files f JOIN media_sources source ON source.media_file_id=f.id "
            "JOIN library_entities episode ON episode.id=f.entity_id "
            "JOIN library_entities season ON season.id=episode.parent_id AND season.entity_type='season' "
            "JOIN library_entities series ON series.id=season.parent_id AND series.entity_type='series' "
            "JOIN libraries library ON library.id=episode.library_id "
            "WHERE f.role=? AND episode.entity_type='episode' AND library.type='tv_series' "
            "AND COALESCE(season.season_number,0)>0 AND COALESCE(source.audio_codec,'')<>''"
            + scope,
            params,
        )
        queued = 0
        for (
            media_file_id,
            entity_id,
            season_id,
            quick_fingerprint,
            size,
            modified_ns,
        ) in rows:
            source_fingerprint = self.source_key(quick_fingerprint, size, modified_ns)
            existing = self.db.execute(
                "SELECT source_fingerprint,analysis_key FROM intro_outro_assets WHERE media_file_id=?",
                (media_file_id,),
            )
            if (
                existing
                and existing[0][0] == source_fingerprint
                and existing[0][1] == fingerprint_settings_key
            ):
                continue
            timestamp = now()
            self.db.execute(
                "INSERT INTO intro_outro_assets(media_file_id,entity_id,season_id,source_fingerprint,analysis_key,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(media_file_id) DO UPDATE SET "
                "entity_id=excluded.entity_id,season_id=excluded.season_id,source_fingerprint=excluded.source_fingerprint,analysis_key=excluded.analysis_key,"
                "intro_fingerprint=NULL,outro_fingerprint=NULL,state='queued',error=NULL,updated_at=excluded.updated_at",
                (
                    media_file_id,
                    entity_id,
                    season_id,
                    source_fingerprint,
                    fingerprint_settings_key,
                    "queued",
                    timestamp,
                    timestamp,
                ),
            )
            self.db.execute(
                "DELETE FROM intro_outro_segments WHERE media_file_id=?",
                (media_file_id,),
            )
            queued += 1
        return queued

    def claim_next(self) -> dict | None:
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT a.media_file_id,a.entity_id,a.season_id,a.source_fingerprint,source.duration_seconds,library.directory,f.relative_path "
                "FROM intro_outro_assets a JOIN media_files f ON f.id=a.media_file_id "
                "JOIN media_sources source ON source.media_file_id=f.id "
                "JOIN library_entities entity ON entity.id=a.entity_id JOIN libraries library ON library.id=entity.library_id "
                "WHERE a.state='queued' ORDER BY a.updated_at,a.media_file_id LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE intro_outro_assets SET state='generating',error=NULL,updated_at=? WHERE media_file_id=? AND state='queued'",
                (now(), row[0]),
            )
            if cursor.rowcount != 1:
                return None
        return {
            "mediaFileId": row[0],
            "entityId": row[1],
            "seasonId": row[2],
            "sourceFingerprint": row[3],
            "durationSeconds": float(row[4] or 0),
            "path": Path(row[5]) / row[6],
        }

    def recover_generating(self) -> int:
        """Make assets claimed by an interrupted worker eligible again."""
        if not self.available():
            return 0
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE intro_outro_assets SET state='queued',error=NULL,updated_at=? "
                "WHERE state='generating'",
                (now(),),
            )
            return cursor.rowcount

    def requeue(self, asset: dict) -> bool:
        """Return one interrupted asset to the queue without clobbering newer work."""
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE intro_outro_assets SET state='queued',error=NULL,updated_at=? "
                "WHERE media_file_id=? AND state='generating' AND source_fingerprint=?",
                (now(), asset["mediaFileId"], asset["sourceFingerprint"]),
            )
            return cursor.rowcount == 1

    def mark_fingerprinted(
        self, asset: dict, intro: bytes | None, outro: bytes | None
    ) -> None:
        self.db.execute(
            "UPDATE intro_outro_assets SET intro_fingerprint=?,outro_fingerprint=?,state='scanned',error=NULL,updated_at=? "
            "WHERE media_file_id=? AND source_fingerprint=? AND state='generating'",
            (intro, outro, now(), asset["mediaFileId"], asset["sourceFingerprint"]),
        )

    def mark_failed(self, asset: dict, error: str) -> None:
        self.db.execute(
            "UPDATE intro_outro_assets SET state='failed',error=?,updated_at=? WHERE media_file_id=? AND source_fingerprint=? AND state='generating'",
            (error[:1000], now(), asset["mediaFileId"], asset["sourceFingerprint"]),
        )

    def recompute_all(self, settings: dict) -> int:
        rows = self.db.execute(
            "SELECT DISTINCT season_id FROM intro_outro_assets WHERE state='scanned'"
        )
        return sum(self.recompute_season(row[0], settings) for row in rows)

    def recompute_season(self, season_id: str, settings: dict) -> int:
        rows = self.db.execute(
            "SELECT media_file_id,intro_fingerprint,outro_fingerprint FROM intro_outro_assets "
            "WHERE season_id=? AND state='scanned'",
            (season_id,),
        )
        if len(rows) < 2:
            return 0
        ids = [row[0] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self.db.execute(
            f"DELETE FROM intro_outro_segments WHERE media_file_id IN ({placeholders})",
            ids,
        )
        selected: dict[tuple[str, str], tuple[float, float]] = {}
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                kinds = []
                if settings["scanIntroduction"]:
                    kinds.append(
                        (
                            "intro",
                            1,
                            settings["minimumIntroDuration"],
                            settings["maximumIntroDuration"],
                            0.0,
                        )
                    )
                if settings["scanCredits"]:
                    kinds.append(
                        (
                            "outro",
                            2,
                            settings["minimumCreditsDuration"],
                            settings["maximumCreditsAnalysisSeconds"],
                            None,
                        )
                    )
                for kind, column, minimum, maximum, offset in kinds:
                    left_points = decode_fingerprint(left[column])
                    right_points = decode_fingerprint(right[column])
                    result = shared_region(
                        left_points, right_points, settings, minimum, maximum
                    )
                    if not result:
                        continue
                    left_start, left_end, right_start, right_end = result
                    left_duration = self._duration(left[0])
                    right_duration = self._duration(right[0])
                    left_offset = (
                        offset
                        if offset is not None
                        else max(
                            0.0,
                            left_duration - settings["maximumCreditsAnalysisSeconds"],
                        )
                    )
                    right_offset = (
                        offset
                        if offset is not None
                        else max(
                            0.0,
                            right_duration - settings["maximumCreditsAnalysisSeconds"],
                        )
                    )
                    self._choose(
                        selected,
                        left[0],
                        kind,
                        left_start + left_offset,
                        min(left_duration, left_end + left_offset),
                    )
                    self._choose(
                        selected,
                        right[0],
                        kind,
                        right_start + right_offset,
                        min(right_duration, right_end + right_offset),
                    )
        if selected:
            with self.db.transaction() as cursor:
                cursor.executemany(
                    "INSERT INTO intro_outro_segments(media_file_id,segment_type,start_seconds,end_seconds) VALUES(?,?,?,?)",
                    [
                        (media_file_id, kind, start, end)
                        for (media_file_id, kind), (start, end) in selected.items()
                    ],
                )
        return len(selected)

    def _duration(self, media_file_id: str) -> float:
        rows = self.db.execute(
            "SELECT duration_seconds FROM media_sources WHERE media_file_id=?",
            (media_file_id,),
        )
        return float(rows[0][0] or 0) if rows else 0.0

    @staticmethod
    def _choose(
        selected: dict, media_file_id: str, kind: str, start: float, end: float
    ) -> None:
        previous = selected.get((media_file_id, kind))
        if not previous or end - start > previous[1] - previous[0]:
            selected[(media_file_id, kind)] = (start, end)

    def segments(self, entity_id: str, source_id: str | None = None) -> dict:
        source_filter = " AND source.id=?" if source_id else ""
        rows = self.db.execute(
            "SELECT source.id,source.media_file_id,asset.state FROM media_sources source "
            "JOIN media_files f ON f.id=source.media_file_id "
            "LEFT JOIN intro_outro_assets asset ON asset.media_file_id=f.id "
            "WHERE source.entity_id=? AND f.role=?"
            + source_filter
            + " ORDER BY source.id LIMIT 1",
            [entity_id, PLAYABLE_ROLE, source_id]
            if source_id
            else [entity_id, PLAYABLE_ROLE],
        )
        if not rows:
            raise HTTPException(404, "Playback source not found.")
        selected_source, media_file_id, state = rows[0]
        segments = self.db.execute(
            "SELECT segment_type,start_seconds,end_seconds FROM intro_outro_segments WHERE media_file_id=? ORDER BY segment_type",
            (media_file_id,),
        )
        return {
            "sourceId": selected_source,
            "state": state or "unavailable",
            "segments": [
                {"type": kind, "startSeconds": start, "endSeconds": end}
                for kind, start, end in segments
            ],
        }

    def inspection(self, entity_id: str) -> dict:
        if not self.available():
            return {"state": "unavailable", "segments": [], "fingerprints": []}
        rows = self.db.execute(
            "SELECT source.id,source.media_file_id,source.duration_seconds,asset.state,asset.error,asset.updated_at,"
            "asset.intro_fingerprint,asset.outro_fingerprint "
            "FROM media_sources source JOIN media_files f ON f.id=source.media_file_id "
            "LEFT JOIN intro_outro_assets asset ON asset.media_file_id=f.id "
            "WHERE source.entity_id=? AND f.role=? ORDER BY source.id LIMIT 1",
            (entity_id, PLAYABLE_ROLE),
        )
        if not rows:
            raise HTTPException(404, "Playback source not found.")
        source_id, media_file_id, duration, state, error, updated_at, intro, outro = (
            rows[0]
        )
        duration = float(duration or 0)
        segments = self.db.execute(
            "SELECT segment_type,start_seconds,end_seconds FROM intro_outro_segments WHERE media_file_id=? ORDER BY segment_type",
            (media_file_id,),
        )
        settings = self.settings()
        intro_duration = min(
            duration * settings["analysisPercent"] / 100.0,
            settings["analysisLengthLimitMinutes"] * 60.0,
        )
        outro_duration = min(float(settings["maximumCreditsAnalysisSeconds"]), duration)
        outro_start = max(0.0, duration - outro_duration)
        return {
            "sourceId": source_id,
            "durationSeconds": duration,
            "state": state or "unavailable",
            "error": error,
            "updatedAt": updated_at,
            "segments": [
                {"type": kind, "startSeconds": start, "endSeconds": end}
                for kind, start, end in segments
            ],
            "fingerprints": [
                {
                    "type": "intro",
                    "startSeconds": 0.0,
                    "endSeconds": intro_duration,
                    **fingerprint_preview(intro),
                },
                {
                    "type": "outro",
                    "startSeconds": outro_start,
                    "endSeconds": duration,
                    **fingerprint_preview(outro),
                },
            ],
        }

    def preview_clip(self, entity_id: str, kind: str) -> dict:
        if kind not in {"intro", "outro"}:
            raise ValueError("Unsupported segment type.")
        if not self.available():
            raise HTTPException(404, "Detected segment not found.")
        rows = self.db.execute(
            "SELECT library.directory,f.relative_path,segment.start_seconds,segment.end_seconds "
            "FROM media_sources source JOIN media_files f ON f.id=source.media_file_id "
            "JOIN library_entities entity ON entity.id=source.entity_id "
            "JOIN libraries library ON library.id=entity.library_id "
            "JOIN intro_outro_segments segment ON segment.media_file_id=f.id AND segment.segment_type=? "
            "WHERE source.entity_id=? AND f.role=? ORDER BY source.id LIMIT 1",
            (kind, entity_id, PLAYABLE_ROLE),
        )
        if not rows:
            raise HTTPException(404, "Detected segment not found.")
        directory, relative_path, start, end = rows[0]
        path = Path(directory) / relative_path
        if not path.is_file():
            raise HTTPException(404, "Media source is unavailable.")
        return {
            "path": path,
            "startSeconds": float(start),
            "durationSeconds": max(0.0, float(end) - float(start)),
        }


def decode_fingerprint(value: bytes | None) -> tuple[int, ...]:
    if not value or len(value) % 4:
        return ()
    return struct.unpack(f"<{len(value) // 4}I", value)


def fingerprint_preview(value: bytes | None, maximum_samples: int = 160) -> dict:
    points = decode_fingerprint(value)
    if not points:
        return {"pointCount": 0, "sampleSeconds": SAMPLE_SECONDS, "values": []}
    sample_count = min(max(1, maximum_samples), len(points))
    values = []
    for index in range(sample_count):
        start = index * len(points) // sample_count
        end = max(start + 1, (index + 1) * len(points) // sample_count)
        values.append(
            round(
                sum(point.bit_count() for point in points[start:end]) / (end - start), 2
            )
        )
    return {
        "pointCount": len(points),
        "sampleSeconds": SAMPLE_SECONDS,
        "values": values,
    }


def audio_preview_command(path: Path, start: float, duration: float) -> list[str]:
    executable = ffmpeg_path()
    if not executable:
        raise RuntimeError("FFmpeg is not available.")
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start),
        "-i",
        str(path),
        "-t",
        str(duration),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "2",
        "-c:a",
        "mp3",
        "-b:a",
        "128k",
        "-f",
        "mp3",
        "-",
    ]


def render_audio_preview(path: Path, start: float, duration: float) -> bytes:
    if not path.is_file():
        raise RuntimeError("Media source is unavailable.")
    try:
        result = subprocess.run(
            audio_preview_command(path, start, duration),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=max(30, min(180, duration * 2 + 15)),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Audio preview timed out.") from error
    if result.returncode or not result.stdout:
        raise RuntimeError(
            (
                result.stderr.decode("utf-8", "replace")
                or "FFmpeg did not return audio preview data."
            ).strip()[-1000:]
        )
    return result.stdout


def _bits(value: int) -> int:
    return value.bit_count()


def shared_region(
    left: tuple[int, ...],
    right: tuple[int, ...],
    settings: dict,
    minimum: float,
    maximum: float,
):
    if not left or not right:
        return None
    right_index: dict[int, list[int]] = {}
    for index, point in enumerate(right):
        right_index.setdefault(point, []).append(index)
    shifts: set[int] = set()
    for left_index, point in enumerate(left):
        for delta in range(
            -settings["invertedIndexShift"], settings["invertedIndexShift"] + 1
        ):
            for right_position in right_index.get((point + delta) & 0xFFFFFFFF, ()):
                shifts.add(right_position - left_index)
    best = None
    max_gap = max(1, int(settings["maximumTimeSkipSeconds"] / SAMPLE_SECONDS))
    for shift in shifts:
        start = max(0, -shift)
        stop = min(len(left), len(right) - shift)
        run_start = run_end = match_count = None
        previous = None
        for left_index in range(start, stop):
            if (
                _bits(left[left_index] ^ right[left_index + shift])
                > settings["maximumFingerprintPointDifferences"]
            ):
                continue
            if previous is None or left_index - previous > max_gap:
                if run_start is not None:
                    best = _select_region(
                        best, run_start, run_end, match_count, shift, minimum, maximum
                    )
                run_start = left_index
                match_count = 0
            run_end = left_index
            match_count += 1
            previous = left_index
        if run_start is not None:
            best = _select_region(
                best, run_start, run_end, match_count, shift, minimum, maximum
            )
    return best


def _select_region(
    best, start: int, end: int, matches: int, shift: int, minimum: float, maximum: float
):
    duration = (end - start + 1) * SAMPLE_SECONDS
    density = matches / (end - start + 1)
    if duration < minimum or duration > maximum or density < MIN_MATCH_DENSITY:
        return best
    candidate = (
        start * SAMPLE_SECONDS,
        (end + 1) * SAMPLE_SECONDS,
        (start + shift) * SAMPLE_SECONDS,
        (end + shift + 1) * SAMPLE_SECONDS,
    )
    return candidate if best is None or duration > best[1] - best[0] else best


class IntroOutroDetector:
    def __init__(self, store=None):
        self.store = store or IntroOutroStore()

    @staticmethod
    def fingerprint_command(path: Path, start: float, duration: float) -> list[str]:
        executable = ffmpeg_path()
        if not executable:
            raise RuntimeError("FFmpeg is not available.")
        return [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-t",
            str(duration),
            "-map",
            "0:a:0",
            "-ac",
            "2",
            "-f",
            "chromaprint",
            "-fp_format",
            "raw",
            "-",
        ]

    def _fingerprint(
        self, path: Path, start: float, duration: float, should_terminate
    ) -> bytes:
        if not path.is_file():
            raise RuntimeError("Media source is unavailable.")
        process = subprocess.Popen(
            self.fingerprint_command(path, start, duration),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        while True:
            try:
                output, error = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if should_terminate():
                process.terminate()
                process.communicate(timeout=10)
                raise RuntimeError("Terminated by administrator")
        if process.returncode or not output or len(output) % 4:
            raise RuntimeError(
                (
                    error.decode("utf-8", "replace")
                    or "FFmpeg did not return a raw Chromaprint fingerprint."
                ).strip()[-1000:]
            )
        return output

    def run(self, run_id: str, job_store, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        recover = getattr(self.store, "recover_generating", None)
        if recover is not None:
            recover()
        settings = self.store.settings()
        queued = self.store.queue_pending(settings=settings)
        workers = settings["introOutroWorkers"]
        job_store.update_run(
            run_id,
            state="running",
            started_at=now(),
            message="Detecting intro and outro segments",
        )
        completed = failures = markers = 0
        progress_lock = Lock()

        def process_assets():
            nonlocal completed, failures
            while not should_terminate():
                asset = self.store.claim_next()
                if not asset:
                    return
                try:
                    duration = asset["durationSeconds"]
                    intro_duration = min(
                        duration * settings["analysisPercent"] / 100.0,
                        settings["analysisLengthLimitMinutes"] * 60.0,
                    )
                    outro_duration = min(
                        duration, float(settings["maximumCreditsAnalysisSeconds"])
                    )
                    outro_start = max(0.0, duration - outro_duration)
                    intro = (
                        self._fingerprint(
                            asset["path"], 0.0, intro_duration, should_terminate
                        )
                        if settings["scanIntroduction"]
                        else None
                    )
                    outro = (
                        self._fingerprint(
                            asset["path"], outro_start, outro_duration, should_terminate
                        )
                        if settings["scanCredits"]
                        else None
                    )
                    self.store.mark_fingerprinted(asset, intro, outro)
                    with progress_lock:
                        completed += 1
                        current = completed
                        job_store.update_run(
                            run_id,
                            progress_current=current,
                            progress_total=max(current, queued),
                            message=f"Fingerprinting episode {current}",
                        )
                except Exception as error:
                    if should_terminate():
                        self.store.requeue(asset)
                        return
                    self.store.mark_failed(asset, str(error))
                    with progress_lock:
                        failures += 1
                    logger.warning(
                        "intro/outro detection failed entity_id=%s media_file_id=%s error=%s",
                        asset["entityId"],
                        asset["mediaFileId"],
                        error,
                    )

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="intro-outro"
        ) as executor:
            futures = [executor.submit(process_assets) for _ in range(workers)]
            for future in futures:
                future.result()
        if should_terminate():
            job_store.update_run(
                run_id,
                state="terminated",
                finished_at=now(),
                message="Terminated by administrator",
            )
        elif failures:
            message = f"Fingerprinted {completed} episodes; {failures} failed"
            job_store.update_run(
                run_id,
                state="failed",
                progress_current=completed,
                progress_total=max(completed + failures, queued),
                finished_at=now(),
                message=message,
                error=message,
            )
        else:
            markers = self.store.recompute_all(settings)
            job_store.update_run(
                run_id,
                state="completed",
                progress_current=completed,
                progress_total=max(completed, queued),
                finished_at=now(),
                message=f"Detected {markers} intro/outro markers"
                if markers
                else "Intro and outro detection is current",
            )
