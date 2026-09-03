from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from threading import Lock

from app.config import Config
from app.ffmpeg_supervisor import run_ffmpeg
from app.logging_config import get_logger
from app.media_probe import first_audio_stream, probe_streams, stream_end_seconds
from app.playback import PLAYABLE_ROLE, ffmpeg_path
from app.progress import (
    ProgressReporter,
    format_progress_message,
    resolve_progress_item,
)
from fastapi import HTTPException

logger = get_logger("intro_outro")
DEFAULT_INTRO_OUTRO_FFMPEG_THREADS = 4
SAMPLE_SECONDS = 4096.0 / 11025.0 / 3.0
MIN_MATCH_DENSITY = 0.55
MIN_FINGERPRINT_WINDOW_SECONDS = 1.0


class EmptyFingerprint(RuntimeError):
    """FFmpeg produced no Chromaprint points for the requested audio window."""


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
    "introOutroFfmpegThreads": 4,
}

COMPARISON_SETTING_KEYS = (
    "analysisPercent",
    "analysisLengthLimitMinutes",
    "scanIntroduction",
    "scanCredits",
    "minimumIntroDuration",
    "maximumIntroDuration",
    "minimumCreditsDuration",
    "maximumCreditsAnalysisSeconds",
    "maximumFingerprintPointDifferences",
    "maximumTimeSkipSeconds",
    "invertedIndexShift",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_settings(values: dict | None = None) -> dict:
    values = {**DEFAULTS, **(values or {})}

    def integer(key: str, minimum: int, maximum: int):
        try:
            integer_value = int(values[key])
        except (TypeError, ValueError):
            return DEFAULTS[key]
        if key == "introOutroFfmpegThreads" and not minimum <= integer_value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}.")
        return max(minimum, min(maximum, integer_value))

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
        "introOutroFfmpegThreads": integer("introOutroFfmpegThreads", 0, 64),
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


def _fingerprint_digest(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    elif not isinstance(value, (bytes, bytearray)):
        value = str(value).encode()
    return hashlib.sha256(bytes(value)).hexdigest()


def comparison_key(settings: dict, rows) -> str:
    """Return the stable input key for one season's marker comparison."""
    normalized = normalize_settings(settings)
    payload = {
        "settings": {key: normalized[key] for key in COMPARISON_SETTING_KEYS},
        "episodes": [
            {
                "mediaFileId": row[0],
                "sourceFingerprint": row[1],
                "durationSeconds": float(row[2] or 0),
                "introFingerprint": _fingerprint_digest(row[3]),
                "outroFingerprint": _fingerprint_digest(row[4]),
                "warning": row[5],
            }
            for row in rows
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class IntroOutroStore:
    def __init__(self, db=None):
        self.db = db or Config().database

    @staticmethod
    def source_key(quick_fingerprint, size, modified_ns) -> str:
        return str(quick_fingerprint or f"{int(size or 0)}:{int(modified_ns or 0)}")

    def available(self) -> bool:
        tables = self._tables()
        return {
            "intro_outro_settings",
            "intro_outro_assets",
            "intro_outro_segments",
        }.issubset(tables)

    def _tables(self) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    def _comparison_state_available(self) -> bool:
        return "intro_outro_comparison_state" in self._tables()

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
            ("introOutroFfmpegThreads", "intro_outro_ffmpeg_threads"),
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
            ("introOutroFfmpegThreads", "intro_outro_ffmpeg_threads"),
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

    def _validate_library_scope(self, library_id: str | None) -> None:
        if library_id is None:
            return
        if not isinstance(library_id, str) or not library_id.strip():
            raise ValueError("libraryId must be a library ID or null.")
        rows = self.db.execute("SELECT type FROM libraries WHERE id=?", (library_id,))
        if not rows:
            raise LookupError("Library not found.")
        if rows[0][0] != "tv_series":
            raise ValueError("Intro/outro cleanup is only available for TV libraries.")

    @staticmethod
    def _zero_cleanup_result(
        library_id: str | None, data_type: str
    ) -> dict[str, object]:
        return {
            "libraryId": library_id,
            "dataType": data_type,
            "removedFingerprints": 0,
            "removedSegments": 0,
            "invalidatedSeasons": 0,
            "queuedEpisodes": 0,
        }

    @staticmethod
    def _asset_scope(library_id: str | None) -> tuple[str, list[object]]:
        if library_id is None:
            return "", []
        return (
            " WHERE media_file_id IN ("
            "SELECT asset.media_file_id FROM intro_outro_assets asset "
            "JOIN library_entities entity ON entity.id=asset.entity_id "
            "JOIN libraries library ON library.id=entity.library_id "
            "WHERE entity.library_id=? AND library.type='tv_series'"
            ")",
            [library_id],
        )

    def clear_data(
        self, library_id: str | None = None, data_type: str = "segments"
    ) -> dict[str, object]:
        """Remove cached intro/outro analysis data for one TV library or all TV libraries."""
        if not isinstance(data_type, str) or data_type not in {
            "fingerprints",
            "segments",
        }:
            raise ValueError("dataType must be fingerprints or segments.")
        self._validate_library_scope(library_id)
        tables = self._tables()
        if not {"intro_outro_assets", "intro_outro_segments"}.issubset(tables):
            return self._zero_cleanup_result(library_id, data_type)

        asset_where, asset_params = self._asset_scope(library_id)
        comparison_where = ""
        comparison_params: list[object] = []
        if library_id is not None:
            comparison_where = (
                " WHERE season_id IN ("
                "SELECT entity.id FROM library_entities entity "
                "JOIN libraries library ON library.id=entity.library_id "
                "WHERE entity.library_id=? AND library.type='tv_series'"
                ")"
            )
            comparison_params = [library_id]

        asset_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(intro_outro_assets)")
        }
        update_values = [
            "intro_fingerprint=NULL",
            "outro_fingerprint=NULL",
            "state='queued'",
        ]
        if "error" in asset_columns:
            update_values.append("error=NULL")
        if "updated_at" in asset_columns:
            update_values.append("updated_at=?")
        update_params = (
            [now()] if "updated_at" in asset_columns else []
        ) + asset_params

        with self.db.transaction() as cursor:
            fingerprint_row = cursor.execute(
                "SELECT COALESCE(SUM((intro_fingerprint IS NOT NULL) + "
                "(outro_fingerprint IS NOT NULL)),0) FROM intro_outro_assets"
                + asset_where,
                asset_params,
            ).fetchone()
            segment_row = cursor.execute(
                "SELECT COUNT(*) FROM intro_outro_segments" + asset_where,
                asset_params,
            ).fetchone()
            removed_fingerprints = (
                int(fingerprint_row[0] or 0) if fingerprint_row else 0
            )
            removed_segments = int(segment_row[0] or 0) if segment_row else 0

            invalidated_seasons = 0
            if "intro_outro_comparison_state" in tables:
                comparison_row = cursor.execute(
                    "SELECT COUNT(*) FROM intro_outro_comparison_state"
                    + comparison_where,
                    comparison_params,
                ).fetchone()
                invalidated_seasons = (
                    int(comparison_row[0] or 0) if comparison_row else 0
                )

            queued_episodes = 0
            if data_type == "fingerprints":
                cursor.execute(
                    "UPDATE intro_outro_assets SET "
                    + ",".join(update_values)
                    + asset_where,
                    update_params,
                )
                queued_episodes = max(0, cursor.rowcount)

            cursor.execute(
                "DELETE FROM intro_outro_segments" + asset_where,
                asset_params,
            )
            if "intro_outro_comparison_state" in tables:
                cursor.execute(
                    "DELETE FROM intro_outro_comparison_state" + comparison_where,
                    comparison_params,
                )

        return {
            "libraryId": library_id,
            "dataType": data_type,
            "removedFingerprints": removed_fingerprints
            if data_type == "fingerprints"
            else 0,
            "removedSegments": removed_segments,
            "invalidatedSeasons": invalidated_seasons,
            "queuedEpisodes": queued_episodes,
        }

    def clear_fingerprints(self, library_id: str | None = None) -> dict[str, object]:
        return self.clear_data(library_id, "fingerprints")

    def clear_segments(self, library_id: str | None = None) -> int:
        return int(self.clear_data(library_id, "segments")["removedSegments"])

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
                "SELECT source_fingerprint,analysis_key,state,error FROM intro_outro_assets WHERE media_file_id=?",
                (media_file_id,),
            )
            if (
                existing
                and existing[0][0] == source_fingerprint
                and existing[0][1] == fingerprint_settings_key
            ):
                state = existing[0][2]
                warning_retry = state == "scanned" and bool(existing[0][3])
                if (
                    state in {"scanned", "fingerprinted", "generating"}
                    and not warning_retry
                ):
                    continue
                if state in {"queued", "failed"} or warning_retry:
                    timestamp = now()
                    self.db.execute(
                        "UPDATE intro_outro_assets SET entity_id=?,season_id=?,state='queued',"
                        "error=NULL,updated_at=? WHERE media_file_id=?",
                        (entity_id, season_id, timestamp, media_file_id),
                    )
                    if not warning_retry:
                        self.db.execute(
                            "DELETE FROM intro_outro_segments WHERE media_file_id=?",
                            (media_file_id,),
                        )
                    queued += 1
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
        source_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(media_sources)")
        }
        probe_payload_select = (
            ",source.probe_payload" if "probe_payload" in source_columns else ""
        )
        query = (
            "SELECT a.media_file_id,a.entity_id,a.season_id,a.source_fingerprint,source.duration_seconds,"
            "library.directory,f.relative_path"
            + probe_payload_select
            + " FROM intro_outro_assets a JOIN media_files f ON f.id=a.media_file_id "
            + "JOIN media_sources source ON source.media_file_id=f.id "
            + "JOIN library_entities entity ON entity.id=a.entity_id JOIN libraries library ON library.id=entity.library_id "
            + "WHERE a.state='queued' ORDER BY a.updated_at,a.media_file_id LIMIT 1"
        )
        with self.db.transaction() as cursor:
            cursor.execute(query)
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE intro_outro_assets SET state='generating',error=NULL,updated_at=? WHERE media_file_id=? AND state='queued'",
                (now(), row[0]),
            )
            if cursor.rowcount != 1:
                return None
        duration_seconds = float(row[4] or 0)
        probe_payload = row[7] if probe_payload_select else None
        streams = probe_streams(probe_payload)
        audio_stream = first_audio_stream(streams) if streams is not None else None
        audio_end = (
            stream_end_seconds(audio_stream, duration_seconds)
            if audio_stream is not None
            else (0.0 if streams is not None else None)
        )
        return {
            "mediaFileId": row[0],
            "entityId": row[1],
            "seasonId": row[2],
            "sourceFingerprint": row[3],
            "durationSeconds": duration_seconds,
            "audioEndSeconds": audio_end,
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
        self,
        asset: dict,
        intro: bytes | None,
        outro: bytes | None,
        warning: str | None = None,
    ) -> None:
        timestamp = now()
        if warning:
            self.db.execute(
                "UPDATE intro_outro_assets SET intro_fingerprint=COALESCE(?,intro_fingerprint),"
                "outro_fingerprint=COALESCE(?,outro_fingerprint),state='scanned',error=?,updated_at=? "
                "WHERE media_file_id=? AND source_fingerprint=? AND state='generating'",
                (
                    intro,
                    outro,
                    warning[:1000],
                    timestamp,
                    asset["mediaFileId"],
                    asset["sourceFingerprint"],
                ),
            )
            return
        self.db.execute(
            "UPDATE intro_outro_assets SET intro_fingerprint=?,outro_fingerprint=?,state='scanned',error=NULL,updated_at=? "
            "WHERE media_file_id=? AND source_fingerprint=? AND state='generating'",
            (intro, outro, timestamp, asset["mediaFileId"], asset["sourceFingerprint"]),
        )

    def mark_failed(self, asset: dict, error: str) -> None:
        self.db.execute(
            "UPDATE intro_outro_assets SET state='failed',error=?,updated_at=? WHERE media_file_id=? AND source_fingerprint=? AND state='generating'",
            (error[:1000], now(), asset["mediaFileId"], asset["sourceFingerprint"]),
        )
        self.db.execute(
            "DELETE FROM intro_outro_segments WHERE media_file_id=? AND EXISTS ("
            "SELECT 1 FROM intro_outro_assets WHERE media_file_id=? "
            "AND source_fingerprint=? AND state='failed')",
            (
                asset["mediaFileId"],
                asset["mediaFileId"],
                asset["sourceFingerprint"],
            ),
        )

    def recompute_all(self, settings: dict, progress=None) -> int:
        if self._comparison_state_available():
            rows = self.db.execute(
                "SELECT season_id FROM intro_outro_assets "
                "UNION SELECT season_id FROM intro_outro_comparison_state "
                "ORDER BY season_id"
            )
        else:
            rows = self.db.execute(
                "SELECT DISTINCT season_id FROM intro_outro_assets WHERE state='scanned'"
            )
        total = len(rows)
        completed = 0
        markers = 0
        for row in rows:
            markers += self.recompute_season(row[0], settings)
            completed += 1
            if progress:
                progress(completed, total, row[0])
        return markers

    def recompute_season(self, season_id: str, settings: dict) -> int:
        settings = normalize_settings(settings)
        rows = self.db.execute(
            "SELECT a.media_file_id,a.source_fingerprint,"
            "COALESCE(source.duration_seconds,0),a.intro_fingerprint,"
            "a.outro_fingerprint,a.error FROM intro_outro_assets a "
            "LEFT JOIN media_sources source ON source.media_file_id=a.media_file_id "
            "WHERE a.season_id=? AND a.state='scanned' "
            "ORDER BY a.media_file_id",
            (season_id,),
        )
        current_key = comparison_key(settings, rows)
        state_available = self._comparison_state_available()
        stored_key = None
        if state_available:
            state_rows = self.db.execute(
                "SELECT comparison_key FROM intro_outro_comparison_state WHERE season_id=?",
                (season_id,),
            )
            stored_key = state_rows[0][0] if state_rows else None
            if stored_key == current_key:
                return self._segment_count(season_id)

        selected: dict[tuple[str, str], tuple[float, float]] = {}
        if len(rows) >= 2:
            kinds = []
            if settings["scanIntroduction"]:
                kinds.append(
                    (
                        "intro",
                        3,
                        settings["minimumIntroDuration"],
                        settings["maximumIntroDuration"],
                        0.0,
                    )
                )
            if settings["scanCredits"]:
                kinds.append(
                    (
                        "outro",
                        4,
                        settings["minimumCreditsDuration"],
                        settings["maximumCreditsAnalysisSeconds"],
                        None,
                    )
                )
            for index, left in enumerate(rows):
                for right in rows[index + 1 :]:
                    for kind, column, minimum, maximum, offset in kinds:
                        left_points = decode_fingerprint(left[column])
                        right_points = decode_fingerprint(right[column])
                        result = shared_region(
                            left_points, right_points, settings, minimum, maximum
                        )
                        if not result:
                            continue
                        left_start, left_end, right_start, right_end = result
                        left_duration = float(left[2] or 0)
                        right_duration = float(right[2] or 0)
                        left_offset = (
                            offset
                            if offset is not None
                            else max(
                                0.0,
                                left_duration
                                - settings["maximumCreditsAnalysisSeconds"],
                            )
                        )
                        right_offset = (
                            offset
                            if offset is not None
                            else max(
                                0.0,
                                right_duration
                                - settings["maximumCreditsAnalysisSeconds"],
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

        existing_rows = self.db.execute(
            "SELECT segment.media_file_id,segment.segment_type,"
            "segment.start_seconds,segment.end_seconds "
            "FROM intro_outro_segments segment "
            "JOIN intro_outro_assets asset ON asset.media_file_id=segment.media_file_id "
            "WHERE asset.season_id=?",
            (season_id,),
        )
        existing = {
            (row[0], row[1]): (float(row[2]), float(row[3])) for row in existing_rows
        }
        deleted = sorted(set(existing) - set(selected))
        changed = sorted(
            key for key, value in selected.items() if existing.get(key) != value
        )
        state_changed = state_available and stored_key != current_key
        if deleted or changed or state_changed:
            with self.db.transaction() as cursor:
                for media_file_id, kind in deleted:
                    cursor.execute(
                        "DELETE FROM intro_outro_segments "
                        "WHERE media_file_id=? AND segment_type=?",
                        (media_file_id, kind),
                    )
                for media_file_id, kind in changed:
                    start, end = selected[(media_file_id, kind)]
                    if (media_file_id, kind) in existing:
                        cursor.execute(
                            "UPDATE intro_outro_segments SET start_seconds=?,end_seconds=? "
                            "WHERE media_file_id=? AND segment_type=?",
                            (start, end, media_file_id, kind),
                        )
                    else:
                        cursor.execute(
                            "INSERT INTO intro_outro_segments "
                            "(media_file_id,segment_type,start_seconds,end_seconds) "
                            "VALUES(?,?,?,?)",
                            (media_file_id, kind, start, end),
                        )
                if state_available:
                    cursor.execute(
                        "INSERT INTO intro_outro_comparison_state "
                        "(season_id,comparison_key,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(season_id) DO UPDATE SET "
                        "comparison_key=excluded.comparison_key,"
                        "updated_at=excluded.updated_at",
                        (season_id, current_key, now()),
                    )
        return len(selected)

    def _segment_count(self, season_id: str) -> int:
        rows = self.db.execute(
            "SELECT COUNT(*) FROM intro_outro_segments segment "
            "JOIN intro_outro_assets asset ON asset.media_file_id=segment.media_file_id "
            "WHERE asset.season_id=?",
            (season_id,),
        )
        return int(rows[0][0] or 0) if rows else 0

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
        self.ffmpeg_threads = DEFAULT_INTRO_OUTRO_FFMPEG_THREADS

    @staticmethod
    def fingerprint_command(
        path: Path,
        start: float,
        duration: float,
        ffmpeg_threads: int = DEFAULT_INTRO_OUTRO_FFMPEG_THREADS,
    ) -> list[str]:
        executable = ffmpeg_path()
        if not executable:
            raise RuntimeError("FFmpeg is not available.")
        return [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-threads",
            str(ffmpeg_threads),
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
        output = run_ffmpeg(
            self.fingerprint_command(
                path,
                start,
                duration,
                getattr(self, "ffmpeg_threads", DEFAULT_INTRO_OUTRO_FFMPEG_THREADS),
            ),
            should_terminate=should_terminate,
        )
        if not output or len(output) % 4:
            if not output:
                raise EmptyFingerprint(
                    "FFmpeg did not return a raw Chromaprint fingerprint."
                )
            raise RuntimeError(
                "FFmpeg returned an invalid raw Chromaprint fingerprint."
            )
        return output

    def run(self, run_id: str, job_store, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        recover = getattr(self.store, "recover_generating", None)
        if recover is not None:
            recover()
        settings = self.store.settings()
        self.ffmpeg_threads = settings["introOutroFfmpegThreads"]
        queued = self.store.queue_pending(settings=settings)
        workers = settings["introOutroWorkers"]
        job_store.update_run(
            run_id,
            state="running",
            started_at=now(),
            message=format_progress_message(
                "Preparing intro/outro analysis", detail=f"{queued} episodes queued"
            ),
            progress_phase="preparation",
            progress_label="Preparing intro/outro analysis",
            progress_stage_current=0,
            progress_stage_total=queued,
            progress_stage_unit="episodes",
        )
        completed = partial_count = failures = markers = 0
        progress_lock = Lock()
        reporter = ProgressReporter(
            partial(job_store.update_run, run_id), unit="episodes"
        )
        reporter.stage("fingerprinting", "Fingerprinting intros/outros", total=queued)

        def process_assets():
            nonlocal completed, partial_count, failures
            while not should_terminate():
                asset = self.store.claim_next()
                if not asset:
                    return
                try:
                    item_label = resolve_progress_item(
                        getattr(self.store, "db", None),
                        asset.get("entityId"),
                        asset.get("path"),
                    )
                    reporter.start(item_label)
                    duration = asset["durationSeconds"]
                    audio_end = asset.get("audioEndSeconds")
                    try:
                        audio_end = float(audio_end) if audio_end is not None else None
                    except (TypeError, ValueError):
                        audio_end = None
                    if audio_end is not None:
                        audio_end = max(0.0, min(duration, audio_end))
                    intro_duration = min(
                        duration * settings["analysisPercent"] / 100.0,
                        settings["analysisLengthLimitMinutes"] * 60.0,
                    )
                    if audio_end is not None:
                        intro_duration = min(intro_duration, audio_end)
                    outro_duration = min(
                        duration, float(settings["maximumCreditsAnalysisSeconds"])
                    )
                    outro_start = max(0.0, duration - outro_duration)
                    if audio_end is not None:
                        outro_duration = max(
                            0.0, min(duration, audio_end) - outro_start
                        )
                    window_errors: list[str] = []

                    def fingerprint_window(
                        kind: str, start: float, window_duration: float
                    ) -> bytes | None:
                        try:
                            return self._fingerprint(
                                asset["path"], start, window_duration, should_terminate
                            )
                        except Exception as error:
                            if should_terminate():
                                raise
                            window_errors.append(f"{kind}: {error}")
                            return None

                    intro = (
                        fingerprint_window("intro", 0.0, intro_duration)
                        if settings["scanIntroduction"]
                        and intro_duration >= MIN_FINGERPRINT_WINDOW_SECONDS
                        else None
                    )
                    outro = (
                        fingerprint_window("outro", outro_start, outro_duration)
                        if settings["scanCredits"]
                        and outro_duration >= MIN_FINGERPRINT_WINDOW_SECONDS
                        else None
                    )
                    if window_errors and intro is None and outro is None:
                        raise RuntimeError("; ".join(window_errors))
                    warning = "; ".join(window_errors) if window_errors else None
                    self.store.mark_fingerprinted(asset, intro, outro, warning)
                    with progress_lock:
                        if warning:
                            partial_count += 1
                        else:
                            completed += 1
                    reporter.settle(item_label)
                except Exception as error:
                    if should_terminate():
                        self.store.requeue(asset)
                        return
                    self.store.mark_failed(asset, str(error))
                    with progress_lock:
                        failures += 1
                    reporter.settle(
                        resolve_progress_item(
                            getattr(self.store, "db", None),
                            asset.get("entityId"),
                            asset.get("path"),
                        ),
                        failed=True,
                    )
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
        else:
            reporter.stage("comparison", "Comparing fingerprints", total=None)
            comparison_progress = lambda current, total, season: job_store.update_run(
                run_id,
                progress_current=current,
                progress_total=total,
                progress_phase="comparison",
                progress_label="Comparing fingerprints",
                progress_stage_current=current,
                progress_stage_total=total,
                progress_stage_unit="seasons",
                progress_current_item=resolve_progress_item(
                    getattr(self.store, "db", None), season, season
                ),
                message=format_progress_message(
                    "Comparing fingerprints",
                    item=resolve_progress_item(
                        getattr(self.store, "db", None), season, season
                    ),
                    current=current,
                    total=total,
                    unit="seasons",
                ),
            )
            try:
                markers = self.store.recompute_all(
                    settings, progress=comparison_progress
                )
            except TypeError as error:
                if "progress" not in str(error):
                    raise
                markers = self.store.recompute_all(settings)
            reporter.finish()
            processed = completed + partial_count
            warning_count = partial_count + failures
            if warning_count:
                state = "completed_with_warnings"
                message = (
                    f"Detected {markers} intro/outro markers; "
                    f"fingerprinted {completed} episodes; {partial_count} partial; "
                    f"{failures} failed"
                )
            else:
                state = "completed"
                message = (
                    f"Detected {markers} intro/outro markers"
                    if markers
                    else "Intro and outro detection is current"
                )
            job_store.update_run(
                run_id,
                state=state,
                progress_current=processed,
                progress_total=max(processed + failures, queued),
                finished_at=now(),
                message=message,
                error=None,
            )
