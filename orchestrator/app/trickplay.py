from __future__ import annotations

import hashlib
import inspect
import math
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.client_auth import issue_ticket
from app.config import Config
from app.images import WEBP_QUALITY
from app.logging_config import get_logger
from app.models.playback_settings import PlaybackSettings
from app.playback import PLAYABLE_ROLE, ffmpeg_path
from fastapi import HTTPException

SHEET_COLUMNS = 10
SHEET_ROWS = 10
FRAMES_PER_SHEET = SHEET_COLUMNS * SHEET_ROWS
logger = get_logger("trickplay")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TrickplayStore:
    def __init__(self, db=None):
        self.db = db or Config().database

    @staticmethod
    def fingerprint(quick_fingerprint, size, modified_ns) -> str:
        return str(quick_fingerprint or f"{int(size or 0)}:{int(modified_ns or 0)}")

    @staticmethod
    def output_key(fingerprint: str, width: int, height: int, interval: int) -> str:
        return hashlib.sha256(
            f"{fingerprint}:{width}x{height}:{interval}".encode()
        ).hexdigest()

    def queue_pending(
        self, library_id: str | None = None, settings: dict | None = None
    ) -> int:
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"media_files", "media_sources", "trickplay_assets"}.issubset(tables):
            return 0
        settings = settings or PlaybackSettings(self.db).get()
        width = settings["trickplayFrameWidth"]
        height = settings["trickplayFrameHeight"]
        interval = settings["trickplayIntervalSeconds"]
        params: list[str] = [PLAYABLE_ROLE]
        scope = ""
        if library_id:
            scope = " AND e.library_id=?"
            params.append(library_id)
        rows = self.db.execute(
            "SELECT f.id,f.entity_id,f.quick_fingerprint,f.size,f.modified_ns "
            "FROM media_files f "
            "JOIN library_entities e ON e.id=f.entity_id "
            "JOIN media_sources s ON s.media_file_id=f.id "
            "WHERE f.role=? AND s.video_codec IS NOT NULL AND s.video_codec<>''"
            + scope,
            params,
        )
        queued = 0
        for media_file_id, entity_id, quick_fingerprint, size, modified_ns in rows:
            fingerprint = self.fingerprint(quick_fingerprint, size, modified_ns)
            existing = self.db.execute(
                "SELECT source_fingerprint,frame_width,frame_height,interval_seconds,state "
                "FROM trickplay_assets WHERE media_file_id=?",
                (media_file_id,),
            )
            timestamp = now()
            if not existing:
                self.db.execute(
                    "INSERT INTO trickplay_assets(media_file_id,entity_id,source_fingerprint,frame_width,frame_height,interval_seconds,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        media_file_id,
                        entity_id,
                        fingerprint,
                        width,
                        height,
                        interval,
                        "queued",
                        timestamp,
                        timestamp,
                    ),
                )
                queued += 1
                continue
            old_fingerprint, old_width, old_height, old_interval, state = existing[0]
            if old_fingerprint != fingerprint:
                self.db.execute(
                    "UPDATE trickplay_assets SET entity_id=?,source_fingerprint=?,frame_width=?,frame_height=?,interval_seconds=?,"
                    "state='queued',output_key=NULL,error=NULL,updated_at=? WHERE media_file_id=?",
                    (
                        entity_id,
                        fingerprint,
                        width,
                        height,
                        interval,
                        timestamp,
                        media_file_id,
                    ),
                )
                queued += 1
        return queued

    def claim_next(self) -> dict | None:
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT a.media_file_id,a.entity_id,a.source_fingerprint,a.frame_width,a.frame_height,a.interval_seconds,"
                "s.duration_seconds,l.directory,f.relative_path "
                "FROM trickplay_assets a "
                "JOIN media_files f ON f.id=a.media_file_id "
                "JOIN media_sources s ON s.media_file_id=f.id "
                "JOIN library_entities e ON e.id=a.entity_id "
                "JOIN libraries l ON l.id=e.library_id "
                "WHERE a.state='queued' AND s.video_codec IS NOT NULL "
                "ORDER BY a.updated_at,a.media_file_id LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE trickplay_assets SET state='generating',error=NULL,updated_at=? "
                "WHERE media_file_id=? AND state='queued'",
                (now(), row[0]),
            )
            if cursor.rowcount != 1:
                return None
        return {
            "mediaFileId": row[0],
            "entityId": row[1],
            "fingerprint": row[2],
            "width": int(row[3]),
            "height": int(row[4]),
            "intervalSeconds": int(row[5]),
            "durationSeconds": float(row[6] or 0),
            "path": Path(row[7]) / row[8],
        }

    def mark_ready(self, asset: dict, sheets: list[dict]) -> str | None:
        output_key = self.output_key(
            asset["fingerprint"],
            asset["width"],
            asset["height"],
            asset["intervalSeconds"],
        )
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT source_fingerprint FROM trickplay_assets WHERE media_file_id=? AND state='generating'",
                (asset["mediaFileId"],),
            )
            current = cursor.fetchone()
            if not current or current[0] != asset["fingerprint"]:
                return None
            cursor.execute(
                "DELETE FROM trickplay_sheets WHERE media_file_id=?",
                (asset["mediaFileId"],),
            )
            cursor.executemany(
                "INSERT INTO trickplay_sheets(media_file_id,output_key,sheet_index,first_frame,frame_count,relative_path) "
                "VALUES(?,?,?,?,?,?)",
                [
                    (
                        asset["mediaFileId"],
                        output_key,
                        value["index"],
                        value["firstFrame"],
                        value["frameCount"],
                        value["relativePath"],
                    )
                    for value in sheets
                ],
            )
            cursor.execute(
                "UPDATE trickplay_assets SET state='ready',output_key=?,error=NULL,updated_at=? WHERE media_file_id=?",
                (output_key, now(), asset["mediaFileId"]),
            )
        return output_key

    def mark_failed(self, asset: dict, message: str) -> None:
        self.db.execute(
            "UPDATE trickplay_assets SET state='failed',error=?,updated_at=? "
            "WHERE media_file_id=? AND source_fingerprint=? AND state='generating'",
            (message[:1000], now(), asset["mediaFileId"], asset["fingerprint"]),
        )

    def recover_generating(self) -> int:
        result = self.db.execute(
            "UPDATE trickplay_assets SET state='queued',error=NULL,updated_at=? WHERE state='generating'",
            (now(),),
        )
        return (
            getattr(result, "rowcount", 0) if not isinstance(result, Exception) else 0
        )

    def requeue(self, asset: dict) -> bool:
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE trickplay_assets SET state='queued',error=NULL,updated_at=? "
                "WHERE media_file_id=? AND source_fingerprint=? AND state='generating'",
                (now(), asset["mediaFileId"], asset["fingerprint"]),
            )
            return cursor.rowcount == 1


class TrickplayExtractor:
    def __init__(self, store=None):
        self.store = store or TrickplayStore()
        self.db = self.store.db

    def cache_root(self) -> Path:
        return Path(self.db.db_file).parent / "trickplay-cache"

    @staticmethod
    def command(asset: dict, output_pattern: Path) -> list[str]:
        executable = ffmpeg_path()
        if not executable:
            raise RuntimeError("FFmpeg is not available.")
        width = asset["width"]
        height = asset["height"]
        interval = asset["intervalSeconds"]
        filter_graph = (
            f"fps=1/{interval},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"setsar=1,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"tile={SHEET_COLUMNS}x{SHEET_ROWS}:padding=0:margin=0"
        )
        return [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-y",
            "-i",
            str(asset["path"]),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            filter_graph,
            "-c:v",
            "libwebp",
            "-quality",
            str(WEBP_QUALITY),
            "-compression_level",
            "6",
            "-start_number",
            "0",
            str(output_pattern),
        ]

    def extract(self, asset: dict, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        if not asset["path"].is_file():
            raise RuntimeError("Media source is unavailable.")
        root = self.cache_root()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root, prefix=".tmp-") as temporary:
            temporary_root = Path(temporary)
            process = subprocess.Popen(
                self.command(asset, temporary_root / "sheet-%05d.webp"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            started = time.monotonic()
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if should_terminate() or time.monotonic() - started >= 3600:
                        process.terminate()
                        try:
                            stdout, stderr = process.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            stdout, stderr = process.communicate()
                        raise RuntimeError(
                            "Terminated by administrator"
                            if should_terminate()
                            else "FFmpeg trickplay extraction timed out."
                        )
            if process.returncode != 0:
                raise RuntimeError(
                    (stderr or "FFmpeg trickplay extraction failed.").strip()[-1000:]
                )
            images = sorted(temporary_root.glob("sheet-*.webp"))
            if not images:
                raise RuntimeError("FFmpeg did not produce trickplay sheets.")
            output_key = self.store.output_key(
                asset["fingerprint"],
                asset["width"],
                asset["height"],
                asset["intervalSeconds"],
            )
            media_root = root / asset["mediaFileId"]
            destination = media_root / output_key
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = media_root / f".{output_key}.tmp"
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(temporary_root, staging)
            shutil.rmtree(destination, ignore_errors=True)
            staging.replace(destination)
            expected_frames = max(
                1, int(math.ceil(asset["durationSeconds"] / asset["intervalSeconds"]))
            )
            sheets = [
                {
                    "index": index,
                    "firstFrame": index * FRAMES_PER_SHEET,
                    "frameCount": min(
                        FRAMES_PER_SHEET,
                        max(0, expected_frames - index * FRAMES_PER_SHEET),
                    ),
                    "relativePath": f"{asset['mediaFileId']}/{output_key}/{image.name}",
                }
                for index, image in enumerate(images)
            ]
            if self.store.mark_ready(asset, sheets):
                self._remove_old_outputs(media_root, output_key)

    def _remove_old_outputs(self, media_root: Path, current_key: str) -> None:
        try:
            root = self.cache_root().resolve()
            resolved = media_root.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return
        for child in resolved.iterdir() if resolved.is_dir() else []:
            if (
                child.name != current_key
                and child.is_dir()
                and not child.name.startswith(".")
            ):
                shutil.rmtree(child, ignore_errors=True)

    def remove_orphan_cache(self) -> None:
        root = self.cache_root()
        if not root.is_dir():
            return
        candidates = [
            child
            for child in root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ]
        if not candidates:
            return
        placeholders = ",".join("?" for _ in candidates)
        rows = self.db.execute(
            f"SELECT id FROM media_files WHERE id IN ({placeholders})",
            [child.name for child in candidates],
        )
        existing = {row[0] for row in rows}
        resolved_root = root.resolve()
        for child in candidates:
            if child.name in existing:
                continue
            try:
                resolved = child.resolve()
                resolved.relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            shutil.rmtree(resolved, ignore_errors=True)

    def run(self, run_id: str, job_store, should_terminate=None) -> None:
        should_terminate = should_terminate or (lambda: False)
        self.remove_orphan_cache()
        self.store.recover_generating()
        settings = PlaybackSettings(self.store.db).get()
        discovered = self.store.queue_pending(settings=settings)
        workers = settings["trickplayWorkers"]
        job_store.update_run(
            run_id,
            state="running",
            started_at=now(),
            message="Extracting trickplay sheets",
        )
        completed = 0
        failures = []
        progress_lock = Lock()

        def process_assets():
            nonlocal completed
            while not should_terminate():
                asset = self.store.claim_next()
                if not asset:
                    return
                try:
                    extractor = self.extract
                    if len(inspect.signature(extractor).parameters) >= 2:
                        extractor(asset, should_terminate)
                    else:
                        extractor(asset)
                    with progress_lock:
                        completed += 1
                        current = completed
                        job_store.update_run(
                            run_id,
                            progress_current=current,
                            progress_total=max(current, discovered),
                            message=f"Extracted {current} trickplay assets",
                        )
                except Exception as error:
                    if should_terminate():
                        self.store.requeue(asset)
                        return
                    self.store.mark_failed(asset, str(error))
                    with progress_lock:
                        failures.append(asset["mediaFileId"])
                    logger.warning(
                        "trickplay extraction failed entity_id=%s media_file_id=%s error=%s",
                        asset["entityId"],
                        asset["mediaFileId"],
                        error,
                    )

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="trickplay"
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
            summary = f"Extracted {completed} trickplay assets; {len(failures)} failed"
            job_store.update_run(
                run_id,
                state="failed",
                progress_current=completed,
                progress_total=max(completed + len(failures), discovered),
                finished_at=now(),
                message=summary,
                error=summary,
            )
        else:
            job_store.update_run(
                run_id,
                state="completed",
                progress_current=completed,
                progress_total=max(completed, discovered),
                finished_at=now(),
                message=f"Extracted {completed} trickplay assets"
                if completed
                else "Trickplay sheets are current",
            )

    def manifest(
        self,
        user_id: str,
        entity_id: str,
        source_id: str | None = None,
        auth_session_id: str | None = None,
    ) -> dict:
        rows = self.db.execute(
            "SELECT s.id,s.media_file_id,s.duration_seconds FROM media_sources s "
            "JOIN media_files f ON f.id=s.media_file_id WHERE s.entity_id=? AND f.role=?"
            + (" AND s.id=?" if source_id else "")
            + " ORDER BY f.size DESC LIMIT 1",
            [entity_id, PLAYABLE_ROLE, source_id]
            if source_id
            else [entity_id, PLAYABLE_ROLE],
        )
        if not rows:
            raise HTTPException(404, "Trickplay source not found.")
        selected_source_id, media_file_id, duration_seconds = rows[0]
        asset_rows = self.db.execute(
            "SELECT frame_width,frame_height,interval_seconds,state,output_key,error FROM trickplay_assets WHERE media_file_id=?",
            (media_file_id,),
        )
        if not asset_rows:
            raise HTTPException(404, "Trickplay is unavailable.")
        width, height, interval, state, output_key, error = asset_rows[0]
        base = {
            "state": state,
            "sourceId": selected_source_id,
            "mediaFileId": media_file_id,
            "frameWidth": width,
            "frameHeight": height,
            "intervalSeconds": interval,
            "columns": SHEET_COLUMNS,
            "rows": SHEET_ROWS,
            "durationSeconds": duration_seconds or 0,
        }
        if state != "ready" or not output_key:
            if state == "failed":
                raise HTTPException(
                    422, {**base, "detail": error or "Trickplay extraction failed."}
                )
            return base
        rows = self.db.execute(
            "SELECT sheet_index,first_frame,frame_count FROM trickplay_sheets "
            "WHERE media_file_id=? AND output_key=? ORDER BY sheet_index",
            (media_file_id, output_key),
        )
        claims = {"entity": entity_id}
        if auth_session_id:
            claims["sessionId"] = auth_session_id
        ticket = issue_ticket(user_id, "resource", 15 * 60, **claims)
        return {
            **base,
            "generation": output_key,
            "frameCount": sum(value[2] for value in rows),
            "sheets": [
                {
                    "index": index,
                    "firstFrame": first_frame,
                    "frameCount": frame_count,
                    "url": f"/api/playback/items/{entity_id}/trickplay/{output_key}/{index}.webp?access={ticket}",
                }
                for index, first_frame, frame_count in rows
            ],
        }

    def sheet_path(self, entity_id: str, generation: str, index: int) -> Path:
        if len(generation) != 64 or any(
            char not in "0123456789abcdef" for char in generation
        ):
            raise HTTPException(404, "Trickplay sheet not found.")
        rows = self.db.execute(
            "SELECT s.relative_path FROM trickplay_sheets s JOIN trickplay_assets a ON a.media_file_id=s.media_file_id "
            "WHERE a.entity_id=? AND a.state='ready' AND s.output_key=? AND s.sheet_index=?",
            (entity_id, generation, index),
        )
        if not rows:
            raise HTTPException(404, "Trickplay sheet not found.")
        root = self.cache_root().resolve()
        try:
            path = (root / rows[0][0]).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            raise HTTPException(404, "Trickplay sheet not found.") from None
        if not path.is_file():
            raise HTTPException(404, "Trickplay sheet not found.")
        return path
