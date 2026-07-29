from __future__ import annotations

import subprocess
import tempfile
import uuid
import math
from pathlib import Path

WEBP_QUALITY = 85
_BASE83 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"


def _base83(value: int, length: int) -> str:
    encoded = ""
    for exponent in range(length - 1, -1, -1):
        digit = (value // (83**exponent)) % 83
        encoded += _BASE83[digit]
    return encoded


def _srgb_to_linear(value: int) -> float:
    normalized = value / 255
    return normalized / 12.92 if normalized <= 0.04045 else ((normalized + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> int:
    value = max(0.0, min(1.0, value))
    normalized = value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    return int(max(0, min(255, normalized * 255 + 0.5)))


def _sign_pow(value: float, exponent: float) -> float:
    return math.copysign(abs(value) ** exponent, value)


def encode_blurhash(pixels: bytes, width: int, height: int, components_x: int = 4, components_y: int = 3) -> str:
    if width <= 0 or height <= 0 or len(pixels) != width * height * 3:
        raise ValueError("BlurHash pixels must be a complete RGB image.")
    linear = [_srgb_to_linear(value) for value in pixels]
    factors: list[tuple[float, float, float]] = []
    for y_component in range(components_y):
        for x_component in range(components_x):
            normal = 1 if x_component == 0 and y_component == 0 else 2
            red = green = blue = 0.0
            for y in range(height):
                for x in range(width):
                    basis = math.cos(math.pi * x_component * x / width) * math.cos(math.pi * y_component * y / height)
                    index = 3 * (x + y * width)
                    red += basis * linear[index]
                    green += basis * linear[index + 1]
                    blue += basis * linear[index + 2]
            scale = normal / (width * height)
            factors.append((red * scale, green * scale, blue * scale))
    dc = factors[0]
    dc_value = (_linear_to_srgb(dc[0]) << 16) + (_linear_to_srgb(dc[1]) << 8) + _linear_to_srgb(dc[2])
    maximum = max((max(abs(value) for value in factor) for factor in factors[1:]), default=0.0)
    quantized_maximum = int(max(0, min(82, maximum * 166 - 0.5)))
    actual_maximum = (quantized_maximum + 1) / 166
    encoded = _base83((components_x - 1) + (components_y - 1) * 9, 1)
    encoded += _base83(quantized_maximum, 1)
    encoded += _base83(dc_value, 4)
    for factor in factors[1:]:
        quantized = [int(max(0, min(18, _sign_pow(value / actual_maximum, 0.5) * 9 + 9.5))) for value in factor]
        encoded += _base83(quantized[0] * 19 * 19 + quantized[1] * 19 + quantized[2], 2)
    return encoded


def blurhash_for_image(source: Path) -> str:
    from app.playback import ffmpeg_path

    executable = ffmpeg_path()
    if not executable:
        raise RuntimeError("FFmpeg is not available for BlurHash encoding.")
    completed = subprocess.run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=32:32",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout) != 32 * 32 * 3:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail[-1000:] or "FFmpeg did not produce BlurHash pixels.")
    return encode_blurhash(completed.stdout, 32, 32)


def encode_webp(source: Path, target: Path) -> None:
    from app.playback import ffmpeg_path

    executable = ffmpeg_path()
    if not executable:
        raise RuntimeError("FFmpeg is not available for WebP image encoding.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.webp")
    try:
        completed = subprocess.run(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-c:v",
                "libwebp",
                "-quality",
                str(WEBP_QUALITY),
                "-compression_level",
                "6",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if completed.returncode != 0 or not temporary.is_file() or not temporary.stat().st_size:
            detail = (completed.stderr or "FFmpeg did not produce a WebP image.").strip()
            raise RuntimeError(detail[-1000:])
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def encode_webp_bytes(content: bytes, target: Path, suffix: str = ".image") -> None:
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".source-", suffix=normalized_suffix, delete=False
    ) as handle:
        source = Path(handle.name)
        handle.write(content)
    try:
        encode_webp(source, target)
    finally:
        source.unlink(missing_ok=True)


class LocalArtworkCache:
    def __init__(self, db):
        self.db = db
        db_file = getattr(db, "db_file", None)
        self.root = (
            Path(db_file).parent / "image-cache" / "local"
            if db_file and db_file != ":memory:"
            else None
        )

    @staticmethod
    def _valid_hash(value: str | None) -> bool:
        return isinstance(value, str) and len(value) == 64 and all(
            character in "0123456789abcdef" for character in value.lower()
        )

    def path(self, content_hash: str | None) -> Path | None:
        if self.root is None or not self._valid_hash(content_hash):
            return None
        return self.root / f"{content_hash.lower()}.webp"

    def materialize(self, source: Path, content_hash: str | None) -> Path | None:
        target = self.path(content_hash)
        if target is None:
            return None
        if target.is_file() and target.stat().st_size:
            return target
        encode_webp(source, target)
        return target

    def prune(self) -> None:
        if self.root is None or not self.root.is_dir():
            return
        try:
            hashes = {
                row[0].lower()
                for row in self.db.execute(
                    "SELECT file_hash FROM media_files WHERE role='image' AND file_hash IS NOT NULL"
                )
                if self._valid_hash(row[0])
            }
        except Exception:
            return
        for candidate in self.root.glob("*.webp"):
            if candidate.stem.lower() not in hashes:
                candidate.unlink(missing_ok=True)
