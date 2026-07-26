from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

from app.catalog import Catalog
from app.config import Config
from app.client_auth import issue_ticket
from app.library import LANGUAGE_ALIASES, language_name
from app.logging_config import get_logger
from app.models.playback_settings import PlaybackSettings


logger = get_logger("playback")
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
    _processes: dict[str, subprocess.Popen] = {}
    _users: dict[str, set[str]] = {}
    _session_keys: dict[str, tuple[str, str, str, str]] = {}
    _seek_generations: dict[tuple[str, str, str], int] = {}
    _session_specs: dict[str, dict] = {}
    _session_workers: dict[str, dict] = {}
    _session_locks: dict[str, threading.RLock] = {}
    _segment_seconds = 4.0

    def __init__(self):
        self.db = Config().database
        self.catalog = Catalog()

    @classmethod
    def stop_all(cls) -> None:
        with cls._lock:
            sessions = list(cls._processes.items())
        for session_id, process in sessions:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    
                    process.kill()
            with cls._lock:
                cls._processes.pop(session_id, None)
                cls._session_keys.pop(session_id, None)
                cls._session_specs.pop(session_id, None)
                cls._session_workers.pop(session_id, None)
                cls._session_locks.pop(session_id, None)
        cls._users.clear()

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
        path = Path(rows[0][1]) / rows[0][2]
        if not path.is_file():
            raise HTTPException(404, "Media source is unavailable.")
        return rows[0][0], path

    def probe_entity(self, entity_id: str) -> list[dict]:
        executable = ffprobe_path()
        if not executable:
            return []
        rows = self.db.execute(
            "SELECT f.id,l.directory,f.relative_path FROM media_files f JOIN library_entities e ON e.id=f.entity_id JOIN libraries l ON l.id=e.library_id WHERE f.entity_id=? AND f.role=?",
            (entity_id, PLAYABLE_ROLE),
        )
        values = []
        for media_file_id, directory, relative_path in rows:
            path = Path(directory) / relative_path
            if not path.is_file():
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
                        str(path),
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
            video = next(
                (value for value in streams if value.get("codec_type") == "video"), {}
            )
            audio = next(
                (value for value in streams if value.get("codec_type") == "audio"), {}
            )
            format_value = payload.get("format") or {}
            source_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"zenstream:{media_file_id}")
            )
            value = {
                "id": source_id,
                "container": format_value.get("format_name"),
                "durationSeconds": float(format_value.get("duration") or 0),
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
            values.append(value)
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
            language = LANGUAGE_ALIASES.get(
                raw_language.lower(), raw_language.lower() or None
            )
            if language:
                tags["language"] = language
            if str(value.get("codec_type") or "").lower() == "subtitle":
                current_title = str(tags.get("title") or "").strip().lower()
                if not current_title or current_title in {"subtitle", "subtitles"}:
                    tags["title"] = language_name(language, "subtitle")
            value["tags"] = tags
            return value

        sidecars = [
            {
                "index": 1000 + index,
                "codec_type": "subtitle",
                "codec_name": Path(relative_path).suffix.lstrip("."),
                "fileId": file_id,
                "kind": role,
                "tags": {
                    **({"language": language} if language else {}),
                    "title": language_name(language, role),
                },
            }
            for index, (file_id, relative_path, language, role) in enumerate(
                self.db.execute(
                    "SELECT id,relative_path,language,role FROM media_files WHERE entity_id=? AND role IN ('subtitle','lyrics') ORDER BY relative_path COLLATE NOCASE",
                    (entity_id,),
                )
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
                + sidecars,
            }
            for row in rows
        ]

    @staticmethod
    def _profile_values(profile: dict, key: str, defaults: set[str]) -> set[str]:
        if key not in profile or profile[key] is None:
            return defaults
        value = profile[key]
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {str(item).strip().lower() for item in value if str(item).strip()}

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

    @classmethod
    def _playback_mode(cls, source: dict, profile: dict) -> str:
        if profile.get("forceTranscoding") is True:
            return "video-transcode"
        requested_mode = str(profile.get("requestedMode") or "").lower()
        if requested_mode == "video-transcode":
            return requested_mode
        containers = cls._profile_values(profile, "containers", {"mp4", "webm"})
        video = cls._profile_values(profile, "videoCodecs", {"h264", "vp9", "av1"})
        audio = cls._profile_values(profile, "audioCodecs", {"aac", "opus", "vorbis"})
        source_container = cls._container_values(source.get("container"))
        container_ok = bool(source_container & containers)
        video_streams = [
            stream
            for stream in source.get("streams", [])
            if str(stream.get("codec_type") or "").lower() == "video"
        ]
        has_video = bool(
            video_streams
            or source.get("width")
            or source.get("height")
            or source.get("videoCodec")
        )
        video_codec = str(source.get("videoCodec") or "").lower()
        audio_stream = cls._stream_for_profile(source, profile)
        audio_codec = str(
            audio_stream.get("codec_name") or source.get("audioCodec") or ""
        ).lower()
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

    def negotiate(self, user_id: str, entity_id: str, profile: dict) -> dict:
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
        access = issue_ticket(user_id, "resource", 6 * 60 * 60, entity=entity_id)
        if self._direct(source, profile):
            return {
                "mode": "direct",
                "sessionState": "ready",
                "source": source,
                "sourceId": source["id"],
                "audioStreamId": profile.get("audioStreamId"),
                "url": f"/api/playback/items/{entity_id}/stream?sourceId={source['id']}&access={access}",
                "mimeType": self._mime(source),
                "startPositionSeconds": 0.0,
                "durationSeconds": source.get("durationSeconds"),
            }
        if direct_only:
            raise HTTPException(
                409,
                detail={
                    "code": "DIRECT_PLAY_UNAVAILABLE",
                    "message": "The selected media cannot be played directly by this client.",
                },
            )
        start_time = max(0.0, float(profile.get("startPositionSeconds") or 0.0))
        start_time = min(
            start_time, max(0.0, float(source.get("durationSeconds") or 0.0))
        )
        transcode_mode = self._playback_mode(source, profile)
        logger.info(
            "playback decision entity_id=%s source_id=%s mode=%s container=%s video_codec=%s audio_codec=%s channels=%s bitrate=%s max_bitrate=%s",
            entity_id,
            source["id"],
            transcode_mode,
            source.get("container"),
            source.get("videoCodec"),
            source.get("audioCodec"),
            self._stream_for_profile(source, profile).get("channels"),
            source.get("bitrate"),
            profile.get("maxStreamingBitrate"),
        )
        result = self._transcode(
            user_id, entity_id, source, access, profile, start_time, transcode_mode
        )
        result["sessionState"] = result.get("sessionState", "starting")
        result["sourceId"] = source["id"]
        result["audioStreamId"] = profile.get("audioStreamId")
        result["startPositionSeconds"] = start_time
        result["durationSeconds"] = source.get("durationSeconds")
        return result

    @staticmethod
    def _mime(source: dict) -> str:
        container = str(source.get("container") or "").split(",", 1)[0].lower()
        return {
            "matroska": "video/x-matroska",
            "mkv": "video/x-matroska",
            "webm": "video/webm",
            "mov": "video/mp4",
            "mp4": "video/mp4",
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
                if (
                    existing_key == session_key
                    and existing_key[:3] == base_key
                ):
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
                    if not playlist_ready and (process is None or process.poll() is not None):
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
                result["sessionState"] = "ready" if self._startup_ready(reused_session[1]) else "starting"
                result["sourceId"] = source["id"]
                result["audioStreamId"] = profile.get("audioStreamId")
                logger.info(
                    "reused playback session is ready session_id=%s requested_start=%.3f actual_start=0.000",
                    existing_id,
                    start_time,
                )
                return result
            active = [
                process for process in self._processes.values() if process.poll() is None
            ]
            per_user = [
                process
                for session_id in self._users.get(user_id, set())
                if (process := self._processes.get(session_id))
                and process.poll() is None
            ]
            global_limit, user_limit = self._limits()
            if len(active) >= global_limit or len(per_user) >= user_limit:
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
            self._start_worker_locked(session_id, start_index)
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
        segment_count = max(1, int((duration + cls._segment_seconds - 0.0001) // cls._segment_seconds))
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:4",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        for index in range(segment_count):
            segment_duration = min(cls._segment_seconds, max(0.1, duration - index * cls._segment_seconds))
            lines.extend([f"#EXTINF:{segment_duration:.3f},", f"segment-{index:06d}.ts"])
        lines.append("#EXT-X-ENDLIST")
        temporary = output / "master.m3u8.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, output / "master.m3u8")

    def _build_ffmpeg_command(
        self,
        spec: dict,
        worker_dir: Path,
        start_index: int,
        stop_index: int | None = None,
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
        selected_audio_codec = str(selected_audio.get("codec_name") or source.get("audioCodec") or "").lower()
        try:
            selected_audio_channels = int(selected_audio.get("channels") or 2)
        except (TypeError, ValueError):
            selected_audio_channels = 2
        copy_audio = mode == "remux" or (
            mode == "video-transcode" and selected_audio_codec == "aac" and selected_audio_channels <= 2
        )
        has_video = any(
            str(stream.get("codec_type") or "").lower() == "video"
            for stream in source.get("streams", [])
        ) or bool(source.get("width") or source.get("height"))
        command = [
            spec["executable"], "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start_time:.3f}",
            "-noaccurate_seek" if mode in {"remux", "audio-transcode"} else "-accurate_seek",
            "-i", str(spec["path"]),
        ]
        if has_video:
            command.extend(["-map", "0:v:0", "-c:v", "copy" if mode in {"remux", "audio-transcode"} else "libx264"])
        else:
            command.append("-vn")
        command.extend(["-map", audio_map, "-c:a", "copy" if copy_audio else "aac"])
        if not copy_audio:
            command.extend(["-ac", "2", "-ar", "48000", "-b:a", "192k"])
        maximum_bitrate = profile.get("maxStreamingBitrate")
        if maximum_bitrate and mode == "video-transcode":
            command.extend(["-maxrate", str(int(maximum_bitrate)), "-bufsize", str(int(maximum_bitrate) * 2)])
        if mode == "video-transcode":
            command.extend([
                "-preset", "veryfast", "-force_key_frames", "expr:gte(t,n_forced*4)",
                "-sc_threshold", "0", "-g", "96", "-keyint_min", "96",
            ])
        output_options = [
            "-copyts", "-avoid_negative_ts", "disabled", "-f", "hls", "-hls_time", "4",
            "-hls_playlist_type", "event", "-hls_list_size", "0", "-start_number", str(start_index),
            "-hls_segment_filename", str(worker_dir / "segment-%06d.ts"),
            str(worker_dir / "worker.m3u8"),
        ]
        if stop_index is not None:
            output_options[0:0] = ["-to", f"{(stop_index + 1) * self._segment_seconds:.3f}"]
        command.extend(output_options)
        return command

    def _monitor_startup(
        self,
        session_id: str,
        output: Path,
        process: subprocess.Popen,
        generation: int,
    ) -> None:
        deadline = time.monotonic() + 10
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
        process.terminate()
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

    def _start_worker_locked(self, session_id: str, start_index: int) -> subprocess.Popen:
        spec = self._session_specs[session_id]
        old = self._processes.get(session_id)
        if old is not None and old.poll() is None:
            old.terminate()
            logger.info("cancelled playback worker session_id=%s reason=segment_seek", session_id)
        generation = int(spec.get("generation") or 0) + 1
        spec["generation"] = generation
        worker_dir = spec["output"] / f"worker-{generation:06d}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        command = self._build_ffmpeg_command(spec, worker_dir, start_index, start_index)
        logger.info(
            "starting playback worker session_id=%s generation=%s start_index=%s source_start=%.3f output_directory=%s",
            session_id, generation, start_index, start_index * self._segment_seconds, spec["output"],
        )
        logger.info(
            "ffmpeg command session_id=%s generation=%s command=%s",
            session_id,
            generation,
            command,
        )
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
        worker = {
            "process": process,
            "generation": generation,
            "start_index": start_index,
            "stop_index": start_index,
            "worker_dir": worker_dir,
        }
        self._session_workers[session_id] = worker
        self._processes[session_id] = process
        self._users.setdefault(spec["user_id"], set()).add(session_id)
        self.db.execute(
            "UPDATE playback_sessions SET process_id=?,state='starting',requested_start_seconds=?,actual_start_seconds=?,seek_generation=?,failure_code=NULL,failure_detail=NULL WHERE id=?",
            (process.pid, start_index * self._segment_seconds, start_index * self._segment_seconds, generation, session_id),
        )
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
                    for line in playlist.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.startswith("#EXTINF:") and ":" in line
                ),
                None,
            ) if playlist.is_file() else None,
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
        deadline = time.monotonic() + 30
        with lock:
            while time.monotonic() < deadline:
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
                available_end = self._playlist_snapshot(output).get("availableSegmentEnd")
                if process is not None and process.poll() is not None and process.returncode != 0:
                    failure_row = self.db.execute(
                        "SELECT state,failure_code,failure_detail FROM playback_sessions WHERE id=?",
                        (session_id,),
                    )
                    failure_detail = "FFmpeg exited before the requested HLS segment was produced."
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
                        and
                        available_end is not None
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
        raise HTTPException(
            503,
            detail={
                "sessionId": session_id,
                "sessionState": "starting",
                "errorCode": "HLS_SEGMENT_TIMEOUT",
                "errorDetail": "The requested HLS segment was not produced before the bounded startup deadline.",
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

    def _cleanup_expired(self) -> None:
        rows = self.db.execute(
            "SELECT id,output_directory FROM playback_sessions WHERE expires_at<=? AND state NOT IN ('expired','stopping')",
            (_iso(),),
        )
        for session_id, output_directory in rows or []:
            logger.info(
                "cleaning expired playback session_id=%s output_directory=%s",
                session_id,
                output_directory,
            )
            with self._lock:
                process = self._processes.get(session_id)
                if process is not None and process.poll() is None:
                    process.terminate()
                    logger.info(
                        "terminated expired playback process session_id=%s", session_id
                    )
            if output_directory:
                shutil.rmtree(output_directory, ignore_errors=True)
            with self._lock:
                self._processes.pop(session_id, None)
                self._session_keys.pop(session_id, None)
                self._session_specs.pop(session_id, None)
                self._session_workers.pop(session_id, None)
                self._session_locks.pop(session_id, None)
            self.db.execute(
                "UPDATE playback_sessions SET state='expired' WHERE id=?",
                (session_id,),
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
            "SELECT state,source_id,created_at,expires_at,last_accessed_at,failure_code,failure_detail,output_directory,requested_start_seconds,actual_start_seconds,seek_generation FROM playback_sessions WHERE id=? AND user_id=?",
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
        process_alive = bool(process is not None and process.poll() is None)
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
            "SELECT output_directory FROM playback_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        if not rows:
            raise HTTPException(404, "Playback session not found.")
        with self._lock:
            process = self._processes.get(session_id)
            if process is not None and process.poll() is None:
                process.terminate()
                logger.info(
                    "cancelled playback session_id=%s user_id=%s", session_id, user_id
                )
        self.db.execute(
            "UPDATE playback_sessions SET state='stopping' WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
