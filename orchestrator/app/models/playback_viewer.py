from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from threading import Lock

from app.config import Config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _clean(value: object, limit: int = 160) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value)).strip()
    return text[:limit] or None


def normalize_device_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "deviceId": _clean(value.get("deviceId"), 128),
        "deviceType": _clean(value.get("deviceType"), 32) or "unknown",
        "browser": _clean(value.get("browser"), 80),
        "operatingSystem": _clean(value.get("operatingSystem"), 80),
        "deviceName": _clean(value.get("deviceName"), 120),
        "clientName": _clean(value.get("clientName"), 80),
        "clientVersion": _clean(value.get("clientVersion"), 80),
    }


def _title(relative_path: str | None, entity_type: str | None) -> str:
    name = PurePath(relative_path or "").stem.strip()
    if name:
        return name
    return (entity_type or "Media").replace("_", " ").title()


def _json_streams(payload: object) -> list[dict]:
    try:
        value = json.loads(payload or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    streams = value.get("streams") if isinstance(value, dict) else None
    safe = []
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict):
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        safe.append(
            {
                key: stream[key]
                for key in (
                    "index",
                    "codec_type",
                    "codec_name",
                    "profile",
                    "width",
                    "height",
                    "channels",
                    "sample_rate",
                    "bit_rate",
                )
                if stream.get(key) is not None
            }
            | {
                "language": _clean(tags.get("language") or tags.get("LANGUAGE"), 32),
                "title": _clean(tags.get("title") or tags.get("TITLE"), 120),
            }
        )
    return safe


class PlaybackViewerStore:
    """Persistence for live player instances and administrator controls.

    This is deliberately separate from ``playback_sessions``. The latter is a
    process-backed HLS worker and may be shared by several viewers; direct play
    does not create one at all.
    """

    HEARTBEAT_TIMEOUT_SECONDS = 15
    COMMAND_TTL_SECONDS = 30
    HEARTBEAT_WRITE_INTERVAL_SECONDS = 3
    _heartbeat_write_lock = Lock()
    _last_heartbeat_write: dict[str, float] = {}
    _expiry_check_lock = Lock()
    _last_expiry_check = 0.0

    def __init__(self, database=None):
        self.db = database or Config().database

    def available(self) -> bool:
        try:
            rows = self.db.read_execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('user_devices','playback_viewer_sessions','playback_viewer_commands')"
            )
        except Exception:
            return False
        return isinstance(rows, list) and {row[0] for row in rows if row} == {
            "user_devices",
            "playback_viewer_sessions",
            "playback_viewer_commands",
        }

    def _has_column(self, table: str, column: str) -> bool:
        try:
            return any(
                row[1] == column
                for row in self.db.read_execute(f"PRAGMA table_info({table})")
            )
        except Exception:
            return False

    def ensure_device(
        self,
        user_id: str,
        metadata: object = None,
        ip_address: str | None = None,
        auth_session_id: str | None = None,
    ) -> str | None:
        if not self.available():
            return None
        device = normalize_device_metadata(metadata)
        device_key = device.get("deviceId") or "legacy"
        now = _iso()
        rows = self.db.execute(
            "SELECT id FROM user_devices WHERE user_id=? AND device_key=?",
            (user_id, device_key),
        )
        if rows:
            device_id = rows[0][0]
            self.db.execute(
                "UPDATE user_devices SET device_type=?,browser=?,operating_system=?,device_name=?,client_name=?,client_version=?,ip_address=?,last_seen_at=? WHERE id=? AND user_id=?",
                (
                    device["deviceType"],
                    device.get("browser"),
                    device.get("operatingSystem"),
                    device.get("deviceName"),
                    device.get("clientName"),
                    device.get("clientVersion"),
                    _clean(ip_address, 80),
                    now,
                    device_id,
                    user_id,
                ),
            )
        else:
            device_id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO user_devices(id,user_id,device_key,device_type,browser,operating_system,device_name,client_name,client_version,ip_address,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    device_id,
                    user_id,
                    device_key,
                    device["deviceType"],
                    device.get("browser"),
                    device.get("operatingSystem"),
                    device.get("deviceName"),
                    device.get("clientName"),
                    device.get("clientVersion"),
                    _clean(ip_address, 80),
                    now,
                    now,
                ),
            )
        if auth_session_id and self._has_column("user_sessions", "device_id"):
            self.db.execute(
                "UPDATE user_sessions SET device_id=? WHERE id=? AND user_id=?",
                (device_id, auth_session_id, user_id),
            )
        return device_id

    def _expire_stale(self) -> None:
        if not self.available():
            return
        now = _now()
        cutoff = _iso(now - timedelta(seconds=self.HEARTBEAT_TIMEOUT_SECONDS))
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE playback_viewer_sessions SET state='expired',ended_at=? WHERE state='active' AND last_heartbeat_at<?",
                (_iso(now), cutoff),
            )
            cursor.execute(
                "UPDATE playback_viewer_commands SET state='expired' WHERE state IN ('pending','delivered') AND expires_at<=?",
                (_iso(now),),
            )

    def cleanup_history(self, retention_days: int = 30) -> int:
        """Retain only recent viewer history and inactive device records."""
        if not self.available():
            return 0
        self._expire_stale()
        cutoff = _iso(_now() - timedelta(days=max(1, int(retention_days))))
        removed = 0
        session_device_guard = ""
        if self._has_column("user_sessions", "device_id"):
            session_device_guard = (
                "AND NOT EXISTS (SELECT 1 FROM user_sessions s "
                "WHERE s.device_id=user_devices.id)"
            )
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM playback_viewer_commands WHERE "
                "viewer_session_id IN (SELECT id FROM playback_viewer_sessions "
                "WHERE state IN ('ended','expired') AND COALESCE(ended_at,created_at)<?) "
                "OR (state IN ('expired','acknowledged','failed') "
                "AND COALESCE(acknowledged_at,expires_at,issued_at)<?)",
                (cutoff, cutoff),
            )
            removed += max(0, cursor.rowcount)
            cursor.execute(
                "DELETE FROM playback_viewer_sessions WHERE state IN ('ended','expired') "
                "AND COALESCE(ended_at,created_at, last_heartbeat_at)<?",
                (cutoff,),
            )
            removed += max(0, cursor.rowcount)
            cursor.execute(
                "DELETE FROM user_devices WHERE last_seen_at<? "
                "AND NOT EXISTS (SELECT 1 FROM playback_viewer_sessions v "
                "WHERE v.device_id=user_devices.id AND v.state='active') "
                + session_device_guard,
                (cutoff,),
            )
            removed += max(0, cursor.rowcount)
        return removed

    def _expire_stale_if_due(self) -> None:
        current = time.monotonic()
        with self._expiry_check_lock:
            if current - self._last_expiry_check < 1:
                return
            type(self)._last_expiry_check = current
        self._expire_stale()

    @classmethod
    def _should_persist_heartbeat(cls, viewer_id: str, force: bool = False) -> bool:
        current = time.monotonic()
        with cls._heartbeat_write_lock:
            previous = cls._last_heartbeat_write.get(viewer_id, 0.0)
            if not force and current - previous < cls.HEARTBEAT_WRITE_INTERVAL_SECONDS:
                return False
            cls._last_heartbeat_write[viewer_id] = current
            if len(cls._last_heartbeat_write) > 10_000:
                cutoff = current - cls.HEARTBEAT_TIMEOUT_SECONDS * 4
                cls._last_heartbeat_write = {
                    key: value
                    for key, value in cls._last_heartbeat_write.items()
                    if value >= cutoff
                }
            return True

    def create_viewer(
        self,
        user_id: str,
        auth_session_id: str | None,
        entity_id: str,
        source_id: str,
        mode: str,
        profile: dict,
        worker_session_id: str | None = None,
        ip_address: str | None = None,
    ) -> str | None:
        if not self.available():
            return None
        device_id = self.ensure_device(
            user_id,
            profile.get("device"),
            ip_address,
            auth_session_id,
        )
        viewer_id = str(uuid.uuid4())
        start = max(0.0, float(profile.get("startPositionSeconds") or 0.0))
        duration = profile.get("durationSeconds")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        max_bitrate = profile.get("maxStreamingBitrate")
        try:
            max_bitrate = int(max_bitrate) if max_bitrate is not None else None
        except (TypeError, ValueError):
            max_bitrate = None
        self.db.execute(
            "INSERT INTO playback_viewer_sessions(id,user_id,auth_session_id,device_id,entity_id,source_id,worker_session_id,mode,state,engine,position_seconds,duration_seconds,paused,created_at,last_heartbeat_at,requested_bitrate,audio_stream_id,requested_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                viewer_id,
                user_id,
                auth_session_id,
                device_id,
                entity_id,
                source_id,
                worker_session_id,
                mode,
                "active",
                _clean(profile.get("engine"), 32),
                start,
                duration,
                int(bool(profile.get("paused"))),
                _iso(),
                _iso(),
                max_bitrate,
                _clean(profile.get("audioStreamId"), 40),
                _clean(profile.get("requestedMode"), 40),
            ),
        )
        return viewer_id

    @staticmethod
    def _select_sql() -> str:
        return """
            SELECT v.id,v.user_id,u.username,v.device_id,
                   d.device_type,d.browser,d.operating_system,d.device_name,
                   d.client_name,d.client_version,d.ip_address,
                   v.entity_id,e.entity_type,e.relative_path,e.season_number,e.episode_number,
                   v.mode,v.state,v.engine,v.position_seconds,v.duration_seconds,v.paused,
                   v.created_at,v.last_heartbeat_at,v.ended_at,v.worker_session_id,
                   v.source_id,v.requested_bitrate,v.audio_stream_id,v.requested_mode,
                   ms.container,ms.bitrate,ms.width,ms.height,ms.video_codec,ms.audio_codec,
                   ms.probe_payload,ps.state,ps.process_id
              FROM playback_viewer_sessions v
              JOIN users u ON u.id=v.user_id
              LEFT JOIN user_devices d ON d.id=v.device_id
              LEFT JOIN library_entities e ON e.id=v.entity_id
              LEFT JOIN media_sources ms ON ms.id=v.source_id
              LEFT JOIN playback_sessions ps ON ps.id=v.worker_session_id
        """

    @staticmethod
    def _payload(row: tuple, detail: bool = False) -> dict:
        (
            viewer_id,
            user_id,
            username,
            device_id,
            device_type,
            browser,
            operating_system,
            device_name,
            client_name,
            client_version,
            ip_address,
            entity_id,
            entity_type,
            relative_path,
            season_number,
            episode_number,
            mode,
            state,
            engine,
            position,
            duration,
            paused,
            created_at,
            last_heartbeat_at,
            ended_at,
            worker_session_id,
            source_id,
            requested_bitrate,
            audio_stream_id,
            requested_mode,
            container,
            source_bitrate,
            width,
            height,
            video_codec,
            audio_codec,
            probe_payload,
            worker_state,
            worker_process_id,
        ) = row
        title = _title(relative_path, entity_type)
        episode_label = None
        if season_number is not None or episode_number is not None:
            season = f"S{int(season_number):02d}" if season_number is not None else "S?"
            episode = (
                f"E{int(episode_number):02d}" if episode_number is not None else "E?"
            )
            episode_label = f"{season} · {episode}"
        value = {
            "id": viewer_id,
            "user": {"id": user_id, "username": username},
            "device": {
                "id": device_id,
                "type": device_type or "unknown",
                "browser": browser,
                "operatingSystem": operating_system,
                "name": device_name,
                "clientName": client_name,
                "clientVersion": client_version,
                "ipAddress": ip_address,
            },
            "item": {
                "id": entity_id,
                "title": title,
                "type": entity_type,
                "seasonNumber": season_number,
                "episodeNumber": episode_number,
                "subtitle": episode_label,
            },
            "playback": {
                "mode": mode,
                "state": state,
                "engine": engine,
                "positionSeconds": float(position or 0),
                "durationSeconds": float(duration) if duration is not None else None,
                "paused": bool(paused),
                "workerSessionId": worker_session_id,
                "requestedBitrate": requested_bitrate,
                "audioStreamId": audio_stream_id,
                "requestedMode": requested_mode,
            },
            "timestamps": {
                "createdAt": created_at,
                "lastHeartbeatAt": last_heartbeat_at,
                "endedAt": ended_at,
            },
        }
        if detail:
            streams = _json_streams(probe_payload)
            audio_streams = [
                stream for stream in streams if stream.get("codec_type") == "audio"
            ]
            selected = next(
                (
                    stream
                    for stream in audio_streams
                    if str(stream.get("index")) == str(audio_stream_id)
                ),
                None,
            ) or (audio_streams[0] if audio_streams else None)
            value["diagnostics"] = {
                "sourceId": source_id,
                "container": container,
                "resolution": {
                    "width": width,
                    "height": height,
                },
                "videoCodec": video_codec,
                "audioCodec": audio_codec,
                "audioChannels": selected.get("channels") if selected else None,
                "selectedAudioStream": selected,
                "sourceBitrate": source_bitrate,
                "requestedBitrate": requested_bitrate,
                "worker": (
                    {
                        "sessionId": worker_session_id,
                        "state": worker_state,
                        "processAlive": bool(worker_process_id),
                    }
                    if worker_session_id
                    else None
                ),
            }
        return value

    def list_sessions(self, user_id: str | None = None) -> dict:
        self._expire_stale()
        where = " WHERE v.state='active'"
        params: list[object] = []
        if user_id:
            where += " AND v.user_id=?"
            params.append(user_id)
        rows = self.db.read_execute(
            self._select_sql() + where + " ORDER BY v.last_heartbeat_at DESC",
            params,
        )
        return {"sessions": [self._payload(row) for row in rows], "updatedAt": _iso()}

    def get_session(self, viewer_id: str) -> dict | None:
        self._expire_stale()
        rows = self.db.read_execute(self._select_sql() + " WHERE v.id=?", (viewer_id,))
        return self._payload(rows[0], detail=True) if rows else None

    def heartbeat(
        self,
        user_id: str,
        auth_session_id: str,
        viewer_id: str,
        payload: dict,
        ip_address: str | None = None,
    ) -> dict:
        if not self.available():
            raise LookupError("Playback viewer support is unavailable.")
        self._expire_stale_if_due()
        now = _iso()
        position = payload.get("positionSeconds", 0)
        duration = payload.get("durationSeconds")
        try:
            position = max(0.0, float(position))
        except (TypeError, ValueError):
            position = 0.0
        try:
            duration = max(0.0, float(duration)) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        acks = payload.get("commandAcks") or []
        if not isinstance(acks, list):
            acks = []
        with self.db.transaction() as cursor:
            row = cursor.execute(
                "SELECT device_id,worker_session_id,state FROM playback_viewer_sessions WHERE id=? AND user_id=? AND auth_session_id=?",
                (viewer_id, user_id, auth_session_id),
            ).fetchone()
            if not row or row[2] != "active":
                raise LookupError("Playback viewer is no longer active.")
            persist_heartbeat = self._should_persist_heartbeat(viewer_id, bool(acks))
            for ack in acks[:32]:
                command_id = ack.get("id") if isinstance(ack, dict) else ack
                if not command_id:
                    continue
                success = (
                    True
                    if not isinstance(ack, dict)
                    else bool(ack.get("success", True))
                )
                cursor.execute(
                    "UPDATE playback_viewer_commands SET state=?,acknowledged_at=?,error=? WHERE id=? AND viewer_session_id=? AND state IN ('pending','delivered')",
                    (
                        "acknowledged" if success else "failed",
                        now,
                        _clean(ack.get("error"), 200)
                        if isinstance(ack, dict)
                        else None,
                        str(command_id),
                        viewer_id,
                    ),
                )
            if persist_heartbeat:
                cursor.execute(
                    "UPDATE playback_viewer_sessions SET position_seconds=?,duration_seconds=COALESCE(?,duration_seconds),paused=?,last_heartbeat_at=?,worker_session_id=COALESCE(?,worker_session_id) WHERE id=?",
                    (
                        position,
                        duration,
                        int(bool(payload.get("paused"))),
                        now,
                        _clean(payload.get("workerSessionId"), 80),
                        viewer_id,
                    ),
                )
                if row[0]:
                    cursor.execute(
                        "UPDATE user_devices SET last_seen_at=?,ip_address=COALESCE(?,ip_address) WHERE id=? AND user_id=?",
                        (now, _clean(ip_address, 80), row[0], user_id),
                    )
            commands = cursor.execute(
                "SELECT id,action,issued_at FROM playback_viewer_commands WHERE viewer_session_id=? AND state IN ('pending','delivered') AND expires_at>? ORDER BY issued_at LIMIT 8",
                (viewer_id, now),
            ).fetchall()
            cursor.executemany(
                "UPDATE playback_viewer_commands SET state='delivered',delivered_at=COALESCE(delivered_at,?) WHERE id=? AND state='pending'",
                [(now, command[0]) for command in commands],
            )
        return {
            "viewerSessionId": viewer_id,
            "serverTime": now,
            "commands": [
                {"id": command[0], "action": command[1], "issuedAt": command[2]}
                for command in commands
            ],
        }

    def end_viewer(self, user_id: str, auth_session_id: str, viewer_id: str) -> dict:
        if not self.available():
            return {"viewerSessionId": viewer_id, "stopWorker": False}
        now = _iso()
        with self.db.transaction() as cursor:
            row = cursor.execute(
                "SELECT worker_session_id,state FROM playback_viewer_sessions WHERE id=? AND user_id=? AND auth_session_id=?",
                (viewer_id, user_id, auth_session_id),
            ).fetchone()
            if not row:
                raise LookupError("Playback viewer not found.")
            cursor.execute(
                "UPDATE playback_viewer_sessions SET state='ended',ended_at=?,last_heartbeat_at=? WHERE id=? AND state='active'",
                (now, now, viewer_id),
            )
            cursor.execute(
                "UPDATE playback_viewer_commands SET state='acknowledged',acknowledged_at=? WHERE viewer_session_id=? AND action='stop' AND state='delivered'",
                (now, viewer_id),
            )
            cursor.execute(
                "UPDATE playback_viewer_commands SET state='expired' WHERE viewer_session_id=? AND state IN ('pending','delivered')",
                (viewer_id,),
            )
            worker_id = row[0]
            remaining = (
                cursor.execute(
                    "SELECT COUNT(*) FROM playback_viewer_sessions WHERE worker_session_id=? AND state='active'",
                    (worker_id,),
                ).fetchone()[0]
                if worker_id
                else 0
            )
        return {
            "viewerSessionId": viewer_id,
            "workerSessionId": worker_id,
            "stopWorker": bool(worker_id and not remaining),
        }

    def issue_command(self, viewer_id: str, action: str) -> dict | None:
        if action not in {"pause", "resume", "stop"}:
            raise ValueError("Unsupported playback command.")
        self._expire_stale()
        now = _now()
        expires = now + timedelta(seconds=self.COMMAND_TTL_SECONDS)
        command_id = str(uuid.uuid4())
        result = self.db.execute(
            "INSERT INTO playback_viewer_commands(id,viewer_session_id,action,state,issued_at,expires_at) SELECT ?,?,'"
            + action
            + "','pending',?,? WHERE EXISTS (SELECT 1 FROM playback_viewer_sessions WHERE id=? AND state='active')",
            (command_id, viewer_id, _iso(now), _iso(expires), viewer_id),
        )
        if not result and not self.db.read_execute(
            "SELECT 1 FROM playback_viewer_commands WHERE id=?", (command_id,)
        ):
            return None
        return {
            "id": command_id,
            "viewerSessionId": viewer_id,
            "action": action,
            "state": "pending",
            "expiresAt": _iso(expires),
        }

    def list_devices(self, user_id: str | None = None) -> dict:
        self._expire_stale()
        where = ""
        params: list[object] = []
        if user_id:
            where = " WHERE d.user_id=?"
            params.append(user_id)
        rows = self.db.read_execute(
            """
            SELECT d.id,d.user_id,u.username,d.device_key,d.device_type,d.browser,
                   d.operating_system,d.device_name,d.client_name,d.client_version,
                   d.ip_address,d.first_seen_at,d.last_seen_at,
                   v.id,v.entity_id,v.position_seconds,v.duration_seconds,v.paused,
                   v.last_heartbeat_at,e.entity_type,e.relative_path
              FROM user_devices d JOIN users u ON u.id=d.user_id
              LEFT JOIN playback_viewer_sessions v ON v.device_id=d.id AND v.state='active'
              LEFT JOIN library_entities e ON e.id=v.entity_id
            """
            + where
            + " ORDER BY d.last_seen_at DESC,v.last_heartbeat_at DESC",
            params,
        )
        grouped: dict[str, dict] = {}
        for row in rows:
            device_id = row[0]
            value = grouped.setdefault(
                device_id,
                {
                    "id": device_id,
                    "user": {"id": row[1], "username": row[2]},
                    "deviceKey": row[3],
                    "type": row[4] or "unknown",
                    "browser": row[5],
                    "operatingSystem": row[6],
                    "name": row[7],
                    "clientName": row[8],
                    "clientVersion": row[9],
                    "ipAddress": row[10],
                    "firstSeenAt": row[11],
                    "lastActiveAt": row[12],
                    "active": False,
                    "nowPlaying": None,
                },
            )
            if row[13] and not value["active"]:
                value["active"] = True
                value["lastActiveAt"] = row[19] or value["lastActiveAt"]
                value["nowPlaying"] = {
                    "viewerSessionId": row[13],
                    "itemId": row[14],
                    "title": _title(row[20], row[19]),
                    "positionSeconds": float(row[15] or 0),
                    "durationSeconds": float(row[16]) if row[16] is not None else None,
                    "paused": bool(row[17]),
                    "subtitle": None,
                }
        return {"devices": list(grouped.values()), "updatedAt": _iso()}

    def remove_device(self, device_id: str) -> dict:
        if not self.available():
            return {"deviceId": device_id, "workers": []}
        self._expire_stale()
        workers: list[tuple[str, str]] = []
        with self.db.transaction() as cursor:
            active_rows = cursor.execute(
                "SELECT user_id,worker_session_id FROM playback_viewer_sessions WHERE device_id=? AND state='active' AND worker_session_id IS NOT NULL",
                (device_id,),
            ).fetchall()
            for user_id, worker_id in active_rows:
                count = cursor.execute(
                    "SELECT COUNT(*) FROM playback_viewer_sessions WHERE worker_session_id=? AND state='active' AND device_id<>?",
                    (worker_id, device_id),
                ).fetchone()[0]
                if not count:
                    workers.append((user_id, worker_id))
            cursor.execute(
                "UPDATE playback_viewer_sessions SET state='ended',ended_at=? WHERE device_id=? AND state='active'",
                (_iso(), device_id),
            )
            cursor.execute(
                "UPDATE playback_viewer_commands SET state='expired' WHERE viewer_session_id IN (SELECT id FROM playback_viewer_sessions WHERE device_id=?) AND state IN ('pending','delivered')",
                (device_id,),
            )
            cursor.execute("DELETE FROM user_sessions WHERE device_id=?", (device_id,))
            deleted = cursor.execute(
                "DELETE FROM user_devices WHERE id=?", (device_id,)
            ).rowcount
        if not deleted:
            raise LookupError("Device not found.")
        return {"deviceId": device_id, "workers": workers}
