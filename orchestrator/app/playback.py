from __future__ import annotations

import json
import hashlib
import logging
import os
import platform
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


logger = logging.getLogger(__name__)
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

    def __init__(self):
        self.db = Config().database
        self.catalog = Catalog()

    def _file_path(
        self, entity_id: str, media_file_id: str | None = None, role: str = PLAYABLE_ROLE
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
                if not completed.stdout:
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
            raw_language = str(tags.get("language") or tags.get("LANGUAGE") or "").strip()
            language = LANGUAGE_ALIASES.get(raw_language.lower(), raw_language.lower() or None)
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
                "streams": [normalize_stream(stream) for stream in (json.loads(row[9]).get("streams") or [])] + sidecars,
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
    def _direct(cls, source: dict, profile: dict) -> bool:
        if profile.get("forceTranscoding") is True:
            return False
        if str(profile.get("engine") or "").lower() == "mpv":
            return True
        containers = cls._profile_values(profile, "containers", {"mp4", "webm"})
        containers |= {"mkv"} if "matroska" in containers else set()
        containers |= {"ts"} if "mpegts" in containers else set()
        video = cls._profile_values(profile, "videoCodecs", {"h264", "vp9", "av1"})
        audio = cls._profile_values(profile, "audioCodecs", {"aac", "opus", "vorbis"})
        source_containers = cls._container_values(source.get("container"))
        maximum_bitrate = profile.get("maxStreamingBitrate")
        bitrate_ok = (
            not maximum_bitrate
            or not source.get("bitrate")
            or source["bitrate"] <= int(maximum_bitrate)
        )
        return (
            bool(source_containers & containers)
            and str(source.get("videoCodec") or "").lower() in video
            and str(source.get("audioCodec") or "").lower() in audio
            and bitrate_ok
        )

    def negotiate(self, user_id: str, entity_id: str, profile: dict) -> dict:
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
        requested_source_id = profile.get("mediaSourceId")
        if requested_source_id:
            source = next(
                (value for value in sources if value["id"] == requested_source_id),
                None,
            )
            if source is None:
                raise HTTPException(404, "Media source not found.")
        else:
            source = sources[0]
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
                "source": source,
                "url": f"/api/playback/items/{entity_id}/stream?mediaSourceId={source['id']}&access={access}",
                "mimeType": self._mime(source),
            }
        if direct_only:
            raise HTTPException(
                409,
                detail={
                    "code": "DIRECT_PLAY_UNAVAILABLE",
                    "message": "The selected media cannot be played directly by this client.",
                },
            )
        start_time = max(0.0, float(profile.get("startTimeSeconds") or 0.0))
        start_time = min(start_time, max(0.0, float(source.get("durationSeconds") or 0.0)))
        return self._transcode(user_id, entity_id, source, access, profile, start_time)

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
        self, user_id: str, entity_id: str, source: dict, access: str, profile: dict,
        start_time: float = 0.0,
    ) -> dict:
        executable = ffmpeg_path()
        if not executable:
            raise HTTPException(
                503,
                detail={"code": "FFMPEG_UNAVAILABLE", "message": "FFmpeg is not available."},
            )
        maximum = max(1, int(os.getenv("MAX_TRANSCODES", "2")))
        per_user_maximum = max(1, int(os.getenv("MAX_TRANSCODES_PER_USER", "1")))
        with self._lock:
            session_key = self._transcode_key(user_id, entity_id, source, profile, start_time)
            for existing_id in self._users.get(user_id, set()):
                process = self._processes.get(existing_id)
                if (
                    process is not None
                    and process.poll() is None
                    and self._session_keys.get(existing_id) == session_key
                ):
                    logger.info(
                        "reusing active playback transcode user_id=%s entity_id=%s session_id=%s",
                        user_id,
                        entity_id,
                        existing_id,
                    )
                    result = self._hls_result(existing_id, source, access)
                    result["startTimeSeconds"] = start_time
                    return result
            base_key = self._transcode_key(user_id, entity_id, source, profile, 0.0)[:3]
            for existing_id in list(self._users.get(user_id, set())):
                process = self._processes.get(existing_id)
                existing_key = self._session_keys.get(existing_id)
                if (
                    process is not None
                    and process.poll() is None
                    and existing_key is not None
                    and existing_key[:3] == base_key
                ):
                    process.terminate()
            active = [
                value for value in self._processes.values() if value.poll() is None
            ]
            per_user = [
                value
                for session in self._users.get(user_id, set())
                if (value := self._processes.get(session)) and value.poll() is None
            ]
            if len(active) >= maximum or len(per_user) >= per_user_maximum:
                raise HTTPException(
                    429,
                    detail={
                        "code": "TRANSCODE_CAPACITY",
                        "message": "Transcoding capacity is currently in use.",
                    },
                    headers={"Retry-After": "2"},
                )
            session_id = str(uuid.uuid4())
            output = Path(tempfile.gettempdir()) / "zenstream-transcodes" / session_id
            output.mkdir(parents=True, exist_ok=False)
            media_file_id, path = self._file_path(entity_id, source.get("mediaFileId"))
            audio_index = profile.get("audioStreamIndex")
            audio_map = (
                f"0:{int(audio_index)}"
                if audio_index is not None and int(audio_index) >= 0
                else "0:a:0?"
            )
            command = [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start_time:.3f}",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                audio_map,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
            ]
            maximum_bitrate = profile.get("maxStreamingBitrate")
            if maximum_bitrate:
                command.extend(
                    [
                        "-maxrate",
                        str(int(maximum_bitrate)),
                        "-bufsize",
                        str(int(maximum_bitrate) * 2),
                    ]
                )
            command.extend([
                "-f",
                "hls",
                "-hls_time",
                "4",
                "-hls_playlist_type",
                "event",
                "-hls_segment_filename",
                str(output / "segment-%06d.ts"),
                str(output / "master.m3u8"),
            ])
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._processes[session_id] = process
            self._users.setdefault(user_id, set()).add(session_id)
            self._session_keys[session_id] = session_key
            self.db.execute(
                "INSERT INTO playback_sessions(id,user_id,entity_id,source_id,mode,state,output_directory,created_at,expires_at) VALUES(?,?,?,?,?,'active',?,?,?)",
                (
                    session_id,
                    user_id,
                    entity_id,
                    source["id"],
                    "hls",
                    str(output),
                    _iso(),
                    _iso(datetime.now(timezone.utc) + timedelta(hours=6)),
                ),
            )
            threading.Thread(
                target=self._watch, args=(user_id, session_id, process), daemon=True
            ).start()
        result = self._hls_result(session_id, source, access)
        result["startTimeSeconds"] = start_time
        return result

    @staticmethod
    def _transcode_key(
        user_id: str, entity_id: str, source: dict, profile: dict, start_time: float = 0.0
    ) -> tuple[str, str, str, str]:
        settings = {
            "audioStreamIndex": profile.get("audioStreamIndex"),
            "maxStreamingBitrate": profile.get("maxStreamingBitrate"),
            "startTimeSeconds": round(start_time, 3),
        }
        return (
            user_id,
            entity_id,
            str(source["id"]),
            json.dumps(settings, sort_keys=True),
        )

    @staticmethod
    def _hls_result(session_id: str, source: dict, access: str) -> dict:
        return {
            "mode": "hls",
            "source": source,
            "sessionId": session_id,
            "url": f"/api/playback/sessions/{session_id}/master.m3u8?access={access}",
            "mimeType": "application/vnd.apple.mpegurl",
        }

    def _watch(self, user_id: str, session_id: str, process: subprocess.Popen) -> None:
        process.wait()
        with self._lock:
            self._processes.pop(session_id, None)
            self._users.get(user_id, set()).discard(session_id)
            self._session_keys.pop(session_id, None)
        self.db.execute(
            "UPDATE playback_sessions SET state=? WHERE id=?",
            ("completed" if process.returncode == 0 else "failed", session_id),
        )
        row = self.db.execute(
            "SELECT output_directory FROM playback_sessions WHERE id=?", (session_id,)
        )
        if row:
            shutil.rmtree(row[0][0], ignore_errors=True)

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
        rows = self.db.execute(
            "SELECT output_directory FROM playback_sessions WHERE id=? AND user_id=? AND expires_at>?",
            (session_id, user_id, _iso()),
        )
        if not rows or Path(filename).name != filename:
            raise HTTPException(404, "Playback session not found.")
        path = Path(rows[0][0]) / filename
        deadline = time.monotonic() + 8
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not path.is_file():
            raise HTTPException(
                503, "Playback output is not ready.", headers={"Retry-After": "1"}
            )
        return path
