import hashlib
import sys
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import DatabaseHandler
from app.images import (
    LocalArtworkCache,
    blurhash_for_image,
    encode_blurhash,
    encode_webp,
    encode_webp_bytes,
)


class LocalArtworkCacheTest(unittest.TestCase):
    def test_encodes_a_standard_blurhash_from_rgb_pixels(self):
        value = encode_blurhash(bytes([255, 0, 0]) * 4, 2, 2)

        self.assertEqual(len(value), 28)
        self.assertTrue(value.startswith("L"))

    def test_materializes_by_content_hash_and_prunes_unreferenced_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = DatabaseHandler("sqlite", {}, str(root / "orchestrator.db"))
            try:
                db.execute("CREATE TABLE media_files(quick_fingerprint TEXT,role TEXT)")
                source = root / "poster.jpg"
                source.write_bytes(b"source")
                content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                db.execute(
                    "INSERT INTO media_files VALUES(?, 'image')", (content_hash,)
                )
                cache = LocalArtworkCache(db)

                def encode(_source, target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"webp")

                with patch("app.images.encode_webp", side_effect=encode) as encoder:
                    target = cache.materialize(source, content_hash)
                    self.assertEqual(target.suffix, ".webp")
                    self.assertTrue(target.is_file())
                    cache.materialize(source, content_hash)
                    encoder.assert_called_once()

                db.execute("DELETE FROM media_files")
                cache.prune()
                self.assertFalse(target.exists())
            finally:
                db.close()

    def test_ffmpeg_artwork_commands_are_non_interactive_and_use_level_five(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "poster.jpg"
            target = root / "poster.webp"
            source.write_bytes(b"source")
            calls = []

            def fake_run(args, **kwargs):
                calls.append((args, kwargs))
                Path(args[-1]).write_bytes(b"webp")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("app.playback.ffmpeg_path", return_value="ffmpeg"), patch(
                "app.images.subprocess.run", side_effect=fake_run
            ):
                encode_webp(source, target)

            args, kwargs = calls[0]
            self.assertIn("-nostdin", args)
            self.assertEqual(args[args.index("-compression_level") + 1], "5")
            self.assertIs(kwargs["stdin"], __import__("subprocess").DEVNULL)
            self.assertTrue(target.is_file())

    def test_blurhash_command_is_non_interactive(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "poster.jpg"
            source.write_bytes(b"source")
            calls = []

            def fake_run(args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(
                    returncode=0, stdout=bytes([0, 0, 0]) * 32 * 32, stderr=b""
                )

            with patch("app.playback.ffmpeg_path", return_value="ffmpeg"), patch(
                "app.images.subprocess.run", side_effect=fake_run
            ):
                value = blurhash_for_image(source)

            self.assertEqual(len(value), 28)
            args, kwargs = calls[0]
            self.assertIn("-nostdin", args)
            self.assertIs(kwargs["stdin"], __import__("subprocess").DEVNULL)

    def test_svg_is_rasterized_before_webp_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "logo.webp"
            rendered = []

            def render(**kwargs):
                rendered.append(kwargs["svg_string"])
                return (
                    b"\x89PNG\r\n\x1a\n"
                    + b"\x00" * 8
                    + struct.pack(">II", 100, 40)
                )

            def fake_run(args, **kwargs):
                Path(args[-1]).write_bytes(b"webp")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            fake_resvg = SimpleNamespace(svg_to_bytes=render)
            with patch.dict(sys.modules, {"resvg_py": fake_resvg}), patch(
                "app.playback.ffmpeg_path", return_value="ffmpeg"
            ), patch("app.images.subprocess.run", side_effect=fake_run):
                encode_webp_bytes(
                    b'<svg width="100" height="40" xmlns="http://www.w3.org/2000/svg"/>',
                    target,
                    ".svg",
                )

            self.assertEqual(len(rendered), 1)
            self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
