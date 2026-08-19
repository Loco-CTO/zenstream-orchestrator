from __future__ import annotations

import hashlib
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Config
from app.images import (
    WEBP_COMPRESSION_LEVEL,
    WEBP_QUALITY,
    blurhash_for_image,
)
from app.logging_config import get_logger
from app.playback import PLAYABLE_ROLE, ffmpeg_path

logger = get_logger("screen_extractor")
SCREEN_EXTRACTOR_PROVIDER = "screen_extractor"
SCREEN_EXTRACTOR_VERSION = 1
SCREEN_EXTRACTOR_SEEK_FRACTION = 0.25
SCREEN_EXTRACTOR_MAX_DIMENSION = 1920
SCREEN_EXTRACTOR_TIMEOUT_SECONDS = 120.0
ELIGIBLE_ENTITY_TYPES = {"movie", "episode"}
_RETRY_DELAYS_SECONDS = (300, 3600, 21600, 86400)


def eligible(entity_type: str) -> bool:
    return entity_type in ELIGIBLE_ENTITY_TYPES


def _provider_primary_ready_for_all_locales(
    db, entity_id: str, entity_type: str
) -> bool:
    """Return true only when every configured locale has a usable remote Primary."""
    try:
        from app.models.metadata import MetadataLanguageSettings

        locales = MetadataLanguageSettings().get()
    except Exception:
        locales = ["en"]
    # Local Primary artwork remains the highest-priority source.
    local_names = {"poster", "folder", "cover", "primary", "tvshow", "movie", "season"}
    local_rows = db.execute(
        "SELECT relative_path,quick_fingerprint FROM media_files WHERE entity_id=? AND role='image'",
        (entity_id,),
    )
    if any(
        Path(str(path or "")).stem.casefold() in local_names and fingerprint
        for path, fingerprint in local_rows
    ):
        return True
    try:
        from app.metadata_services import MetadataReadService

        identities = [
            {"provider": row[0], "id": str(row[1])}
            for row in db.execute(
                "SELECT provider,provider_id FROM entity_provider_ids WHERE entity_id=?",
                (entity_id,),
            )
        ]
        reader = MetadataReadService(db)
    except Exception:
        return False
    for locale in locales:
        raw = reader.resolve_raw(entity_type, identities, locale)
        if not reader.ready_artwork(
            entity_type,
            identities,
            raw.get("images", []),
            locale,
            "Primary",
            raw.get("originalLanguage"),
            reader.providers(entity_type),
        ):
            return False
    return True


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ready_file(value: object) -> bool:
    try:
        path = Path(str(value)) if value else None
        return bool(path and path.is_file() and path.stat().st_size > 0)
    except OSError:
        return False


def _content_version(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:12]


class ScreenExtractionError(RuntimeError):
    code = "screen_extraction_failed"


class ScreenExtractionCancelled(ScreenExtractionError):
    code = "cancelled"


class ScreenExtractionPermanentError(ScreenExtractionError):
    pass


class ScreenExtractorStore:
    _ASSET_COLUMN_NAMES = (
        "entity_id",
        "desired_media_file_id",
        "desired_source_fingerprint",
        "desired_output_key",
        "extraction_version",
        "generation",
        "state",
        "ready_media_file_id",
        "ready_source_fingerprint",
        "local_path",
        "blur_hash",
        "version",
        "width",
        "height",
        "seek_seconds",
        "attempt_count",
        "next_attempt_at",
        "last_error_code",
        "last_error",
        "created_at",
        "updated_at",
    )
    _ASSET_COLUMNS = (
        "entity_id,desired_media_file_id,desired_source_fingerprint,"
        "desired_output_key,extraction_version,generation,state,"
        "ready_media_file_id,ready_source_fingerprint,local_path,blur_hash,"
        "version,width,height,seek_seconds,attempt_count,next_attempt_at,"
        "last_error_code,last_error,created_at,updated_at"
    )

    def __init__(self, db=None):
        self.db = db or Config().database

    @staticmethod
    def source_fingerprint(
        quick_fingerprint: object, size: object, modified_ns: object
    ) -> str:
        return str(quick_fingerprint or f"{int(size or 0)}:{int(modified_ns or 0)}")

    @staticmethod
    def output_key(
        source_fingerprint: str,
        *,
        extraction_version: int = SCREEN_EXTRACTOR_VERSION,
        seek_fraction: float = SCREEN_EXTRACTOR_SEEK_FRACTION,
        max_dimension: int = SCREEN_EXTRACTOR_MAX_DIMENSION,
    ) -> str:
        return hashlib.sha256(
            (
                f"{source_fingerprint}:{extraction_version}:"
                f"{seek_fraction:.8f}:{max_dimension}"
            ).encode()
        ).hexdigest()

    @classmethod
    def _asset(cls, row) -> dict | None:
        if not row:
            return None
        return {
            "entityId": row[0],
            "mediaFileId": row[1],
            "fingerprint": row[2],
            "outputKey": row[3],
            "extractionVersion": int(row[4]),
            "generation": int(row[5]),
            "state": row[6],
            "readyMediaFileId": row[7],
            "readyFingerprint": row[8],
            "localPath": row[9],
            "blurHash": row[10],
            "version": row[11],
            "width": row[12],
            "height": row[13],
            "seekSeconds": row[14],
            "attemptCount": int(row[15] or 0),
            "nextAttemptAt": row[16],
            "lastErrorCode": row[17],
            "lastError": row[18],
            "createdAt": row[19],
            "updatedAt": row[20],
        }

    def get(self, entity_id: str) -> dict | None:
        rows = self.db.execute(
            f"SELECT {self._ASSET_COLUMNS} FROM screen_extractor_assets "
            "WHERE entity_id=?",
            (entity_id,),
        )
        return self._asset(rows[0]) if rows else None

    def ready(self, entity_id: str) -> dict | None:
        asset = self.get(entity_id)
        if not asset or not _ready_file(asset.get("localPath")):
            return None
        return asset

    def source(self, entity_id: str) -> dict | None:
        rows = self.db.execute(
            "SELECT f.id,f.quick_fingerprint,f.size,f.modified_ns,s.duration_seconds,"
            "s.width,s.height,l.directory,f.relative_path,e.entity_type "
            "FROM media_files f "
            "JOIN media_sources s ON s.media_file_id=f.id AND s.entity_id=f.entity_id "
            "JOIN library_entities e ON e.id=f.entity_id "
            "JOIN libraries l ON l.id=e.library_id "
            "WHERE f.entity_id=? AND f.role=? "
            "AND e.entity_type IN ('movie','episode') "
            "AND s.video_codec IS NOT NULL AND s.video_codec<>'' "
            "ORDER BY f.size DESC,f.relative_path COLLATE NOCASE,f.id LIMIT 1",
            (entity_id, PLAYABLE_ROLE),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "mediaFileId": row[0],
            "fingerprint": self.source_fingerprint(row[1], row[2], row[3]),
            "durationSeconds": float(row[4] or 0),
            "sourceWidth": int(row[5] or 0),
            "sourceHeight": int(row[6] or 0),
            "directory": row[7],
            "relativePath": row[8],
            "entityType": row[9],
        }

    def reconcile(
        self,
        entity_id: str,
        *,
        needed: bool = True,
        force: bool = False,
    ) -> dict | None:
        if not needed:
            asset = self.get(entity_id)
            if asset and asset["state"] in {"queued", "generating", "retry"}:
                retained_state = (
                    "ready" if _ready_file(asset.get("localPath")) else "failed"
                )
                with self.db.transaction() as cursor:
                    cursor.execute(
                        "UPDATE screen_extractor_assets SET generation=generation+1,"
                        "state=?,next_attempt_at=NULL,last_error_code=?,last_error=?,"
                        "updated_at=? WHERE entity_id=? AND generation=?",
                        (
                            retained_state,
                            "higher_priority_available",
                            "A higher-priority Primary artwork source is ready.",
                            now(),
                            entity_id,
                            asset["generation"],
                        ),
                    )
            return self.get(entity_id)
        source = self.source(entity_id)
        if not source:
            return self.get(entity_id)
        fingerprint = source["fingerprint"]
        output_key = self.output_key(fingerprint)
        timestamp = now()
        with self.db.transaction() as cursor:
            current = cursor.execute(
                f"SELECT {self._ASSET_COLUMNS} FROM screen_extractor_assets "
                "WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
            if current is None:
                cursor.execute(
                    "INSERT INTO screen_extractor_assets("
                    "entity_id,desired_media_file_id,desired_source_fingerprint,"
                    "desired_output_key,extraction_version,generation,state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,1,'queued',?,?)",
                    (
                        entity_id,
                        source["mediaFileId"],
                        fingerprint,
                        output_key,
                        SCREEN_EXTRACTOR_VERSION,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                asset = self._asset(current)
                changed = bool(
                    asset["mediaFileId"] != source["mediaFileId"]
                    or asset["fingerprint"] != fingerprint
                    or asset["outputKey"] != output_key
                    or asset["extractionVersion"] != SCREEN_EXTRACTOR_VERSION
                )
                ready_matches = bool(
                    asset["readyFingerprint"] == fingerprint
                    and _ready_file(asset.get("localPath"))
                )
                priority_was_blocking = (
                    asset["state"] == "failed"
                    and asset["lastErrorCode"] == "higher_priority_available"
                )
                if (
                    changed
                    or force
                    or priority_was_blocking
                    or not ready_matches
                    and asset["state"] == "ready"
                ):
                    cursor.execute(
                        "UPDATE screen_extractor_assets SET "
                        "desired_media_file_id=?,desired_source_fingerprint=?,"
                        "desired_output_key=?,extraction_version=?,generation=generation+1,"
                        "state='queued',attempt_count=0,next_attempt_at=NULL,"
                        "last_error_code=NULL,last_error=NULL,updated_at=? "
                        "WHERE entity_id=?",
                        (
                            source["mediaFileId"],
                            fingerprint,
                            output_key,
                            SCREEN_EXTRACTOR_VERSION,
                            timestamp,
                            entity_id,
                        ),
                    )
        return self.get(entity_id)

    def _claim_query(self, entity_id: str | None = None) -> tuple[str, list[object]]:
        timestamp = now()
        asset_columns = ",".join(f"a.{column}" for column in self._ASSET_COLUMN_NAMES)
        query = (
            f"SELECT {asset_columns},l.directory,f.relative_path,"
            "s.duration_seconds,s.width,s.height,e.entity_type "
            "FROM screen_extractor_assets a "
            "JOIN media_files f ON f.id=a.desired_media_file_id "
            "JOIN media_sources s ON s.media_file_id=f.id AND s.entity_id=f.entity_id "
            "JOIN library_entities e ON e.id=a.entity_id "
            "JOIN libraries l ON l.id=e.library_id "
            "WHERE a.state IN ('queued','retry') "
            "AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=?) "
            "AND e.entity_type IN ('movie','episode') "
            "AND f.role=? AND s.video_codec IS NOT NULL AND s.video_codec<>'' "
        )
        params: list[object] = [timestamp, PLAYABLE_ROLE]
        if entity_id is not None:
            query += "AND a.entity_id=? "
            params.append(entity_id)
        query += "ORDER BY a.updated_at,a.entity_id LIMIT 1"
        return query, params

    def claim_next(self, entity_id: str | None = None) -> dict | None:
        query, params = self._claim_query(entity_id)
        with self.db.transaction() as cursor:
            row = cursor.execute(query, params).fetchone()
            if not row:
                return None
            asset = self._asset(row[:21])
            cursor.execute(
                "UPDATE screen_extractor_assets SET state='generating',"
                "next_attempt_at=NULL,last_error_code=NULL,last_error=NULL,updated_at=? "
                "WHERE entity_id=? AND generation=? AND state IN ('queued','retry')",
                (now(), asset["entityId"], asset["generation"]),
            )
            if cursor.rowcount != 1:
                return None
        asset.update(
            {
                "state": "generating",
                "directory": row[21],
                "relativePath": row[22],
                "durationSeconds": float(row[23] or 0),
                "sourceWidth": int(row[24] or 0),
                "sourceHeight": int(row[25] or 0),
                "entityType": row[26],
            }
        )
        return asset

    def mark_ready(
        self,
        asset: dict,
        path: Path,
        *,
        version: str,
        blur_hash: str | None,
        width: int | None,
        height: int | None,
        seek_seconds: float,
    ) -> bool:
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE screen_extractor_assets SET state='ready',"
                "ready_media_file_id=desired_media_file_id,"
                "ready_source_fingerprint=desired_source_fingerprint,"
                "local_path=?,blur_hash=?,version=?,width=?,height=?,seek_seconds=?,"
                "attempt_count=0,next_attempt_at=NULL,last_error_code=NULL,last_error=NULL,"
                "updated_at=? WHERE entity_id=? AND generation=? "
                "AND desired_output_key=? AND state='generating'",
                (
                    str(path),
                    blur_hash,
                    version,
                    width,
                    height,
                    seek_seconds,
                    now(),
                    asset["entityId"],
                    asset["generation"],
                    asset["outputKey"],
                ),
            )
            return cursor.rowcount == 1

    def mark_retry(self, asset: dict, code: str, message: str) -> bool:
        attempt = int(asset.get("attemptCount") or 0) + 1
        delay = _RETRY_DELAYS_SECONDS[min(attempt - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
        next_attempt = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE screen_extractor_assets SET state='retry',attempt_count=?,"
                "next_attempt_at=?,last_error_code=?,last_error=?,updated_at=? "
                "WHERE entity_id=? AND generation=? AND state='generating'",
                (
                    attempt,
                    next_attempt,
                    code[:100],
                    message[-1000:],
                    now(),
                    asset["entityId"],
                    asset["generation"],
                ),
            )
            return cursor.rowcount == 1

    def mark_failed(self, asset: dict, code: str, message: str) -> bool:
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE screen_extractor_assets SET state='failed',attempt_count=attempt_count+1,"
                "next_attempt_at=NULL,last_error_code=?,last_error=?,updated_at=? "
                "WHERE entity_id=? AND generation=? AND state='generating'",
                (
                    code[:100],
                    message[-1000:],
                    now(),
                    asset["entityId"],
                    asset["generation"],
                ),
            )
            return cursor.rowcount == 1

    def requeue(self, asset: dict) -> bool:
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE screen_extractor_assets SET state='queued',next_attempt_at=NULL,"
                "last_error_code=NULL,last_error=NULL,updated_at=? "
                "WHERE entity_id=? AND generation=? AND state='generating'",
                (now(), asset["entityId"], asset["generation"]),
            )
            return cursor.rowcount == 1

    def recover_generating(self) -> int:
        result = self.db.execute(
            "UPDATE screen_extractor_assets SET state='queued',next_attempt_at=NULL,"
            "last_error_code=NULL,last_error=NULL,updated_at=? "
            "WHERE state='generating'",
            (now(),),
        )
        return max(0, int(getattr(result, "rowcount", 0) or 0))


class ScreenExtractor:
    def __init__(
        self,
        store: ScreenExtractorStore | None = None,
        *,
        max_dimension: int = SCREEN_EXTRACTOR_MAX_DIMENSION,
        timeout_seconds: float = SCREEN_EXTRACTOR_TIMEOUT_SECONDS,
        hasher: Callable[[Path], str] | None = None,
    ):
        self.store = store or ScreenExtractorStore()
        self.db = self.store.db
        self.max_dimension = max(320, min(4096, int(max_dimension)))
        self.timeout_seconds = max(15.0, min(600.0, float(timeout_seconds)))
        self.hasher = hasher or blurhash_for_image

    def cache_root(self) -> Path:
        db_file = getattr(self.db, "db_file", None)
        if not db_file or db_file == ":memory:":
            raise ScreenExtractionPermanentError(
                "Screen Extractor requires a persistent metadata path."
            )
        return Path(db_file).parent / "screen-extractor-cache"

    def output_path(self, asset: dict) -> Path:
        entity_id = str(asset["entityId"])
        output_key = str(asset["outputKey"])
        try:
            uuid.UUID(entity_id)
        except ValueError:
            if not entity_id or any(value in entity_id for value in ("/", "\\", "..")):
                raise ScreenExtractionPermanentError("Entity ID is not cache-safe.")
        if len(output_key) != 64 or any(
            character not in "0123456789abcdef" for character in output_key
        ):
            raise ScreenExtractionPermanentError("Output key is not cache-safe.")
        return self.cache_root() / entity_id / f"{output_key}.webp"

    @staticmethod
    def source_path(asset: dict) -> Path:
        try:
            root = Path(asset["directory"]).resolve(strict=True)
            candidate = root / str(asset["relativePath"])
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            raise ScreenExtractionError("Media source is unavailable.") from error
        if candidate.is_symlink() or not resolved.is_file():
            raise ScreenExtractionPermanentError("Media source is not a regular file.")
        return resolved

    def command(self, asset: dict, source: Path, target: Path) -> list[str]:
        executable = ffmpeg_path()
        if not executable:
            raise ScreenExtractionError("FFmpeg is not available.")
        duration = float(asset.get("durationSeconds") or 0)
        if duration <= 0:
            error = ScreenExtractionPermanentError(
                "Media duration is unavailable for frame extraction."
            )
            error.code = "duration_unavailable"
            raise error
        seek_seconds = duration * SCREEN_EXTRACTOR_SEEK_FRACTION
        dimension = self.max_dimension
        scale = (
            f"scale=w='if(gte(iw,ih),min(iw,{dimension}),-2)':"
            f"h='if(lt(iw,ih),min(ih,{dimension}),-2)'"
        )
        return [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-threads",
            "1",
            "-y",
            "-ss",
            f"{seek_seconds:.6f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-an",
            "-vf",
            scale,
            "-c:v",
            "libwebp",
            "-quality",
            str(WEBP_QUALITY),
            "-compression_level",
            str(WEBP_COMPRESSION_LEVEL),
            str(target),
        ]

    def _dimensions(self, asset: dict) -> tuple[int | None, int | None]:
        width = int(asset.get("sourceWidth") or 0)
        height = int(asset.get("sourceHeight") or 0)
        if width <= 0 or height <= 0:
            return None, None
        scale = min(1.0, self.max_dimension / max(width, height))
        output_width = max(2, int(width * scale) // 2 * 2)
        output_height = max(2, int(height * scale) // 2 * 2)
        return output_width, output_height

    @staticmethod
    def _stop(process: subprocess.Popen) -> tuple[str, str]:
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            return process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                return process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                # A broken child or test double must not hold the worker
                # forever after kill.  Closing the pipe handles releases the
                # Python-side resources even when the OS process is stuck.
                for stream in (
                    getattr(process, "stdout", None),
                    getattr(process, "stderr", None),
                ):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
                return "", "FFmpeg process did not exit after termination."

    def _generate(
        self, asset: dict, should_terminate: Callable[[], bool]
    ) -> tuple[Path, str, str | None, int | None, int | None, float]:
        source = self.source_path(asset)
        destination = self.output_path(asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.webp"
        )
        seek_seconds = (
            float(asset.get("durationSeconds") or 0) * SCREEN_EXTRACTOR_SEEK_FRACTION
        )
        try:
            process = subprocess.Popen(
                self.command(asset, source, temporary),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            started = time.monotonic()
            while True:
                try:
                    _stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if should_terminate():
                        self._stop(process)
                        raise ScreenExtractionCancelled(
                            "Screen extraction was cancelled."
                        )
                    if time.monotonic() - started >= self.timeout_seconds:
                        self._stop(process)
                        error = ScreenExtractionError(
                            "FFmpeg screen extraction timed out."
                        )
                        error.code = "ffmpeg_timeout"
                        raise error
            if process.returncode != 0:
                error = ScreenExtractionError(
                    (stderr or "FFmpeg screen extraction failed.").strip()[-1000:]
                )
                error.code = "ffmpeg_failed"
                raise error
            if not temporary.is_file() or not temporary.stat().st_size:
                error = ScreenExtractionError("FFmpeg did not produce a WebP image.")
                error.code = "empty_output"
                raise error
            blur_hash = self.hasher(temporary)
            version = _content_version(temporary)
            width, height = self._dimensions(asset)
            temporary.replace(destination)
            return destination, version, blur_hash, width, height, seek_seconds
        finally:
            temporary.unlink(missing_ok=True)

    def process(
        self, asset: dict, should_terminate: Callable[[], bool] | None = None
    ) -> dict:
        should_terminate = should_terminate or (lambda: False)
        started = time.monotonic()
        try:
            path, version, blur_hash, width, height, seek_seconds = self._generate(
                asset, should_terminate
            )
            published = self.store.mark_ready(
                asset,
                path,
                version=version,
                blur_hash=blur_hash,
                width=width,
                height=height,
                seek_seconds=seek_seconds,
            )
            state = "ready" if published else "stale"
            logger.info(
                "screen extraction complete entity_id=%s media_file_id=%s generation=%s "
                "state=%s seek_seconds=%.3f elapsed_seconds=%.1f",
                asset["entityId"],
                asset["mediaFileId"],
                asset["generation"],
                state,
                seek_seconds,
                time.monotonic() - started,
            )
            return {"state": state, "path": str(path), "version": version}
        except ScreenExtractionCancelled:
            self.store.requeue(asset)
            raise
        except ScreenExtractionPermanentError as error:
            self.store.mark_failed(asset, error.code, str(error))
            logger.warning(
                "screen extraction failed entity_id=%s media_file_id=%s generation=%s "
                "state=failed error_code=%s elapsed_seconds=%.1f",
                asset["entityId"],
                asset["mediaFileId"],
                asset["generation"],
                error.code,
                time.monotonic() - started,
            )
            return {"state": "failed", "errorCode": error.code, "error": str(error)}
        except Exception as error:
            code = getattr(error, "code", "screen_extraction_failed")
            self.store.mark_retry(asset, code, str(error))
            logger.warning(
                "screen extraction deferred entity_id=%s media_file_id=%s generation=%s "
                "state=retry error_code=%s elapsed_seconds=%.1f",
                asset["entityId"],
                asset["mediaFileId"],
                asset["generation"],
                code,
                time.monotonic() - started,
            )
            return {"state": "retry", "errorCode": code, "error": str(error)}

    def extract_entity(
        self,
        entity_id: str,
        *,
        needed: bool = True,
        force: bool = False,
        should_terminate: Callable[[], bool] | None = None,
    ) -> dict | None:
        asset = self.store.reconcile(entity_id, needed=needed, force=force)
        if not asset or not needed:
            return asset
        if (
            not force
            and asset["state"] == "ready"
            and asset["readyFingerprint"] == asset["fingerprint"]
            and _ready_file(asset.get("localPath"))
        ):
            return asset
        claimed = self.store.claim_next(entity_id)
        return (
            self.process(claimed, should_terminate)
            if claimed
            else self.store.get(entity_id)
        )


def ready_artwork(db, entity_id: str, entity_type: str) -> dict | None:
    if not eligible(entity_type):
        return None
    asset = ScreenExtractorStore(db).ready(entity_id)
    if not asset:
        return None
    return {
        "type": "Primary",
        "url": None,
        "language": None,
        "provider": SCREEN_EXTRACTOR_PROVIDER,
        "localPath": asset["localPath"],
        "blurHash": asset.get("blurHash"),
        "version": asset.get("version"),
        "width": asset.get("width") or 0,
        "height": asset.get("height") or 0,
        "seekSeconds": asset.get("seekSeconds") or 0,
    }


def extract_entity(
    db,
    entity_id: str,
    entity_type: str,
    *,
    force: bool = False,
    should_terminate: Callable[[], bool] | None = None,
):
    if not eligible(entity_type):
        return None
    needed = not _provider_primary_ready_for_all_locales(db, entity_id, entity_type)
    result = ScreenExtractor(ScreenExtractorStore(db)).extract_entity(
        entity_id,
        needed=needed,
        force=force,
        should_terminate=should_terminate,
    )
    if isinstance(result, dict) and result.get("state") in {"retry", "failed"}:
        raise ScreenExtractionError(result.get("error") or "Screen extraction failed")
    return result
