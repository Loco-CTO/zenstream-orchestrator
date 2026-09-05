from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.catalog import Catalog
from app.client_auth import issue_ticket
from app.config import Config
from app.language_registry import normalize_track_language
from app.library import language_name, sidecar_display_title, sidecar_media_path
from app.logging_config import get_logger
from app.media_probe import first_audio_stream, select_usable_video_stream
from app.models.playback_settings import PlaybackSettings
from app.models.playback_viewer import PlaybackViewerStore
from fastapi import HTTPException

logger = get_logger("playback")
PLAYBACK_RESOURCE_TICKET_TTL_SECONDS = 15 * 60
PLAYABLE_ROLE = "media"


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


def _media_tool_path(name: str) -> str | None:
    override = os.getenv("FFMPEG_PATH" if name == "ffmpeg" else "FFPROBE_PATH")
    if override:
        return override
    executable = f"{name}.exe" if platform.system() == "Windows" else name
    roots = [
        Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", None) else None,
        Path(__file__).resolve().parents[2],
    ]
    platform_name = {
        "Windows": "windows",
        "Darwin": "macos",
    }.get(platform.system(), "linux")
    for root in roots:
        if root is None:
            continue
        candidate = root / "assets" / "ffmpeg" / platform_name / executable
        if candidate.is_file():
            return str(candidate)
    return shutil.which(executable) or shutil.which(name)


def ffmpeg_path() -> str | None:
    return _media_tool_path("ffmpeg")


def ffprobe_path() -> str | None:
    return _media_tool_path("ffprobe")


class PlaybackManager:
    _lock = threading.RLock()
    _cleanup_thread_lock = threading.Lock()
    _cleanup_stop = threading.Event()
    _cleanup_thread: threading.Thread | None = None
    _processes: dict[str, subprocess.Popen] = {}
    _users: dict[str, set[str]] = {}
    _session_keys: dict[str, tuple[str, str, str, str]] = {}
    _seek_generations: dict[tuple[str, str, str], int] = {}
    _session_specs: dict[str, dict] = {}
    _session_workers: dict[str, dict] = {}
    _session_locks: dict[str, threading.RLock] = {}
    _segment_seconds = 4.0
    # Startup and segment production are allowed to be slow while FFmpeg is
    # still alive. The web client has a matching readiness deadline and can
    # retry a segment request after the bounded wait expires.
    _startup_timeout_seconds = 30.0
    _segment_wait_timeout_seconds = 45.0

    @staticmethod
    def _idle_timeout_seconds() -> float:
        try:
            value = float(os.getenv("PLAYBACK_SESSION_IDLE_TIMEOUT_SECONDS", "45"))
        except (TypeError, ValueError):
            value = 45.0
        return max(15.0, min(value, 3600.0))

    def __init__(self):
        self.db = Config().database
        self.catalog = Catalog()
        self._start_cleanup_thread()

    def _start_cleanup_thread(self) -> None:
        manager_type = type(self)
        with manager_type._cleanup_thread_lock:
            if manager_type._cleanup_thread and manager_type._cleanup_thread.is_alive():
                return
            manager_type._cleanup_stop = threading.Event()
            manager_type._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="zenstream-playback-cleanup",
                daemon=True,
            )
            manager_type._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        manager_type = type(self)
        while not manager_type._cleanup_stop.wait(
            min(15.0, max(5.0, self._idle_timeout_seconds() / 3.0))
        ):
            try:
                self._cleanup_expired()
            except Exception:
                logger.exception("playback background cleanup failed")

    @classmethod
    def stop_all(cls) -> None:
        with cls._cleanup_thread_lock:
            cleanup_stop = cls._cleanup_stop
            cleanup_thread = cls._cleanup_thread
            cleanup_stop.set()
        with cls._lock:
            sessions = list(cls._processes.items())
        for session_id, process in sessions:
            cls._stop_process(process, f"shutdown session_id={session_id}")
            with cls._lock:
                cls._processes.pop(session_id, None)
                cls._remove_session_indexes_locked(session_id)
        try:
            database = Config().database
            persisted = database.execute(
                "SELECT id,process_id FROM playback_sessions WHERE state IN ('starting','ready','stopping') AND process_id IS NOT NULL"
            )
            for session_id, process_id in persisted or []:
                if any(session_id == active_id for active_id, _ in sessions):
                    continue
                cls._stop_process_id(
                    process_id, f"shutdown persisted session_id={session_id}"
                )
                database.execute(
                    "UPDATE playback_sessions SET state='stopping',completed_at=?,process_id=NULL WHERE id=? AND process_id=?",
                    (_iso(), session_id, process_id),
                )
        except Exception:
            logger.exception(
                "could not clean persisted playback workers during shutdown"
            )
        with cls._lock:
            cls._users.clear()
            cls._session_keys.clear()
            cls._session_specs.clear()
            cls._session_workers.clear()
            cls._session_locks.clear()
            cls._seek_generations.clear()
        if cleanup_thread and cleanup_thread is not threading.current_thread():
            cleanup_thread.join(timeout=2)
        with cls._cleanup_thread_lock:
            if cls._cleanup_thread is cleanup_thread:
                cls._cleanup_thread = None

    @classmethod
    def _remove_session_indexes_locked(cls, session_id: str) -> None:
        spec = cls._session_specs.pop(session_id, None)
        user_id = spec.get("user_id") if isinstance(spec, dict) else None
        if user_id:
            sessions = cls._users.get(user_id)
            if sessions is not None:
                sessions.discard(session_id)
                if not sessions:
                    cls._users.pop(user_id, None)
        key = cls._session_keys.pop(session_id, None)
        cls._session_workers.pop(session_id, None)
        cls._session_locks.pop(session_id, None)
        if key is not None:
            base_key = key[:3]
            if not any(
                existing[:3] == base_key for existing in cls._session_keys.values()
            ):
                cls._seek_generations.pop(base_key, None)

    @classmethod
    def _remove_active_user_index_locked(
        cls, session_id: str, user_id: str | None = None
    ) -> None:
        if user_id is None:
            spec = cls._session_specs.get(session_id)
            user_id = spec.get("user_id") if isinstance(spec, dict) else None
        if not user_id:
            return
        sessions = cls._users.get(user_id)
        if sessions is not None:
            sessions.discard(session_id)
            if not sessions:
                cls._users.pop(user_id, None)

    @classmethod
    def prune_runtime_state(cls) -> None:
        """Remove inactive user indexes and stale generation entries."""
        with cls._lock:
            for user_id, session_ids in list(cls._users.items()):
                for session_id in list(session_ids):
                    process = cls._processes.get(session_id)
                    if process is None or process.poll() is not None:
                        session_ids.discard(session_id)
                if not session_ids:
                    cls._users.pop(user_id, None)
            active_bases = {key[:3] for key in cls._session_keys.values()}
            for base_key in list(cls._seek_generations):
                if base_key not in active_bases:
                    cls._seek_generations.pop(base_key, None)

    @staticmethod
    def _stop_process(process: subprocess.Popen | None, reason: str) -> None:
        if process is None or process.poll() is not None:
            return
        logger.info("stopping playback worker pid=%s reason=%s", process.pid, reason)
        try:
            process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning(
                "killing unresponsive playback worker pid=%s reason=%s",
                process.pid,
                reason,
            )
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.error(
                    "playback worker did not exit after kill pid=%s reason=%s",
                    process.pid,
                    reason,
                )

    @staticmethod
    def _stop_process_id(process_id: int | None, reason: str) -> None:
        if not process_id or process_id <= 0:
            return
        logger.info(
            "stopping persisted playback worker pid=%s reason=%s", process_id, reason
        )
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                logger.warning(
                    "could not stop persisted playback worker pid=%s reason=%s error=%s",
                    process_id,
                    reason,
                    error,
                )
                return
            if result.returncode not in {0, 128}:
                logger.warning(
                    "taskkill failed for persisted playback worker pid=%s reason=%s exit_code=%s stderr=%s",
                    process_id,
                    reason,
                    result.returncode,
                    (result.stderr or "").strip()[-500:],
                )
            return
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as error:
            logger.warning(
                "could not stop persisted playback worker pid=%s reason=%s error=%s",
                process_id,
                reason,
                error,
            )
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(process_id, 0)
            except (ProcessLookupError, OSError):
                return
            time.sleep(0.1)
        try:
            os.kill(process_id, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    @staticmethod
    def _process_id_alive(process_id: int | None) -> bool:
        if not process_id or process_id <= 0:
            return False
        try:
            os.kill(process_id, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    @staticmethod
    def _limits() -> tuple[int, int]:
        settings = PlaybackSettings().get()
        return settings["maxTranscodes"], settings["maxTranscodesPerUser"]

    def _file_path(
        self,
        entity_id: str,
        media_file_id: str | None = None,
        role: str = PLAYABLE_ROLE,
    ) -> tuple[str, Path]:
        params: list = [entity_id, role]
        where = "f.entity_id=? AND f.role=?"
        if media_file_id:
            where += " AND f.id=?"
            params.append(media_file_id)
        rows = self.db.execute(
            f"SELECT f.id,l.directory,f.relative_path FROM media_files f JOIN library_entities e ON e.id=f.entity_id JOIN libraries l ON l.id=e.library_id WHERE {where} ORDER BY f.size DESC LIMIT 1",
            params,
        )
        if not rows:
            raise HTTPException(404, "Media source not found.")
        root = Path(rows[0][1]).resolve()
        path = root / rows[0][2]
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404, "Media source is unavailable.")
        if path.is_symlink() or not resolved.is_file():
            raise HTTPException(404, "Media source is unavailable.")
        return rows[0][0], resolved

    def probe_entity(self, entity_id: str) -> list[dict]:
        executable = ffprobe_path()
        if not executable:
            return []
        rows = self.db.execute(
            "SELECT f.id,l.directory,f.relative_path FROM media_files f JOIN library_entities e ON e.id=f.entity_id JOIN libraries l ON l.id=e.library_id WHERE f.entity_id=? AND f.role=?",
            (entity_id, PLAYABLE_ROLE),
        )
        values = []
        track_index_statements = []
        has_track_index = bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_track_languages'"
            )
        )
        for media_file_id, directory, relative_path in rows:
            root = Path(directory).resolve()
            path = root / relative_path
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            if path.is_symlink() or not resolved.is_file():
                continue
            try:
                completed = subprocess.run(
                    [
                        executable,
                        "-v",
                        "error",
                        "-show_format",
                        "-show_streams",
                        "-of",
                        "json",
                        str(resolved),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=True,
                )
                if not completed.stdout or not isinstance(completed.stdout, str):
                    raise json.JSONDecodeError("FFprobe returned no JSON output", "", 0)
                payload = json.loads(completed.stdout)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                logger.warning(
                    "playback probe failed entity_id=%s media_file_id=%s error=%s",
                    entity_id,
                    media_file_id,
                    error,
                )
                continue
            streams = payload.get("streams") or []
            format_value = payload.get("format") or {}
            duration_seconds = float(format_value.get("duration") or 0)
            video = select_usable_video_stream(streams, duration_seconds) or {}
            audio = first_audio_stream(streams) or {}
            source_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"zenstream:{media_file_id}")
            )
            value = {
                "id": source_id,
                "container": format_value.get("format_name"),
                "durationSeconds": duration_seconds,
                "bitrate": int(float(format_value.get("bit_rate") or 0)),
                "width": video.get("width"),
                "height": video.get("height"),
                "videoCodec": video.get("codec_name"),
                "audioCodec": audio.get("codec_name"),
                "streams": streams,
            }
            self.db.execute(
                "INSERT INTO media_sources(id,entity_id,media_file_id,container,duration_seconds,bitrate,width,height,video_codec,audio_codec,probe_payload,probed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_id,media_file_id) DO UPDATE SET container=excluded.container,duration_seconds=excluded.duration_seconds,bitrate=excluded.bitrate,width=excluded.width,height=excluded.height,video_codec=excluded.video_codec,audio_codec=excluded.audio_codec,probe_payload=excluded.probe_payload,probed_at=excluded.probed_at",
                (
                    source_id,
                    entity_id,
                    media_file_id,
                    value["container"],
                    value["durationSeconds"],
                    value["bitrate"],
                    value["width"],
                    value["height"],
                    value["videoCodec"],
                    value["audioCodec"],
                    json.dumps(payload),
                    _iso(),
                ),
            )
            if has_track_index:
                track_index_statements.append(
                    (
                        "DELETE FROM media_track_languages WHERE media_file_id=?",
                        (media_file_id,),
                    )
                )
                indexed_languages = set()
                for stream in streams:
                    if not isinstance(stream, dict):
                        continue
                    track_type = {
                        "audio": "audio",
                        "subtitle": "subtitle",
                    }.get(str(stream.get("codec_type") or "").lower())
                    if track_type is None:
                        continue
                    tags = stream.get("tags")
                    tags = tags if isinstance(tags, dict) else {}
                    language = normalize_track_language(
                        tags.get("language") or tags.get("LANGUAGE")
                    )
                    if language:
                        indexed_languages.add((track_type, language))
                track_index_statements.extend(
                    (
                        "INSERT OR IGNORE INTO media_track_languages(media_file_id,track_type,language) VALUES(?,?,?)",
                        (media_file_id, track_type, language),
                    )
                    for track_type, language in sorted(indexed_languages)
                )
            values.append(value)
        if track_index_statements:
            self.db.write_many(track_index_statements)
        return values

    def sources(self, user_id: str, entity_id: str) -> list[dict]:
        self.catalog.require_entity(user_id, entity_id)
        rows = self.db.execute(
            "SELECT id,media_file_id,container,duration_seconds,bitrate,width,height,video_codec,audio_codec,probe_payload FROM media_sources WHERE entity_id=? ORDER BY id",
            (entity_id,),
        )
        if not rows:
            self.probe_entity(entity_id)
            rows = self.db.execute(
                "SELECT id,media_file_id,container,duration_seconds,bitrate,width,height,video_codec,audio_codec,probe_payload FROM media_sources WHERE entity_id=? ORDER BY id",
                (entity_id,),
            )

        def normalize_stream(stream: dict) -> dict:
            value = dict(stream)
            tags = dict(value.get("tags") or {})
            raw_language = str(
                tags.get("language") or tags.get("LANGUAGE") or ""
            ).strip()
            language = normalize_track_language(raw_language)
            if language:
                tags["language"] = language
            if str(value.get("codec_type") or "").lower() == "subtitle":
                current_title = str(tags.get("title") or "").strip().lower()
                if not current_title or current_title in {"subtitle", "subtitles"}:
                    tags["title"] = language_name(language, "subtitle")
            value["tags"] = tags
            return value

        file_rows = self.db.execute(
            "SELECT id,relative_path,language,role FROM media_files WHERE entity_id=? AND role IN ('media','subtitle','lyrics') ORDER BY relative_path COLLATE NOCASE",
            (entity_id,),
        )
        media_paths_by_id = {row[0]: row[1] for row in file_rows if row[3] == "media"}
        media_paths = list(media_paths_by_id.values())
        sidecar_rows = [row for row in file_rows if row[3] in {"subtitle", "lyrics"}]

        def sidecars_for_media(media_path: str) -> list[dict]:
            return [
                {
                    "index": 1000 + index,
                    "codec_type": "subtitle",
                    "codec_name": Path(relative_path).suffix.lstrip("."),
                    "fileId": file_id,
                    "kind": role,
                    "tags": {
                        **({"language": language} if language else {}),
                        "title": sidecar_display_title(
                            relative_path, language, role, [media_path]
                        ),
                    },
                }
                for index, (file_id, relative_path, language, role) in enumerate(
                    row
                    for row in sidecar_rows
                    if self._sidecar_matches_media(row[1], media_path, media_paths)
                )
            ]

        return [
            {
                "id": row[0],
                "mediaFileId": row[1],
                "container": row[2],
                "durationSeconds": row[3],
                "bitrate": row[4],
                "width": row[5],
                "height": row[6],
                "videoCodec": row[7],
                "audioCodec": row[8],
                "streams": [
                    normalize_stream(stream)
                    for stream in (json.loads(row[9]).get("streams") or [])
                ]
                + sidecars_for_media(media_paths_by_id.get(row[1], "")),
            }
            for row in rows
        ]

    @staticmethod
    def _sidecar_matches_media(
        sidecar_path: str, media_path: str, media_paths: list[str]
    ) -> bool:
        matching_media = sidecar_media_path(sidecar_path, media_paths)
        return matching_media == Path(media_path)

    def source_metadata(self, user_id: str, entity_id: str) -> dict:
        sources = self.sources(user_id, entity_id)
        if not sources:
            raise HTTPException(
                409,
                detail={
                    "code": "MEDIA_NOT_READY",
                    "message": "Media has not been probed or is unavailable.",
                },
                headers={"Retry-After": "2"},
            )
        source = sources[0]
        return {
            "id": source["id"],
            "streams": source.get("streams") or [],
        }

    def refresh_access(
        self,
        user_id: str,
        entity_id: str,
        source_id: str | None,
        session_id: str | None,
        auth_session_id: str | None,
    ) -> dict:
        """Issue a new media ticket without creating another viewer/session."""
        if not auth_session_id:
            raise HTTPException(401, "Authentication required.")
        self.catalog.require_entity(user_id, entity_id)
        if not source_id:
            raise HTTPException(400, "A playback source is required.")
        if session_id:
            rows = self.db.execute(
                "SELECT source_id,state FROM playback_sessions WHERE id=? AND user_id=? AND entity_id=? AND expires_at>?",
                (session_id, user_id, entity_id, _iso()),
            )
            if not rows or rows[0][0] != source_id:
                raise HTTPException(404, "Playback session not found.")
            if rows[0][1] in {"stopping", "failed", "expired"}:
                raise HTTPException(409, "Playback session is no longer active.")
        else:
            rows = self.db.execute(
                "SELECT 1 FROM media_sources WHERE id=? AND entity_id=?",
                (source_id, entity_id),
            )
            if not rows:
                raise HTTPException(404, "Media source not found.")
        return {
            "ticket": issue_ticket(
                user_id,
                "resource",
                PLAYBACK_RESOURCE_TICKET_TTL_SECONDS,
                entity=entity_id,
                sessionId=auth_session_id,
            ),
            "expiresIn": PLAYBACK_RESOURCE_TICKET_TTL_SECONDS,
        }

    @staticmethod
    def _profile_values(profile: dict, key: str, defaults: set[str]) -> set[str]:
        if key not in profile or profile[key] is None:
            return defaults
        value = profile[key]
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {str(item).strip().lower() for item in value if str(item).strip()}

    @staticmethod
    def _codec_values(value: str | None) -> set[str]:
        aliases = {
            "avc": "h264",
            "avc1": "h264",
            "h265": "hevc",
            "x265": "hevc",
            "ac-3": "ac3",
            "e-ac-3": "eac3",
            "mp4a": "aac",
        }
        return {
            aliases.get(part.strip().lower(), part.strip().lower())
            for part in str(value or "").split(",")
            if part.strip()
        }

    @staticmethod
    def _container_values(value: str | None) -> set[str]:
        aliases = {"matroska": "mkv", "mpegts": "ts", "quicktime": "mov"}
        return {
            aliases.get(part.strip().lower(), part.strip().lower())
            for part in str(value or "").split(",")
            if part.strip()
        }

    @classmethod
    def _stream_for_profile(cls, source: dict, profile: dict) -> dict:
        requested = profile.get("audioStreamId")
        streams = [
            stream
            for stream in source.get("streams", [])
            if str(stream.get("codec_type") or "").lower() == "audio"
        ]
        if requested is None:
            return streams[0] if streams else {}
        try:
            requested_index = int(requested)
        except (TypeError, ValueError):
            return {}
        for stream in streams:
            try:
                if int(stream.get("index", -1)) == requested_index:
                    return stream
            except (TypeError, ValueError):
                continue
        return {}

    @staticmethod
    def _source_has_video(source: dict) -> bool:
        return bool(
            any(
                str(stream.get("codec_type") or "").lower() == "video"
                for stream in source.get("streams", [])
            )
            or source.get("width")
            or source.get("height")
            or source.get("videoCodec")
        )

    @classmethod
    def _playback_mode(cls, source: dict, profile: dict) -> str:
        if profile.get("forceTranscoding") is True:
            return (
                "video-transcode"
                if cls._source_has_video(source)
                else "audio-transcode"
            )
        requested_mode = str(profile.get("requestedMode") or "").lower()
        if requested_mode == "video-transcode":
            return (
                requested_mode if cls._source_has_video(source) else "audio-transcode"
            )
        containers = cls._profile_values(profile, "containers", {"mp4", "webm"})
        video = {
            codec
            for value in cls._profile_values(
                profile, "videoCodecs", {"h264", "vp9", "av1"}
            )
            for codec in cls._codec_values(value)
        }
        audio = {
            codec
            for value in cls._profile_values(
                profile, "audioCodecs", {"aac", "opus", "vorbis"}
            )
            for codec in cls._codec_values(value)
        }
        source_container = cls._container_values(source.get("container"))
        container_ok = bool(source_container & containers)
        video_streams = [
            stream
            for stream in source.get("streams", [])
            if str(stream.get("codec_type") or "").lower() == "video"
        ]
        has_video = bool(video_streams) or cls._source_has_video(source)
        video_codec = next(iter(cls._codec_values(source.get("videoCodec"))), "")
        audio_stream = cls._stream_for_profile(source, profile)
        audio_codec = next(
            iter(
                cls._codec_values(
                    audio_stream.get("codec_name") or source.get("audioCodec")
                )
            ),
            "",
        )
        video_ok = not has_video or video_codec in video
        for limit_key, source_key in (("maxWidth", "width"), ("maxHeight", "height")):
            try:
                limit = (
                    int(profile.get(limit_key))
                    if profile.get(limit_key) is not None
                    else None
                )
                dimension = (
                    int(source.get(source_key)) if source.get(source_key) else None
                )
            except (TypeError, ValueError):
                limit = dimension = None
            if limit is not None and dimension is not None and dimension > limit:
                video_ok = False
        audio_ok = not audio_codec or audio_codec in audio
        try:
            maximum_channels = int(profile.get("maxAudioChannels") or 2)
        except (TypeError, ValueError):
            maximum_channels = 2
        if int(audio_stream.get("channels") or 0) > maximum_channels:
            audio_ok = False
        maximum_bitrate = profile.get("maxStreamingBitrate")
        bitrate_ok = (
            not maximum_bitrate
            or not source.get("bitrate")
            or int(source["bitrate"]) <= int(maximum_bitrate)
        )
        if video_ok and audio_ok and container_ok and bitrate_ok:
            return "direct"
        if video_ok and audio_ok and bitrate_ok:
            return "remux"
        if video_ok and not audio_ok and bitrate_ok:
            return "audio-transcode"
        return "video-transcode"

    @classmethod
    def _direct(cls, source: dict, profile: dict) -> bool:
        return cls._playback_mode(source, profile) == "direct"

    @classmethod
    def _transcode_mode(cls, source: dict, profile: dict) -> str:
        mode = cls._playback_mode(source, profile)
        return "video-transcode" if mode == "direct" else mode

    def _register_viewer(
        self,
        user_id: str,
        entity_id: str,
        source: dict,
        mode: str,
        profile: dict,
        auth_session_id: str | None,
        worker_session_id: str | None,
        start_time: float,
        duration_seconds: float,
        ip_address: str | None,
    ) -> str | None:
        database = getattr(self, "db", None)
        if database is None:
            return None
        store = PlaybackViewerStore(database)
        if not store.available():
            return None
        viewer_profile = {
            **profile,
            "startPositionSeconds": start_time,
            "durationSeconds": duration_seconds,
        }
        return store.create_viewer(
            user_id,
            auth_session_id,
            entity_id,
            source["id"],
            mode,
            viewer_profile,
            worker_session_id,
            ip_address,
        )

    def negotiate(
        self,
        user_id: str,
        entity_id: str,
        profile: dict,
        auth_session_id: str | None = None,
        ip_address: str | None = None,
    ) -> dict:
        forbidden = {
            "EnableTranscoding",
            "MediaSourceId",
            "AudioStreamIndex",
            "StartTimeTicks",
            "StartTimeSeconds",
            "PlaySessionId",
        }
        if forbidden.intersection(profile):
            raise HTTPException(
                400,
                detail={
                    "code": "LEGACY_PLAYBACK_CONTRACT",
                    "message": "Use the canonical playback contract.",
                },
            )
        sources = self.sources(user_id, entity_id)
        if not sources:
            raise HTTPException(
                409,
                detail={
                    "code": "MEDIA_NOT_READY",
                    "message": "Media has not been probed or is unavailable.",
                },
                headers={"Retry-After": "2"},
            )
        requested_source_id = profile.get("sourceId")
        if requested_source_id:
            source = next(
                (value for value in sources if value["id"] == requested_source_id),
                None,
            )
            if source is None:
                raise HTTPException(404, "Media source not found.")
        else:
            source = sources[0]
        if profile.get("audioStreamId") is not None and not self._stream_for_profile(
            source, profile
        ):
            raise HTTPException(
                400,
                detail={
                    "code": "INVALID_AUDIO_TRACK",
                    "message": "The selected audio track is unavailable.",
                },
            )
        direct_only = profile.get("directPlayOnly") is True
        logger.info(
            "playback negotiation entity_id=%s source_id=%s engine=%s force_transcoding=%s direct_play_only=%s",
            entity_id,
            source["id"],
            profile.get("engine"),
            profile.get("forceTranscoding") is True,
            direct_only,
        )
        selected_mode = self._playback_mode(source, profile)
        selected_audio = self._stream_for_profile(source, profile)
        logger.info(
            "playback decision entity_id=%s source_id=%s mode=%s container=%s video_codec=%s audio_codec=%s channels=%s bitrate=%s max_bitrate=%s",
            entity_id,
            source["id"],
            selected_mode,
            source.get("container"),
            source.get("videoCodec"),
            selected_audio.get("codec_name") or source.get("audioCodec"),
            selected_audio.get("channels"),
            source.get("bitrate"),
            profile.get("maxStreamingBitrate"),
        )
        ticket_claims = {"entity": entity_id}
        if auth_session_id:
            ticket_claims["sessionId"] = auth_session_id
        access = issue_ticket(
            user_id,
            "resource",
            PLAYBACK_RESOURCE_TICKET_TTL_SECONDS,
            **ticket_claims,
        )
        start_time = max(0.0, float(profile.get("startPositionSeconds") or 0.0))
        duration_seconds = max(0.0, float(source.get("durationSeconds") or 0.0))
        if duration_seconds > 0:
            start_time = min(start_time, duration_seconds)
        if selected_mode == "direct":
            viewer_id = self._register_viewer(
                user_id,
                entity_id,
                source,
                selected_mode,
                profile,
                auth_session_id,
                None,
                start_time,
                duration_seconds,
                ip_address,
            )
            result = {
                "mode": "direct",
                "sessionState": "ready",
                "source": source,
                "sourceId": source["id"],
                "audioStreamId": profile.get("audioStreamId"),
                "url": f"/api/playback/items/{entity_id}/stream?sourceId={source['id']}&access={access}",
                "mimeType": self._mime(source),
                "startPositionSeconds": start_time,
                "durationSeconds": source.get("durationSeconds"),
            }
            if viewer_id:
                result["viewerSessionId"] = viewer_id
            return result
        if direct_only:
            raise HTTPException(
                409,
                detail={
                    "code": "DIRECT_PLAY_UNAVAILABLE",
                    "message": "The selected media cannot be played directly by this client.",
                },
            )
        transcode_mode = selected_mode
        result = self._transcode(
            user_id, entity_id, source, access, profile, start_time, transcode_mode
        )
        result["sessionState"] = result.get("sessionState", "starting")
        result["sourceId"] = source["id"]
        result["audioStreamId"] = profile.get("audioStreamId")
        result["startPositionSeconds"] = start_time
        result["durationSeconds"] = source.get("durationSeconds")
        viewer_id = self._register_viewer(
            user_id,
            entity_id,
            source,
            selected_mode,
            profile,
            auth_session_id,
            result.get("sessionId"),
            start_time,
            duration_seconds,
            ip_address,
        )
        if viewer_id:
            result["viewerSessionId"] = viewer_id
        return result

    @staticmethod
    def _mime(source: dict) -> str:
        container = str(source.get("container") or "").split(",", 1)[0].lower()
        has_video = PlaybackManager._source_has_video(source)
        if has_video:
            return {
                "matroska": "video/x-matroska",
                "mkv": "video/x-matroska",
                "webm": "video/webm",
                "mov": "video/mp4",
                "mp4": "video/mp4",
            }.get(container, "application/octet-stream")
        return {
            "mp3": "audio/mpeg",
            "flac": "audio/flac",
            "ogg": "audio/ogg",
            "oga": "audio/ogg",
            "opus": "audio/ogg",
            "wav": "audio/wav",
            "aac": "audio/aac",
            "adts": "audio/aac",
            "m4a": "audio/mp4",
            "mp4": "audio/mp4",
            "mov": "audio/mp4",
            "aiff": "audio/aiff",
            "aif": "audio/aiff",
            "wma": "audio/x-ms-wma",
            "ape": "audio/x-ape",
            "wv": "audio/wavpack",
            "webm": "audio/webm",
            "matroska": "audio/x-matroska",
            "mkv": "audio/x-matroska",
        }.get(container, "application/octet-stream")

    def _transcode(
        self,
        user_id: str,
        entity_id: str,
        source: dict,
        access: str,
        profile: dict,
        start_time: float = 0.0,
        transcode_mode: str = "video-transcode",
    ) -> dict:
        executable = ffmpeg_path()
        if not executable:
            raise HTTPException(
                503,
                detail={
                    "code": "FFMPEG_UNAVAILABLE",
                    "message": "FFmpeg is not available.",
                },
            )
        session_key = self._transcode_key(user_id, entity_id, source, profile)
        reused_session: tuple[str, Path, subprocess.Popen | None] | None = None
        with self._lock:
            base_key = session_key[:3]
            for existing_id, existing_key in list(self._session_keys.items()):
                process = self._processes.get(existing_id)
                if existing_key == session_key and existing_key[:3] == base_key:
                    session_rows = self.db.execute(
                        "SELECT output_directory,state FROM playback_sessions WHERE id=? AND user_id=?",
                        (existing_id, user_id),
                    )
                    if not session_rows or session_rows[0][1] not in {
                        "starting",
                        "ready",
                        "completed",
                    }:
                        logger.debug(
                            "not reusing playback session_id=%s reason=database_state state=%s",
                            existing_id,
                            session_rows[0][1] if session_rows else "missing",
                        )
                        continue
                    output = Path(session_rows[0][0])
                    if process is None and existing_id not in self._session_specs:
                        continue
                    playlist_ready = self._startup_ready(output)
                    if not playlist_ready and (
                        process is None or process.poll() is not None
                    ):
                        continue
                    logger.info(
                        "reusing playback session user_id=%s entity_id=%s session_id=%s state=%s playlist_ready=%s",
                        user_id,
                        entity_id,
                        existing_id,
                        session_rows[0][1],
                        playlist_ready,
                    )
                    reused_session = (existing_id, output, process)
                    break
            if reused_session is not None:
                existing_id, _, _ = reused_session
                result = self._hls_result(existing_id, source, access, transcode_mode)
                result["startPositionSeconds"] = start_time
                result["actualStartPositionSeconds"] = 0.0
                result["sessionState"] = (
                    "ready" if self._startup_ready(reused_session[1]) else "starting"
                )
                result["sourceId"] = source["id"]
                result["audioStreamId"] = profile.get("audioStreamId")
                logger.info(
                    "reused playback session is ready session_id=%s requested_start=%.3f actual_start=0.000",
                    existing_id,
                    start_time,
                )
                return result
            with self._lock:
                active = [
                    process
                    for process in self._processes.values()
                    if process.poll() is None
                ]
                per_user = []
                for session_id in list(self._users.get(user_id, set())):
                    process = self._processes.get(session_id)
                    if process is not None and process.poll() is None:
                        per_user.append(process)
                    else:
                        self._remove_active_user_index_locked(session_id, user_id)
            global_limit, user_limit = self._limits()
            global_limit_reached = global_limit > 0 and len(active) >= global_limit
            user_limit_reached = user_limit > 0 and len(per_user) >= user_limit
            if global_limit_reached or user_limit_reached:
                logger.warning(
                    "transcode limit reached user_id=%s active=%s per_user=%s global_limit=%s user_limit=%s",
                    user_id,
                    len(active),
                    len(per_user),
                    global_limit,
                    user_limit,
                )
                raise HTTPException(
                    429,
                    detail={
                        "code": "TRANSCODE_LIMIT_REACHED",
                        "message": "Transcoding capacity is currently full.",
                    },
                    headers={"Retry-After": "5"},
                )

            session_id = str(uuid.uuid4())
            output = Path(tempfile.gettempdir()) / "zenstream-transcodes" / session_id
            output.mkdir(parents=True, exist_ok=False)
            media_file_id, path = self._file_path(entity_id, source.get("mediaFileId"))
            audio_index = profile.get("audioStreamId")
            if audio_index is not None:
                try:
                    audio_index = int(audio_index)
                except (TypeError, ValueError):
                    raise HTTPException(
                        400,
                        detail={
                            "code": "INVALID_AUDIO_TRACK",
                            "message": "The selected audio track is invalid.",
                        },
                    )
            selected_audio = self._stream_for_profile(source, profile)
            if audio_index is not None and not selected_audio:
                shutil.rmtree(output, ignore_errors=True)
                raise HTTPException(
                    400,
                    detail={
                        "code": "INVALID_AUDIO_TRACK",
                        "message": "The selected audio track is unavailable.",
                    },
                )
            generation = 1
            self._seek_generations[base_key] = generation
            duration = max(0.1, float(source.get("durationSeconds") or 0.0))
            start_index = int(start_time // self._segment_seconds)
            actual_start = start_index * self._segment_seconds
            self._write_public_playlist(output, duration)
            self.db.execute(
                "INSERT INTO playback_sessions(id,user_id,entity_id,source_id,mode,state,output_directory,created_at,expires_at,process_id,requested_start_seconds,actual_start_seconds,audio_stream_id,last_accessed_at,seek_generation) VALUES(?,?,?,?,?,'starting',?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    user_id,
                    entity_id,
                    source["id"],
                    transcode_mode,
                    str(output),
                    _iso(),
                    _iso(datetime.now(timezone.utc) + timedelta(hours=6)),
                    None,
                    start_time,
                    actual_start,
                    str(profile.get("audioStreamId"))
                    if profile.get("audioStreamId") is not None
                    else None,
                    _iso(),
                    generation,
                ),
            )
            self._session_keys[session_id] = session_key
            self._session_locks[session_id] = threading.RLock()
            self._session_specs[session_id] = {
                "user_id": user_id,
                "entity_id": entity_id,
                "source": source,
                "profile": profile,
                "mode": transcode_mode,
                "executable": executable,
                "path": path,
                "output": output,
                "generation": generation,
            }
            try:
                self._start_worker_locked(session_id, start_index)
            except Exception:
                self._remove_session_indexes_locked(session_id)
                shutil.rmtree(output, ignore_errors=True)
                raise
        result = self._hls_result(session_id, source, access, transcode_mode)
        result["startPositionSeconds"] = start_time
        result["sessionState"] = "ready" if self._startup_ready(output) else "starting"
        result["sourceId"] = source["id"]
        result["audioStreamId"] = profile.get("audioStreamId")
        result["durationSeconds"] = source.get("durationSeconds")
        result["actualStartPositionSeconds"] = actual_start
        return result

    @classmethod
    def _session_lock(cls, session_id: str) -> threading.RLock:
        with cls._lock:
            return cls._session_locks.setdefault(session_id, threading.RLock())

    @classmethod
    def _write_public_playlist(cls, output: Path, duration: float) -> None:
        segment_count = max(
            1, int((duration + cls._segment_seconds - 0.0001) // cls._segment_seconds)
        )
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:4",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        for index in range(segment_count):
            segment_duration = min(
                cls._segment_seconds, max(0.1, duration - index * cls._segment_seconds)
            )
            lines.extend(
                [f"#EXTINF:{segment_duration:.3f},", f"segment-{index:06d}.ts"]
            )
        lines.append("#EXT-X-ENDLIST")
        temporary = output / "master.m3u8.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, output / "master.m3u8")

    def _build_ffmpeg_command(
        self,
        spec: dict,
        worker_dir: Path,
        start_index: int,
    ) -> list[str]:
        source = spec["source"]
        profile = spec["profile"]
        mode = spec["mode"]
        start_time = start_index * self._segment_seconds
        audio_index = profile.get("audioStreamId")
        audio_map = (
            f"0:{int(audio_index)}"
            if audio_index is not None and int(audio_index) >= 0
            else "0:a:0?"
        )
        selected_audio = self._stream_for_profile(source, profile)
        selected_audio_codec = str(
            selected_audio.get("codec_name") or source.get("audioCodec") or ""
        ).lower()
        try:
            selected_audio_channels = int(selected_audio.get("channels") or 2)
        except (TypeError, ValueError):
            selected_audio_channels = 2
        copy_audio = mode == "remux" or (
            mode == "video-transcode"
            and selected_audio_codec == "aac"
            and selected_audio_channels <= 2
        )
        has_video = self._source_has_video(source)
        command = [
            spec["executable"],
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_time:.3f}",
            "-noaccurate_seek"
            if mode in {"remux", "audio-transcode"}
            else "-accurate_seek",
            "-i",
            str(spec["path"]),
        ]
        if has_video:
            command.extend(
                [
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy" if mode in {"remux", "audio-transcode"} else "libx264",
                ]
            )
        else:
            command.append("-vn")
        command.extend(["-map", audio_map, "-c:a", "copy" if copy_audio else "aac"])
        if not copy_audio:
            command.extend(["-ac", "2", "-ar", "48000", "-b:a", "192k"])
        maximum_bitrate = profile.get("maxStreamingBitrate")
        if maximum_bitrate and mode == "video-transcode":
            command.extend(
                [
                    "-maxrate",
                    str(int(maximum_bitrate)),
                    "-bufsize",
                    str(int(maximum_bitrate) * 2),
                ]
            )
        if mode == "video-transcode" and has_video:
            command.extend(
                [
                    "-pix_fmt",
                    "yuv420p",
                    "-profile:v",
                    "main",
                    "-preset",
                    "veryfast",
                    "-force_key_frames",
                    "expr:gte(t,n_forced*4)",
                    "-sc_threshold",
                    "0",
                    "-g",
                    "96",
                    "-keyint_min",
                    "96",
                ]
            )
        output_options = [
            "-copyts",
            "-avoid_negative_ts",
            "disabled",
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_playlist_type",
            "event",
            "-hls_list_size",
            "0",
            "-start_number",
            str(start_index),
            "-hls_segment_filename",
            str(worker_dir / "segment-%06d.ts"),
            str(worker_dir / "worker.m3u8"),
        ]
        command.extend(output_options)
        return command

    def _monitor_startup(
        self,
        session_id: str,
        output: Path,
        process: subprocess.Popen,
        generation: int,
    ) -> None:
        deadline = time.monotonic() + self._startup_timeout_seconds
        last_snapshot = None
        while time.monotonic() < deadline:
            with self._lock:
                worker = self._session_workers.get(session_id)
                current = bool(
                    worker
                    and worker.get("process") is process
                    and worker.get("generation") == generation
                )
            if not current:
                return
            self._publish_worker_segments(session_id)
            snapshot = self._playlist_snapshot(output)
            if snapshot != last_snapshot:
                logger.debug(
                    "playback readiness session_id=%s generation=%s snapshot=%s",
                    session_id,
                    generation,
                    snapshot,
                )
                last_snapshot = snapshot
            if snapshot["playlistReady"] and snapshot["segmentCount"] > 0:
                self.db.execute(
                    "UPDATE playback_sessions SET state='ready',started_at=? WHERE id=? AND state='starting' AND seek_generation=?",
                    (_iso(), session_id, generation),
                )
                logger.info(
                    "playback ready session_id=%s generation=%s snapshot=%s",
                    session_id,
                    generation,
                    snapshot,
                )
                return
            if process.poll() is not None:
                return
            time.sleep(0.05)
        with self._lock:
            worker = self._session_workers.get(session_id)
            current = bool(
                worker
                and worker.get("process") is process
                and worker.get("generation") == generation
            )
        if not current or process.poll() is not None:
            return
        logger.warning(
            "playback startup timeout session_id=%s generation=%s snapshot=%s",
            session_id,
            generation,
            self._playlist_snapshot(output),
        )
        self._stop_process(
            process, f"startup_timeout session_id={session_id} generation={generation}"
        )
        self.db.execute(
            "UPDATE playback_sessions SET state='failed',completed_at=?,failure_code=?,failure_detail=? WHERE id=? AND state='starting' AND seek_generation=?",
            (
                _iso(),
                "TRANSCODE_START_TIMEOUT",
                json.dumps(
                    {
                        "stage": "startup",
                        "returnCode": None,
                        **self._playlist_snapshot(output),
                    },
                    sort_keys=True,
                ),
                session_id,
                generation,
            ),
        )

    def _monitor_idle(
        self,
        user_id: str,
        session_id: str,
        process: subprocess.Popen,
        generation: int,
    ) -> None:
        timeout = self._idle_timeout_seconds()
        interval = min(5.0, max(1.0, timeout / 5.0))
        while process.poll() is None:
            time.sleep(interval)
            with self._lock:
                worker = self._session_workers.get(session_id)
                current = bool(
                    worker
                    and worker.get("process") is process
                    and worker.get("generation") == generation
                )
            if not current:
                return
            rows = self.db.execute(
                "SELECT state,last_accessed_at,process_id FROM playback_sessions WHERE id=? AND user_id=?",
                (session_id, user_id),
            )
            if not rows or rows[0][0] in {"stopping", "failed", "completed", "expired"}:
                return
            try:
                last_accessed = datetime.fromisoformat(rows[0][1]).timestamp()
            except (TypeError, ValueError, OSError):
                last_accessed = time.time()
            idle_for = time.time() - last_accessed
            if idle_for < timeout:
                continue
            with self._lock:
                spec = self._session_specs.get(session_id)
            if not spec:
                return
            snapshot = self._playlist_snapshot(spec["output"])
            logger.warning(
                "playback idle timeout session_id=%s generation=%s idle_seconds=%.1f snapshot=%s",
                session_id,
                generation,
                idle_for,
                snapshot,
            )
            self.db.execute(
                "UPDATE playback_sessions SET state='stopping',failure_code=?,failure_detail=?,completed_at=? WHERE id=? AND user_id=? AND seek_generation=? AND state NOT IN ('stopping','failed','completed','expired')",
                (
                    "PLAYBACK_IDLE_TIMEOUT",
                    json.dumps(
                        {
                            "stage": "idle_watchdog",
                            "idleSeconds": round(idle_for, 3),
                            "timeoutSeconds": timeout,
                            **snapshot,
                        },
                        sort_keys=True,
                    ),
                    _iso(),
                    session_id,
                    user_id,
                    generation,
                ),
            )
            self._stop_process(process, f"idle_timeout session_id={session_id}")
            if process is None:
                self._stop_process_id(
                    rows[0][2], f"idle_timeout session_id={session_id}"
                )
            return

    def _start_worker_locked(
        self, session_id: str, start_index: int
    ) -> subprocess.Popen:
        spec = self._session_specs[session_id]
        old = self._processes.get(session_id)
        if old is not None and old.poll() is None:
            self._stop_process(old, f"segment_seek session_id={session_id}")
        generation = int(spec.get("generation") or 0) + 1
        spec["generation"] = generation
        worker_dir = spec["output"] / f"worker-{generation:06d}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        command = self._build_ffmpeg_command(spec, worker_dir, start_index)
        logger.info(
            "starting playback worker session_id=%s generation=%s start_index=%s source_start=%.3f output_directory=%s",
            session_id,
            generation,
            start_index,
            start_index * self._segment_seconds,
            spec["output"],
        )
        logger.info(
            "ffmpeg command session_id=%s generation=%s command=%s",
            session_id,
            generation,
            command,
        )
        process: subprocess.Popen | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.db.execute(
                "UPDATE playback_sessions SET process_id=?,state='starting',requested_start_seconds=?,actual_start_seconds=?,seek_generation=?,failure_code=NULL,failure_detail=NULL WHERE id=?",
                (
                    process.pid,
                    start_index * self._segment_seconds,
                    start_index * self._segment_seconds,
                    generation,
                    session_id,
                ),
            )
        except Exception as error:
            if process is not None:
                self._stop_process(
                    process, f"worker_registration_failed session_id={session_id}"
                )
            logger.exception(
                "could not register playback worker session_id=%s generation=%s error=%s",
                session_id,
                generation,
                error,
            )
            try:
                self.db.execute(
                    "UPDATE playback_sessions SET state='failed',completed_at=?,failure_code='PLAYBACK_WORKER_REGISTRATION_FAILED',failure_detail=? WHERE id=? AND seek_generation=?",
                    (_iso(), str(error)[-1000:], session_id, generation),
                )
            except Exception:
                logger.exception(
                    "could not record worker registration failure session_id=%s",
                    session_id,
                )
            raise HTTPException(
                503,
                detail={
                    "code": "PLAYBACK_WORKER_REGISTRATION_FAILED",
                    "message": "The playback worker could not be registered safely.",
                },
            ) from error
        worker = {
            "process": process,
            "generation": generation,
            "start_index": start_index,
            "worker_dir": worker_dir,
        }
        self._session_workers[session_id] = worker
        self._processes[session_id] = process
        self._users.setdefault(spec["user_id"], set()).add(session_id)
        threading.Thread(
            target=self._watch,
            args=(spec["user_id"], session_id, process, generation),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._monitor_startup,
            args=(session_id, spec["output"], process, generation),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._monitor_idle,
            args=(spec["user_id"], session_id, process, generation),
            daemon=True,
        ).start()
        return process

    @staticmethod
    def _transcode_key(
        user_id: str,
        entity_id: str,
        source: dict,
        profile: dict,
        start_time: float = 0.0,
    ) -> tuple[str, str, str, str]:
        settings = {
            "audioStreamId": profile.get("audioStreamId"),
            "maxStreamingBitrate": profile.get("maxStreamingBitrate"),
            "transcodeMode": PlaybackManager._playback_mode(source, profile),
        }
        return (
            user_id,
            entity_id,
            str(source["id"]),
            json.dumps(settings, sort_keys=True),
        )

    @staticmethod
    def _hls_result(session_id: str, source: dict, access: str, mode: str) -> dict:
        return {
            "mode": mode,
            "source": source,
            "sourceId": source["id"],
            "sessionId": session_id,
            "url": f"/api/playback/sessions/{session_id}/master.m3u8?access={access}",
            "mimeType": "application/vnd.apple.mpegurl",
        }

    @staticmethod
    def _playlist_snapshot(output: Path) -> dict:
        playlist = output / "master.m3u8"
        segments = sorted(
            (
                path
                for path in output.glob("segment-*.ts")
                if re.fullmatch(r"segment-\d+\.ts", path.name)
            ),
            key=lambda path: int(re.search(r"segment-(\d+)\.ts$", path.name).group(1)),
        )
        if not playlist.is_file():
            playlist_state = "missing"
        elif playlist.stat().st_size == 0:
            playlist_state = "empty"
        elif "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8", errors="replace"):
            playlist_state = "endlist"
        else:
            playlist_state = "progressive"
        indices = [
            int(match.group(1))
            for path in segments
            if (match := re.search(r"segment-(\d+)\.ts$", path.name))
        ]
        return {
            "playlistReady": playlist.is_file() and playlist.stat().st_size > 0,
            "segmentCount": len(segments),
            "availableSegmentStart": min(indices) if indices else None,
            "availableSegmentEnd": max(indices) if indices else None,
            "firstSegmentDurationSeconds": next(
                (
                    float(line.split(":", 1)[1].rstrip(","))
                    for line in playlist.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if line.startswith("#EXTINF:") and ":" in line
                ),
                None,
            )
            if playlist.is_file()
            else None,
            "playlistState": playlist_state,
        }

    @staticmethod
    def _segment_index(filename: str) -> int | None:
        match = re.fullmatch(r"segment-(\d{1,9})\.ts", filename)
        return int(match.group(1)) if match else None

    @staticmethod
    def _stable_file(path: Path) -> bool:
        try:
            first = path.stat().st_size
            if first <= 0:
                return False
            time.sleep(0.03)
            return path.is_file() and path.stat().st_size == first
        except OSError:
            return False

    def _publish_worker_segments(self, session_id: str) -> None:
        with self._lock:
            worker = self._session_workers.get(session_id)
            spec = self._session_specs.get(session_id)
        if not worker or not spec:
            return
        output = spec["output"]
        worker_dir = worker["worker_dir"]
        worker_playlist = worker_dir / "worker.m3u8"
        completed_names: set[str] = set()
        if worker_playlist.is_file():
            try:
                completed_names = {
                    line.strip()
                    for line in worker_playlist.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if re.fullmatch(r"segment-\d+\.ts", line.strip())
                }
            except OSError:
                completed_names = set()
        for candidate in worker_dir.glob("segment-*.ts"):
            if candidate.name not in completed_names:
                continue
            if not self._stable_file(candidate):
                continue
            destination = output / candidate.name
            try:
                if destination.exists():
                    candidate.unlink(missing_ok=True)
                else:
                    os.replace(candidate, destination)
                logger.debug(
                    "published playback segment session_id=%s generation=%s filename=%s",
                    session_id,
                    worker["generation"],
                    destination.name,
                )
            except OSError as error:
                logger.debug(
                    "playback segment publish deferred session_id=%s filename=%s error=%s",
                    session_id,
                    candidate.name,
                    error,
                )

    def _ensure_segment(self, user_id: str, session_id: str, index: int) -> Path:
        lock = self._session_lock(session_id)
        deadline = time.monotonic() + self._segment_wait_timeout_seconds
        last_access_touch = 0.0
        with lock:
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now - last_access_touch >= 5.0:
                    self.db.execute(
                        "UPDATE playback_sessions SET last_accessed_at=? WHERE id=? AND user_id=?",
                        (_iso(), session_id, user_id),
                    )
                    last_access_touch = now
                with self._lock:
                    spec = self._session_specs.get(session_id)
                    worker = self._session_workers.get(session_id)
                if spec is None:
                    raise HTTPException(
                        503,
                        detail={
                            "sessionId": session_id,
                            "sessionState": "starting",
                            "errorCode": "PLAYBACK_WORKER_UNAVAILABLE",
                            "errorDetail": "The playback worker is not available in this process.",
                        },
                    )
                output = spec["output"]
                destination = output / f"segment-{index:06d}.ts"
                self._publish_worker_segments(session_id)
                if destination.is_file() and destination.stat().st_size > 0:
                    logger.info(
                        "playback segment ready session_id=%s index=%s generation=%s",
                        session_id,
                        index,
                        worker.get("generation") if worker else None,
                    )
                    return destination
                process = worker.get("process") if worker else None
                available_end = self._playlist_snapshot(output).get(
                    "availableSegmentEnd"
                )
                if (
                    process is not None
                    and process.poll() is not None
                    and process.returncode != 0
                ):
                    failure_row = self.db.execute(
                        "SELECT state,failure_code,failure_detail FROM playback_sessions WHERE id=?",
                        (session_id,),
                    )
                    failure_detail = (
                        "FFmpeg exited before the requested HLS segment was produced."
                    )
                    failure_code = "FFMPEG_FAILED"
                    state = "failed"
                    if failure_row:
                        state = failure_row[0][0]
                        failure_code = failure_row[0][1] or failure_code
                        failure_detail = failure_row[0][2] or failure_detail
                    raise HTTPException(
                        502,
                        detail={
                            "sessionId": session_id,
                            "sessionState": state,
                            "errorCode": failure_code,
                            "errorDetail": failure_detail,
                            **self._playlist_snapshot(output),
                        },
                    )
                needs_worker = (
                    process is None
                    or process.poll() is not None
                    or worker is None
                    or index < int(worker.get("start_index", 0))
                    or (
                        int(worker.get("start_index", 0)) < index
                        and available_end is not None
                        and index > int(available_end) + 2
                    )
                )
                if needs_worker:
                    with self._lock:
                        self._start_worker_locked(session_id, index)
                    worker = self._session_workers.get(session_id)
                    logger.info(
                        "playback segment requested session_id=%s index=%s generation=%s",
                        session_id,
                        index,
                        worker.get("generation") if worker else None,
                    )
                process = worker.get("process") if worker else None
                if process is not None and process.poll() is not None:
                    self._publish_worker_segments(session_id)
                    destination = output / f"segment-{index:06d}.ts"
                    if destination.is_file() and destination.stat().st_size > 0:
                        return destination
                    raise HTTPException(
                        502,
                        detail={
                            "sessionId": session_id,
                            "sessionState": "failed",
                            "errorCode": "FFMPEG_FAILED",
                            "errorDetail": "FFmpeg exited before the requested HLS segment was produced.",
                            **self._playlist_snapshot(output),
                        },
                    )
                time.sleep(0.1)
        with self._lock:
            spec = self._session_specs.get(session_id)
        snapshot = self._playlist_snapshot(spec["output"]) if spec else {}
        logger.warning(
            "playback segment timeout user_id=%s session_id=%s index=%s snapshot=%s",
            user_id,
            session_id,
            index,
            snapshot,
        )
        raise HTTPException(
            503,
            detail={
                "sessionId": session_id,
                "sessionState": "starting",
                "errorCode": "HLS_SEGMENT_TIMEOUT",
                "errorDetail": "The requested HLS segment was not produced before the bounded segment wait deadline.",
                **snapshot,
            },
            headers={"Retry-After": "1"},
        )

    @classmethod
    def _startup_ready(cls, output: Path) -> bool:
        snapshot = cls._playlist_snapshot(output)
        return bool(snapshot["playlistReady"] and snapshot["segmentCount"] > 0)

    @staticmethod
    def _finalize_playlist(session_id: str, output: Path) -> None:
        playlist = output / "master.m3u8"
        if not playlist.is_file():
            raise RuntimeError("FFmpeg completed without producing master.m3u8")
        lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = [
            line
            for line in lines
            if line
            not in {
                "#EXT-X-PLAYLIST-TYPE:EVENT",
                "#EXT-X-PLAYLIST-TYPE:VOD",
                "#EXT-X-ENDLIST",
            }
        ]
        version_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.startswith("#EXT-X-VERSION:")
            ),
            0,
        )
        lines.insert(version_index + 1, "#EXT-X-PLAYLIST-TYPE:VOD")
        lines.append("#EXT-X-ENDLIST")
        temporary = output / "master.m3u8.finalizing"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, playlist)
        logger.info(
            "finalized playback playlist session_id=%s playlist_state=vod segment_count=%s",
            session_id,
            len(list(output.glob("segment-*.ts"))),
        )

    def _watch(
        self,
        user_id: str,
        session_id: str,
        process: subprocess.Popen,
        generation: int,
    ) -> None:
        row = self.db.execute(
            "SELECT output_directory,state,failure_code FROM playback_sessions WHERE id=?",
            (session_id,),
        )
        output = Path(row[0][0]) if row else None
        stderr_lines: list[str] = []
        stderr = getattr(process, "stderr", None)
        error_file = output / "ffmpeg.stderr.log" if output else None
        target = error_file.open("a", encoding="utf-8") if error_file else None
        try:
            if stderr is not None:
                for line in stderr:
                    value = str(line).rstrip()
                    if not value:
                        continue
                    stderr_lines.append(value)
                    del stderr_lines[:-80]
                    if target:
                        target.write(value + "\n")
                    logger.debug(
                        "ffmpeg stderr session_id=%s detail=%s", session_id, value
                    )
        finally:
            if target:
                target.close()
        process.wait()
        return_code = process.returncode
        with self._lock:
            worker = self._session_workers.get(session_id)
            is_current = bool(
                worker
                and worker.get("process") is process
                and worker.get("generation") == generation
            )
            if is_current:
                self._processes.pop(session_id, None)
                self._remove_active_user_index_locked(session_id, user_id)
                key = self._session_keys.get(session_id)
                if key is not None and not any(
                    existing[:3] == key[:3]
                    for existing_id, existing in self._session_keys.items()
                    if existing_id != session_id
                ):
                    self._seek_generations.pop(key[:3], None)
        row = self.db.execute(
            "SELECT output_directory,state,failure_code FROM playback_sessions WHERE id=?",
            (session_id,),
        )
        if not is_current:
            logger.info(
                "ignored stale playback worker exit session_id=%s generation=%s return_code=%s",
                session_id,
                generation,
                return_code,
            )
            return
        was_cancelled = bool(row and row[0][1] == "stopping")
        output = Path(row[0][0]) if row else output
        self._publish_worker_segments(session_id)
        snapshot = self._playlist_snapshot(output) if output else {}
        existing_failure = row[0][2] if row else None
        state = (
            "stopping"
            if was_cancelled
            else ("completed" if return_code == 0 else "failed")
        )
        detail = None
        failure_code = None
        if state == "failed":
            failure_code = existing_failure or "FFMPEG_FAILED"
            detail = json.dumps(
                {
                    "stage": "ffmpeg",
                    "returnCode": return_code,
                    "generation": generation,
                    "stderrTail": "\n".join(stderr_lines)[-4000:],
                    **snapshot,
                },
                sort_keys=True,
            )
        if state == "failed":
            logger.error(
                "ffmpeg failed session_id=%s generation=%s return_code=%s stderr_tail=%s",
                session_id,
                generation,
                return_code,
                "\\n".join(stderr_lines)[-4000:] or "<empty>",
            )
        logger.info(
            "ffmpeg exited session_id=%s return_code=%s state=%s snapshot=%s stderr_tail=%s",
            session_id,
            return_code,
            state,
            snapshot,
            "present" if stderr_lines else "empty",
        )
        try:
            self.db.execute(
                "UPDATE playback_sessions SET state=?,completed_at=?,failure_code=?,failure_detail=?,process_id=NULL WHERE id=? AND seek_generation=?",
                (
                    state,
                    _iso(),
                    failure_code,
                    detail,
                    session_id,
                    generation,
                ),
            )
        finally:
            if state != "completed":
                with self._lock:
                    self._remove_session_indexes_locked(session_id)

    def _cleanup_expired(self) -> None:
        self.db.execute(
            "UPDATE playback_sessions SET process_id=NULL WHERE state IN ('failed','completed','expired') AND process_id IS NOT NULL"
        )
        rows = self.db.execute(
            "SELECT id,output_directory,process_id FROM playback_sessions WHERE expires_at<=? AND state != 'expired'",
            (_iso(),),
        )
        for session_id, output_directory, process_id in rows or []:
            logger.info(
                "cleaning expired playback session_id=%s output_directory=%s",
                session_id,
                output_directory,
            )
            with self._lock:
                process = self._processes.get(session_id)
            self._stop_process(process, f"expiry session_id={session_id}")
            if process is None:
                self._stop_process_id(process_id, f"expiry session_id={session_id}")
            if output_directory:
                shutil.rmtree(output_directory, ignore_errors=True)
            with self._lock:
                self._processes.pop(session_id, None)
                self._remove_session_indexes_locked(session_id)
            self.db.execute(
                "UPDATE playback_sessions SET state='expired',completed_at=COALESCE(completed_at,?),output_directory=NULL,process_id=NULL WHERE id=?",
                (_iso(), session_id),
            )

        timeout = self._idle_timeout_seconds()
        orphan_rows = self.db.execute(
            "SELECT id,last_accessed_at,process_id,seek_generation FROM playback_sessions WHERE state IN ('starting','ready') AND process_id IS NOT NULL AND expires_at>?",
            (_iso(),),
        )
        for session_id, last_accessed_at, process_id, generation in orphan_rows or []:
            try:
                idle_for = (
                    time.time() - datetime.fromisoformat(last_accessed_at).timestamp()
                )
            except (TypeError, ValueError, OSError):
                idle_for = timeout
            if idle_for < timeout:
                continue
            logger.warning(
                "reaping orphaned playback worker session_id=%s pid=%s generation=%s idle_seconds=%.1f",
                session_id,
                process_id,
                generation,
                idle_for,
            )
            self.db.execute(
                "UPDATE playback_sessions SET state='stopping',failure_code=?,failure_detail=?,completed_at=? WHERE id=? AND seek_generation=? AND state IN ('starting','ready')",
                (
                    "PLAYBACK_IDLE_TIMEOUT",
                    json.dumps(
                        {
                            "stage": "orphan_reaper",
                            "idleSeconds": round(idle_for, 3),
                            "timeoutSeconds": timeout,
                            "processId": process_id,
                        },
                        sort_keys=True,
                    ),
                    _iso(),
                    session_id,
                    generation,
                ),
            )
            with self._lock:
                process = self._processes.get(session_id)
            self._stop_process(process, f"orphan_reaper session_id={session_id}")
            if process is None:
                self._stop_process_id(
                    process_id, f"orphan_reaper session_id={session_id}"
                )
                self.db.execute(
                    "UPDATE playback_sessions SET process_id=NULL WHERE id=? AND seek_generation=? AND state='stopping'",
                    (session_id, generation),
                )

        # Keep terminal rows only for bounded diagnostics.  Remove any
        # verified old output before deleting its row; completed sessions can
        # otherwise retain a non-null path forever and evade the old purge.
        retention_cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=7))
        terminal_rows = self.db.execute(
            "SELECT id,output_directory FROM playback_sessions "
            "WHERE state IN ('failed','completed','expired','stopping') "
            "AND completed_at IS NOT NULL AND completed_at<? "
            "AND process_id IS NULL AND output_directory IS NOT NULL AND output_directory<>''",
            (retention_cutoff,),
        )
        for session_id, output_directory in terminal_rows or []:
            if output_directory:
                shutil.rmtree(output_directory, ignore_errors=True)
            self.db.execute(
                "UPDATE playback_sessions SET output_directory=NULL WHERE id=? AND process_id IS NULL",
                (session_id,),
            )
        self.db.execute(
            "DELETE FROM playback_sessions "
            "WHERE state IN ('failed','completed','expired','stopping') "
            "AND completed_at IS NOT NULL AND completed_at<? "
            "AND process_id IS NULL AND (output_directory IS NULL OR output_directory='')",
            (retention_cutoff,),
        )

    def direct_path(
        self, user_id: str, entity_id: str, media_source_id: str | None = None
    ) -> Path:
        self.catalog.require_entity(user_id, entity_id)
        media_file_id = None
        if media_source_id:
            rows = self.db.execute(
                "SELECT media_file_id FROM media_sources WHERE id=? AND entity_id=?",
                (media_source_id, entity_id),
            )
            if not rows:
                raise HTTPException(404, "Media source not found.")
            media_file_id = rows[0][0]
        return self._file_path(entity_id, media_file_id)[1]

    def direct_path_and_metadata(
        self, user_id: str, entity_id: str, media_source_id: str | None = None
    ) -> tuple[Path, int, str]:
        """Resolve a direct source with the MIME type implied by its probe metadata."""
        self.catalog.require_entity(user_id, entity_id)
        media_file_id = None
        source: dict = {}
        if media_source_id:
            rows = self.db.execute(
                "SELECT media_file_id,container,width,height,video_codec,audio_codec FROM media_sources WHERE id=? AND entity_id=?",
                (media_source_id, entity_id),
            )
            if not rows:
                raise HTTPException(404, "Media source not found.")
            media_file_id, container, width, height, video_codec, audio_codec = rows[0]
            source = {
                "container": container,
                "width": width,
                "height": height,
                "videoCodec": video_codec,
                "audioCodec": audio_codec,
            }
        media_file_id, path = self._file_path(entity_id, media_file_id)
        if not source:
            rows = self.db.execute(
                "SELECT container,width,height,video_codec,audio_codec FROM media_sources WHERE entity_id=? AND media_file_id=? ORDER BY id LIMIT 1",
                (entity_id, media_file_id),
            )
            if rows:
                container, width, height, video_codec, audio_codec = rows[0]
                source = {
                    "container": container,
                    "width": width,
                    "height": height,
                    "videoCodec": video_codec,
                    "audioCodec": audio_codec,
                }
        if not source:
            source = {"container": path.suffix.lower().lstrip(".")}
        return path, path.stat().st_size, self._mime(source)

    def session_file(self, user_id: str, session_id: str, filename: str) -> Path:
        self._cleanup_expired()
        rows = self.db.execute(
            "SELECT output_directory,state,failure_code,failure_detail FROM playback_sessions WHERE id=? AND user_id=? AND expires_at>?",
            (session_id, user_id, _iso()),
        )
        index = self._segment_index(filename)
        allowed = filename == "master.m3u8" or index is not None
        if not rows or not allowed or rows[0][1] in {"stopping", "expired"}:
            raise HTTPException(404, "Playback session not found.")
        if rows[0][1] == "failed":
            snapshot = self._playlist_snapshot(Path(rows[0][0]))
            raise HTTPException(
                502,
                detail={
                    "sessionId": session_id,
                    "sessionState": rows[0][1],
                    "errorCode": rows[0][2] or "FFMPEG_FAILED",
                    "errorDetail": rows[0][3]
                    or "The playback process failed before producing this file.",
                    **snapshot,
                },
            )
        output = Path(rows[0][0]).resolve()
        path = output / filename
        if path.parent != output:
            raise HTTPException(404, "Playback session not found.")
        if index is not None and not path.is_file():
            path = self._ensure_segment(user_id, session_id, index)
        if not path.is_file():
            snapshot = self._playlist_snapshot(output)
            logger.warning(
                "playback output unavailable session_id=%s filename=%s state=%s snapshot=%s",
                session_id,
                filename,
                rows[0][1],
                snapshot,
            )
            raise HTTPException(
                503,
                detail={
                    "sessionId": session_id,
                    "sessionState": rows[0][1],
                    "errorCode": "PLAYBACK_OUTPUT_NOT_READY",
                    "errorDetail": "The playback session is still preparing media.",
                    **snapshot,
                },
                headers={"Retry-After": "1"},
            )
        self.db.execute(
            "UPDATE playback_sessions SET last_accessed_at=? WHERE id=? AND user_id=?",
            (_iso(), session_id, user_id),
        )
        logger.debug(
            "served playback output session_id=%s filename=%s state=%s",
            session_id,
            filename,
            rows[0][1],
        )
        return path

    def session_status(self, user_id: str, session_id: str) -> dict:
        self._cleanup_expired()
        rows = self.db.execute(
            "SELECT state,source_id,created_at,expires_at,last_accessed_at,failure_code,failure_detail,output_directory,requested_start_seconds,actual_start_seconds,seek_generation,process_id FROM playback_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        if not rows:
            raise HTTPException(404, "Playback session not found.")
        row = rows[0]
        self.db.execute(
            "UPDATE playback_sessions SET last_accessed_at=? WHERE id=? AND user_id=?",
            (_iso(), session_id, user_id),
        )
        output = Path(row[7])
        snapshot = self._playlist_snapshot(output)
        with self._lock:
            process = self._processes.get(session_id)
            worker = self._session_workers.get(session_id)
        process_alive = bool(
            (process is not None and process.poll() is None)
            or self._process_id_alive(row[11])
        )
        logger.debug(
            "playback session status session_id=%s state=%s process_alive=%s snapshot=%s",
            session_id,
            row[0],
            process_alive,
            snapshot,
        )
        return {
            "sessionId": session_id,
            "sessionState": row[0],
            "sourceId": row[1],
            "createdAt": row[2],
            "expiresAt": row[3],
            "lastAccessedAt": row[4],
            "errorCode": row[5],
            "errorDetail": row[6],
            "requestedStartPositionSeconds": row[8],
            "actualStartPositionSeconds": row[9],
            "seekGeneration": row[10],
            **snapshot,
            "processAlive": process_alive,
            "activeGeneration": worker.get("generation") if worker else row[10],
        }

    def cancel_session(self, user_id: str, session_id: str) -> None:
        self._cleanup_expired()
        rows = self.db.execute(
            "SELECT output_directory,process_id,state FROM playback_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        if not rows:
            raise HTTPException(404, "Playback session not found.")
        process_id = rows[0][1]
        self.db.execute(
            "UPDATE playback_sessions SET state='stopping',completed_at=? WHERE id=? AND user_id=? AND state NOT IN ('stopping','failed','completed','expired')",
            (_iso(), session_id, user_id),
        )
        with self._lock:
            process = self._processes.get(session_id)
        self._stop_process(
            process, f"client_cancel user_id={user_id} session_id={session_id}"
        )
        if process is None:
            self._stop_process_id(
                process_id, f"client_cancel user_id={user_id} session_id={session_id}"
            )
