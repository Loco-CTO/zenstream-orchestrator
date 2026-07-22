"""Media probing and direct/HLS playback runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


def ffmpeg_path() -> str | None:
    return os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return os.getenv("FFPROBE_PATH") or shutil.which("ffprobe")


class PlaybackManager:
    _lock = threading.RLock()
    _processes: dict[str, subprocess.Popen] = {}
    _users: dict[str, set[str]] = {}

    def __init__(self):
        self.db = Config().database
        self.catalog = Catalog()

    def _file_path(self, entity_id: str, media_file_id: str | None = None, role: str = "video") -> tuple[str, Path]:
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
            "SELECT f.id,l.directory,f.relative_path FROM media_files f JOIN library_entities e ON e.id=f.entity_id JOIN libraries l ON l.id=e.library_id WHERE f.entity_id=? AND f.role='video'",
            (entity_id,),
        )
        values = []
        for media_file_id, directory, relative_path in rows:
            path = Path(directory) / relative_path
            if not path.is_file():
                continue
            try:
                completed = subprocess.run(
                    [executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                    capture_output=True, text=True, timeout=60, check=True,
                )
                payload = json.loads(completed.stdout)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                continue
            streams = payload.get("streams") or []
            video = next((value for value in streams if value.get("codec_type") == "video"), {})
            audio = next((value for value in streams if value.get("codec_type") == "audio"), {})
            format_value = payload.get("format") or {}
            source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"zenstream:{media_file_id}"))
            value = {
                "id": source_id,
                "container": format_value.get("format_name"),
                "durationSeconds": float(format_value.get("duration") or 0),
                "bitrate": int(float(format_value.get("bit_rate") or 0)),
                "width": video.get("width"), "height": video.get("height"),
                "videoCodec": video.get("codec_name"), "audioCodec": audio.get("codec_name"),
                "streams": streams,
            }
            self.db.execute(
                "INSERT INTO media_sources(id,entity_id,media_file_id,container,duration_seconds,bitrate,width,height,video_codec,audio_codec,probe_payload,probed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_id,media_file_id) DO UPDATE SET container=excluded.container,duration_seconds=excluded.duration_seconds,bitrate=excluded.bitrate,width=excluded.width,height=excluded.height,video_codec=excluded.video_codec,audio_codec=excluded.audio_codec,probe_payload=excluded.probe_payload,probed_at=excluded.probed_at",
                (source_id, entity_id, media_file_id, value["container"], value["durationSeconds"], value["bitrate"], value["width"], value["height"], value["videoCodec"], value["audioCodec"], json.dumps(payload), _iso()),
            )
            values.append(value)
        return values

    def sources(self, user_id: str, entity_id: str) -> list[dict]:
        self.catalog.require_entity(user_id, entity_id)
        rows = self.db.execute(
            "SELECT id,container,duration_seconds,bitrate,width,height,video_codec,audio_codec,probe_payload FROM media_sources WHERE entity_id=?",
            (entity_id,),
        )
        if not rows:
            self.probe_entity(entity_id)
            rows = self.db.execute(
                "SELECT id,container,duration_seconds,bitrate,width,height,video_codec,audio_codec,probe_payload FROM media_sources WHERE entity_id=?",
                (entity_id,),
            )
        sidecars = [
            {"index": 1000 + index, "codec_type": "subtitle", "codec_name": Path(relative_path).suffix.lstrip("."), "fileId": file_id, "tags": {"language": language} if language else {}}
            for index, (file_id, relative_path, language) in enumerate(self.db.execute(
                "SELECT id,relative_path,language FROM media_files WHERE entity_id=? AND role='subtitle' ORDER BY relative_path COLLATE NOCASE",
                (entity_id,),
            ))
        ]
        return [{"id": row[0], "container": row[1], "durationSeconds": row[2], "bitrate": row[3], "width": row[4], "height": row[5], "videoCodec": row[6], "audioCodec": row[7], "streams": (json.loads(row[8]).get("streams") or []) + sidecars} for row in rows]

    @staticmethod
    def _direct(source: dict, profile: dict) -> bool:
        if str(profile.get("engine") or "").lower() == "mpv":
            return True
        containers = {str(value).lower() for value in profile.get("containers") or ["mp4", "webm"]}
        video = {str(value).lower() for value in profile.get("videoCodecs") or ["h264", "vp9", "av1"]}
        audio = {str(value).lower() for value in profile.get("audioCodecs") or ["aac", "opus", "vorbis"]}
        source_containers = set(str(source.get("container") or "").lower().split(","))
        return bool(source_containers & containers) and source.get("videoCodec") in video and source.get("audioCodec") in audio

    def negotiate(self, user_id: str, entity_id: str, profile: dict) -> dict:
        sources = self.sources(user_id, entity_id)
        if not sources:
            raise HTTPException(409, "Media has not been probed or is unavailable.")
        source = sources[0]
        access = issue_ticket(user_id, "resource", 6 * 60 * 60, entity=entity_id)
        if self._direct(source, profile):
            return {"mode": "direct", "source": source, "url": f"/api/playback/items/{entity_id}/stream?access={access}", "mimeType": self._mime(source)}
        return self._transcode(user_id, entity_id, source, access, profile)

    @staticmethod
    def _mime(source: dict) -> str:
        container = str(source.get("container") or "").split(",", 1)[0]
        return {"matroska": "video/x-matroska", "webm": "video/webm", "mov": "video/mp4", "mp4": "video/mp4"}.get(container, "application/octet-stream")

    def _transcode(self, user_id: str, entity_id: str, source: dict, access: str, profile: dict) -> dict:
        executable = ffmpeg_path()
        if not executable:
            raise HTTPException(503, "FFmpeg is not available.")
        maximum = max(1, int(os.getenv("MAX_TRANSCODES", "2")))
        per_user_maximum = max(1, int(os.getenv("MAX_TRANSCODES_PER_USER", "1")))
        with self._lock:
            active = [value for value in self._processes.values() if value.poll() is None]
            per_user = [value for session in self._users.get(user_id, set()) if (value := self._processes.get(session)) and value.poll() is None]
            if len(active) >= maximum or len(per_user) >= per_user_maximum:
                raise HTTPException(429, "Transcoding capacity is currently in use.")
            session_id = str(uuid.uuid4())
            output = Path(tempfile.gettempdir()) / "zenstream-transcodes" / session_id
            output.mkdir(parents=True, exist_ok=False)
            media_file_id, path = self._file_path(entity_id)
            command = [
                executable, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
                "-hls_segment_filename", str(output / "segment-%06d.ts"), str(output / "master.m3u8"),
            ]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._processes[session_id] = process
            self._users.setdefault(user_id, set()).add(session_id)
            self.db.execute(
                "INSERT INTO playback_sessions(id,user_id,entity_id,source_id,mode,state,output_directory,created_at,expires_at) VALUES(?,?,?,?,?,'active',?,?,?)",
                (session_id, user_id, entity_id, source["id"], "hls", str(output), _iso(), _iso(datetime.now(timezone.utc) + timedelta(hours=6))),
            )
            threading.Thread(target=self._watch, args=(user_id, session_id, process), daemon=True).start()
        return {"mode": "hls", "source": source, "sessionId": session_id, "url": f"/api/playback/sessions/{session_id}/master.m3u8?access={access}", "mimeType": "application/vnd.apple.mpegurl"}

    def _watch(self, user_id: str, session_id: str, process: subprocess.Popen) -> None:
        process.wait()
        with self._lock:
            self._processes.pop(session_id, None)
            self._users.get(user_id, set()).discard(session_id)
        self.db.execute("UPDATE playback_sessions SET state=? WHERE id=?", ("completed" if process.returncode == 0 else "failed", session_id))

    def direct_path(self, user_id: str, entity_id: str) -> Path:
        self.catalog.require_entity(user_id, entity_id)
        return self._file_path(entity_id)[1]

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
            raise HTTPException(503, "Playback output is not ready.", headers={"Retry-After": "1"})
        return path
