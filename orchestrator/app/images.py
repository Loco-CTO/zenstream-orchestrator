from __future__ import annotations

import math
import re
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path

WEBP_QUALITY = 85
WEBP_COMPRESSION_LEVEL = 5
_SVG_MAX_DIMENSION = 4096
_SVG_MAX_PIXELS = 16 * 1024 * 1024
_BASE83 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"


def _base83(value: int, length: int) -> str:
    encoded = ""
    for exponent in range(length - 1, -1, -1):
        digit = (value // (83**exponent)) % 83
        encoded += _BASE83[digit]
    return encoded


def _srgb_to_linear(value: int) -> float:
    normalized = value / 255
    return (
        normalized / 12.92
        if normalized <= 0.04045
        else ((normalized + 0.055) / 1.055) ** 2.4
    )


def _linear_to_srgb(value: float) -> int:
    value = max(0.0, min(1.0, value))
    normalized = (
        value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    )
    return int(max(0, min(255, normalized * 255 + 0.5)))


def _sign_pow(value: float, exponent: float) -> float:
    return math.copysign(abs(value) ** exponent, value)


def encode_blurhash(
    pixels: bytes, width: int, height: int, components_x: int = 4, components_y: int = 3
) -> str:
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
                    basis = math.cos(math.pi * x_component * x / width) * math.cos(
                        math.pi * y_component * y / height
                    )
                    index = 3 * (x + y * width)
                    red += basis * linear[index]
                    green += basis * linear[index + 1]
                    blue += basis * linear[index + 2]
            scale = normal / (width * height)
            factors.append((red * scale, green * scale, blue * scale))
    dc = factors[0]
    dc_value = (
        (_linear_to_srgb(dc[0]) << 16)
        + (_linear_to_srgb(dc[1]) << 8)
        + _linear_to_srgb(dc[2])
    )
    maximum = max(
        (max(abs(value) for value in factor) for factor in factors[1:]), default=0.0
    )
    quantized_maximum = int(max(0, min(82, maximum * 166 - 0.5)))
    actual_maximum = (quantized_maximum + 1) / 166
    encoded = _base83((components_x - 1) + (components_y - 1) * 9, 1)
    encoded += _base83(quantized_maximum, 1)
    encoded += _base83(dc_value, 4)
    for factor in factors[1:]:
        quantized = [
            int(max(0, min(18, _sign_pow(value / actual_maximum, 0.5) * 9 + 9.5)))
            for value in factor
        ]
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
            "-nostdin",
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
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
                "-nostdin",
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
                str(WEBP_COMPRESSION_LEVEL),
                str(temporary),
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if (
            completed.returncode != 0
            or not temporary.is_file()
            or not temporary.stat().st_size
        ):
            detail = (
                completed.stderr or "FFmpeg did not produce a WebP image."
            ).strip()
            raise RuntimeError(detail[-1000:])
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _svg_dimension(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:px)?\s*", value, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _rasterize_svg(content: bytes) -> bytes:
    """Render provider SVG input to a bounded transparent PNG."""
    try:
        markup = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("SVG artwork is not valid UTF-8") from error
    lowered = markup.lower()
    if any(
        token in lowered
        for token in (
            "<!doctype",
            "<script",
            " onload=",
            "javascript:",
            'href="http:',
            "href='http:",
            'href="https:',
            "href='https:",
            'href="file:',
            "href='file:",
        )
    ):
        raise ValueError("SVG artwork contains unsupported or external content")
    root = re.search(r"<svg\b([^>]*)>", markup, re.IGNORECASE | re.DOTALL)
    if not root:
        raise ValueError("SVG artwork has no root element")
    attributes = root.group(1)
    width = _svg_dimension(
        (
            re.search(r"\bwidth\s*=\s*['\"]([^'\"]+)['\"]", attributes, re.IGNORECASE)
            or [None, None]
        )[1]
    )
    height = _svg_dimension(
        (
            re.search(r"\bheight\s*=\s*['\"]([^'\"]+)['\"]", attributes, re.IGNORECASE)
            or [None, None]
        )[1]
    )
    view_box = re.search(
        r"\bviewBox\s*=\s*['\"]\s*([0-9.+-]+)\s+([0-9.+-]+)\s+([0-9.+-]+)\s+([0-9.+-]+)\s*['\"]",
        attributes,
        re.IGNORECASE,
    )
    if view_box:
        width = width or float(view_box.group(3))
        height = height or float(view_box.group(4))
    if (
        width
        and height
        and (
            width > _SVG_MAX_DIMENSION
            or height > _SVG_MAX_DIMENSION
            or width * height > _SVG_MAX_PIXELS
        )
    ):
        raise ValueError("SVG artwork dimensions exceed the supported limit")
    try:
        import resvg_py

        rendered = bytes(
            resvg_py.svg_to_bytes(
                svg_string=markup,
                resources_dir=None,
                skip_system_fonts=False,
            )
        )
        if len(rendered) < 24 or rendered[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("SVG rasterizer did not produce a PNG")
        rendered_width, rendered_height = struct.unpack(">II", rendered[16:24])
        if (
            not rendered_width
            or not rendered_height
            or rendered_width > _SVG_MAX_DIMENSION
            or rendered_height > _SVG_MAX_DIMENSION
            or rendered_width * rendered_height > _SVG_MAX_PIXELS
        ):
            raise ValueError("SVG rasterized dimensions exceed the supported limit")
        return rendered
    except ImportError as error:
        raise RuntimeError("SVG artwork support is unavailable") from error
    except Exception as error:
        raise ValueError(f"SVG artwork could not be rasterized: {error}") from error


def encode_webp_bytes(content: bytes, target: Path, suffix: str = ".image") -> None:
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if (
        normalized_suffix.lower() in {".svg", ".svgz"}
        or content.lstrip().startswith(b"<svg")
        or content.lstrip().startswith(b"<?xml")
        and b"<svg" in content[:4096].lower()
    ):
        raster = tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".raster-", suffix=".png", delete=False
        )
        raster_path = Path(raster.name)
        try:
            raster.write(_rasterize_svg(content))
            raster.close()
            encode_webp(raster_path, target)
        finally:
            raster.close()
            raster_path.unlink(missing_ok=True)
        return
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
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value.lower())
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
                    "SELECT quick_fingerprint FROM media_files WHERE role='image' AND quick_fingerprint IS NOT NULL"
                )
                if self._valid_hash(row[0])
            }
        except Exception:
            return
        for candidate in self.root.glob("*.webp"):
            if candidate.stem.lower() not in hashes:
                candidate.unlink(missing_ok=True)
