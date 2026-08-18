from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import Config
from app.images import WEBP_COMPRESSION_LEVEL, WEBP_QUALITY
from app.playback import ffmpeg_path, ffprobe_path

AVATAR_MAX_BYTES = 20 * 1024 * 1024
AVATAR_SIZE = 500
AVATAR_MAX_DIMENSION = 10_000
AVATAR_MAX_PIXELS = 100_000_000
AVATAR_FORMATS = {"jpeg", "png", "webp", "gif"}


class AvatarError(ValueError):
    """A user-correctable avatar upload or processing error."""


class AvatarTooLargeError(AvatarError):
    pass


class AvatarUnsupportedError(AvatarError):
    pass


@dataclass(frozen=True)
class AvatarCrop:
    crop_x: float
    crop_y: float
    crop_size: float
    rotation: int


@dataclass(frozen=True)
class _ValidatedCrop:
    crop_x: int
    crop_y: int
    crop_size: int
    rotation: int


@dataclass(frozen=True)
class _Probe:
    width: int
    height: int


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_from_bytes(content: bytes) -> str:
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    raise AvatarUnsupportedError("Unsupported avatar image format.")


def _format_extension(value: str) -> str:
    return "gif" if value == "gif" else "webp"


def _safe_user_directory(root: Path, user_id: str) -> Path:
    # User IDs are server-owned, but hashing keeps imported legacy IDs and
    # route parameters from ever becoming path components.
    directory = root / hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    resolved_root = root.resolve()
    resolved_directory = directory.resolve()
    try:
        resolved_directory.relative_to(resolved_root)
    except ValueError as error:
        raise AvatarError("Avatar storage path is invalid.") from error
    return resolved_directory


def _run_process(command: list[str], timeout: int, *, json_output: bool = False):
    try:
        return subprocess.run(
            command,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=json_output,
            encoding="utf-8" if json_output else None,
            errors="replace" if json_output else None,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AvatarError("Image processing is unavailable.") from error


def _probe_image(source: Path) -> _Probe:
    executable = ffprobe_path()
    if not executable:
        raise AvatarError("Image processing is unavailable.")
    completed = _run_process(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(source),
        ],
        30,
        json_output=True,
    )
    if completed.returncode != 0:
        raise AvatarError("The avatar image could not be decoded.")
    try:
        payload = json.loads(completed.stdout or "{}")
        stream = payload["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AvatarError("The avatar image has no usable dimensions.") from error
    if (
        width <= 0
        or height <= 0
        or width > AVATAR_MAX_DIMENSION
        or height > AVATAR_MAX_DIMENSION
        or width * height > AVATAR_MAX_PIXELS
    ):
        raise AvatarError("The avatar image dimensions are too large.")
    return _Probe(width, height)


def _validated_crop(crop: AvatarCrop, probe: _Probe) -> _ValidatedCrop:
    if crop.rotation not in {0, 90, 180, 270}:
        raise AvatarError("Avatar rotation is invalid.")
    values = (crop.crop_x, crop.crop_y, crop.crop_size)
    if not all(math.isfinite(value) for value in values) or crop.crop_size <= 0:
        raise AvatarError("Avatar crop values are invalid.")

    rotated_width, rotated_height = (
        (probe.height, probe.width)
        if crop.rotation in {90, 270}
        else (probe.width, probe.height)
    )
    crop_size = int(round(crop.crop_size))
    crop_x = int(round(crop.crop_x))
    crop_y = int(round(crop.crop_y))
    if crop_size <= 0 or crop_size > min(rotated_width, rotated_height):
        raise AvatarError("Avatar crop is outside the source image.")

    # Browser layout math can differ from FFmpeg by a fractional pixel. Allow
    # one pixel of tolerance, then normalize the final crop to a safe rectangle.
    if (
        crop_x < -1
        or crop_y < -1
        or crop_x + crop_size > rotated_width + 1
        or crop_y + crop_size > rotated_height + 1
    ):
        raise AvatarError("Avatar crop is outside the source image.")
    crop_x = max(0, min(rotated_width - crop_size, crop_x))
    crop_y = max(0, min(rotated_height - crop_size, crop_y))
    return _ValidatedCrop(crop_x, crop_y, crop_size, crop.rotation)


def avatar_filter(crop: _ValidatedCrop) -> str:
    rotation = {
        0: (),
        90: ("transpose=1",),
        180: ("hflip", "vflip"),
        270: ("transpose=2",),
    }[crop.rotation]
    return ",".join(
        [
            *rotation,
            f"crop={crop.crop_size}:{crop.crop_size}:{crop.crop_x}:{crop.crop_y}",
            f"scale={AVATAR_SIZE}:{AVATAR_SIZE}:flags=lanczos",
        ]
    )


def _encode_avatar(
    source: Path, target: Path, image_format: str, crop: _ValidatedCrop
) -> None:
    executable = ffmpeg_path()
    if not executable:
        raise AvatarError("Image processing is unavailable.")
    target.parent.mkdir(parents=True, exist_ok=True)
    filter_value = avatar_filter(crop)
    if image_format == "gif":
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            f"[0:v]{filter_value},split[avatar][palette_source];"
            "[palette_source]palettegen=stats_mode=diff[palette];"
            "[avatar][palette]paletteuse=dither=sierra2_4a",
            "-loop",
            "0",
            "-f",
            "gif",
            str(target),
        ]
    else:
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            filter_value,
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            "-quality",
            str(WEBP_QUALITY),
            "-compression_level",
            str(WEBP_COMPRESSION_LEVEL),
            str(target),
        ]
    completed = _run_process(command, 120)
    if completed.returncode != 0 or not target.is_file() or not target.stat().st_size:
        raise AvatarError("The avatar image could not be processed.")
    output_probe = _probe_image(target)
    if output_probe.width != AVATAR_SIZE or output_probe.height != AVATAR_SIZE:
        raise AvatarError("The processed avatar has an invalid size.")


class UserAvatarStore:
    def __init__(self, db=None):
        self.db = db or Config().database
        db_file = getattr(self.db, "db_file", None)
        self.root = (
            Path(db_file).parent / "avatar-cache"
            if db_file and db_file != ":memory:"
            else None
        )

    def _has_table(self) -> bool:
        try:
            rows = self.db.read_execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_avatars'"
            )
        except Exception:
            return False
        return bool(rows and isinstance(rows[0], (tuple, list)))

    def _record(self, user_id: str):
        if not self._has_table():
            return None
        rows = self.db.read_execute(
            "SELECT version,file_format FROM user_avatars WHERE user_id=?",
            (user_id,),
        )
        return rows[0] if rows else None

    def version(self, user_id: str) -> str | None:
        record = self._record(user_id)
        return str(record[0]) if record else None

    def _path(self, user_id: str, version: str, file_format: str) -> Path | None:
        if self.root is None or file_format not in {"webp", "gif"}:
            return None
        directory = _safe_user_directory(self.root, user_id)
        path = (directory / f"{version}.{file_format}").resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as error:
            raise AvatarError("Avatar storage path is invalid.") from error
        return path

    def resolve(self, user_id: str, requested_version: str | None = None):
        record = self._record(user_id)
        if not record:
            return None
        version, file_format = str(record[0]), str(record[1])
        if requested_version and requested_version != version:
            return None
        path = self._path(user_id, version, file_format)
        if path is None or not path.is_file() or not path.stat().st_size:
            return None
        return path, version, file_format

    def save(
        self, user_id: str, content: bytes, content_type: str | None, crop: AvatarCrop
    ) -> str:
        del content_type  # The bytes are authoritative; MIME is advisory only.
        if len(content) > AVATAR_MAX_BYTES:
            raise AvatarTooLargeError("Avatar file is too large.")
        if not content:
            raise AvatarError("Avatar file is empty.")
        image_format = _format_from_bytes(content)
        if image_format not in AVATAR_FORMATS:
            raise AvatarUnsupportedError("Unsupported avatar image format.")
        if self.root is None or not self._has_table():
            raise AvatarError("Avatar storage is unavailable.")

        self.root.mkdir(parents=True, exist_ok=True)
        directory = _safe_user_directory(self.root, user_id)
        directory.mkdir(parents=True, exist_ok=True)
        old_record = self._record(user_id)
        version = uuid.uuid4().hex
        output_format = _format_extension(image_format)
        target = self._path(user_id, version, output_format)
        if target is None:
            raise AvatarError("Avatar storage is unavailable.")
        input_path: Path | None = None
        output_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory,
                prefix=".avatar-input-",
                suffix=f".{image_format}",
                delete=False,
            ) as handle:
                input_path = Path(handle.name)
                handle.write(content)
            probe = _probe_image(input_path)
            validated_crop = _validated_crop(crop, probe)
            output_path = directory / f".{version}.{output_format}"
            _encode_avatar(input_path, output_path, image_format, validated_crop)
            output_path.replace(target)
            try:
                now = _iso_now()
                with self.db.transaction() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO user_avatars(user_id,version,file_format,created_at,updated_at)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            version=excluded.version,
                            file_format=excluded.file_format,
                            updated_at=excluded.updated_at
                        """,
                        (user_id, version, output_format, now, now),
                    )
            except Exception:
                target.unlink(missing_ok=True)
                raise
        finally:
            if input_path is not None:
                input_path.unlink(missing_ok=True)
            if output_path is not None:
                output_path.unlink(missing_ok=True)

        if old_record:
            old_path = self._path(user_id, str(old_record[0]), str(old_record[1]))
            if old_path and old_path != target:
                old_path.unlink(missing_ok=True)
        return version

    def remove(self, user_id: str) -> None:
        record = self._record(user_id)
        if not record:
            return
        old_path = self._path(user_id, str(record[0]), str(record[1]))
        if self._has_table():
            self.db.execute("DELETE FROM user_avatars WHERE user_id=?", (user_id,))
        if old_path:
            old_path.unlink(missing_ok=True)

    def remove_path_for_deleted_user(self, user_id: str, record) -> None:
        if not record:
            return
        path = self._path(user_id, str(record[0]), str(record[1]))
        if path:
            path.unlink(missing_ok=True)

    def record_for_cleanup(self, user_id: str):
        return self._record(user_id)
