import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.avatar import (
    AvatarCrop,
    AvatarError,
    UserAvatarStore,
    _encode_avatar,
    _Probe,
    _probe_image,
    _validated_crop,
    avatar_filter,
)
from app.models.account import Account
from app.playback import ffmpeg_path, ffprobe_path


class _Database:
    def __init__(self, path: Path):
        self.db_file = str(path)
        self.connection = sqlite3.connect(path)

    def read_execute(self, query, params=()):
        cursor = self.connection.execute(query, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def execute(self, query, params=()):
        cursor = self.connection.execute(query, params)
        try:
            self.connection.commit()
            return cursor.fetchall()
        finally:
            cursor.close()

    @contextmanager
    def transaction(self):
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def close(self):
        self.connection.close()


class AvatarProcessingTests(unittest.TestCase):
    def test_filter_uses_clockwise_rotations_before_crop_and_scale(self):
        probe = _Probe(1200, 800)
        self.assertEqual(
            avatar_filter(_validated_crop(AvatarCrop(10, 20, 700, 90), probe)),
            "transpose=1,crop=700:700:10:20,scale=500:500:flags=lanczos",
        )
        self.assertEqual(
            avatar_filter(_validated_crop(AvatarCrop(0, 0, 700, 180), probe)),
            "hflip,vflip,crop=700:700:0:0,scale=500:500:flags=lanczos",
        )

    def test_rejects_invalid_rotation_and_out_of_bounds_crop(self):
        probe = _Probe(800, 600)
        with self.assertRaises(AvatarError):
            _validated_crop(AvatarCrop(0, 0, 100, 45), probe)
        with self.assertRaises(AvatarError):
            _validated_crop(AvatarCrop(700, 0, 200, 0), probe)
        with self.assertRaises(AvatarError):
            _validated_crop(AvatarCrop(0, 0, 601, 90), probe)

    def test_non_gif_processing_is_exactly_500_square_webp(self):
        source = Path(__file__).resolve().parents[2] / "assets" / "icons" / "icon.png"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "avatar.webp"
            probe = _probe_image(source)
            crop = _validated_crop(
                AvatarCrop(0, 0, min(probe.width, probe.height), 0), probe
            )
            _encode_avatar(source, target, "png", crop)
            self.assertEqual(_probe_image(target), _Probe(500, 500))
            self.assertGreater(target.stat().st_size, 0)

    def test_gif_processing_keeps_multiple_frames_and_square_dimensions(self):
        executable = ffmpeg_path()
        probe_executable = ffprobe_path()
        if not executable or not probe_executable:
            self.skipTest("FFmpeg is not available")
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            source = directory_path / "source.gif"
            created = subprocess.run(
                [
                    executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=80x60:r=2",
                    "-t",
                    "1",
                    "-f",
                    "gif",
                    str(source),
                ],
                capture_output=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest("Bundled FFmpeg cannot create the GIF fixture")
            target = directory_path / "avatar.gif"
            probe = _probe_image(source)
            crop = _validated_crop(AvatarCrop(0, 0, 60, 90), probe)
            _encode_avatar(source, target, "gif", crop)
            self.assertEqual(_probe_image(target), _Probe(500, 500))
            inspected = subprocess.run(
                [
                    probe_executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-count_frames",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=nb_read_frames",
                    "-of",
                    "json",
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(inspected.stdout)
            self.assertGreaterEqual(int(payload["streams"][0]["nb_read_frames"]), 2)


class AvatarStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = _Database(Path(self.directory.name) / "orchestrator.db")
        self.db.connection.execute("PRAGMA foreign_keys=ON")
        self.db.execute(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT NOT NULL,
                password TEXT NOT NULL, password_scheme TEXT NOT NULL DEFAULT 'sha256',
                disabled INTEGER NOT NULL DEFAULT 0)
            """
        )
        self.db.execute(
            """
            CREATE TABLE user_avatars(
                user_id TEXT PRIMARY KEY NOT NULL,
                version TEXT NOT NULL,
                file_format TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        self.db.execute(
            "INSERT INTO users(id,username,password) VALUES(?,?,?)",
            ("user-1", "Alex", "password"),
        )
        self.store = UserAvatarStore(self.db)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def _insert_avatar(self, version="old-version", file_format="webp"):
        path = self.store._path("user-1", version, file_format)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old-avatar")
        self.db.execute(
            "INSERT INTO user_avatars(user_id,version,file_format,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("user-1", version, file_format, "now", "now"),
        )
        return path

    def test_failed_replacement_keeps_previous_avatar(self):
        previous = self._insert_avatar()
        with (
            patch("app.avatar._probe_image", return_value=_Probe(100, 100)),
            patch(
                "app.avatar._encode_avatar",
                side_effect=AvatarError("processing failed"),
            ),
            self.assertRaises(AvatarError),
        ):
            self.store.save(
                "user-1", b"\x89PNG\r\n\x1a\n", None, AvatarCrop(0, 0, 100, 0)
            )
        self.assertEqual(self.store.version("user-1"), "old-version")
        self.assertEqual(previous.read_bytes(), b"old-avatar")

    def test_successful_replacement_removes_previous_file_atomically(self):
        previous = self._insert_avatar()

        def write_output(_source, target, _format, _crop):
            target.write_bytes(b"new-avatar")

        with (
            patch(
                "app.avatar._probe_image",
                side_effect=[_Probe(100, 100), _Probe(500, 500)],
            ),
            patch("app.avatar._encode_avatar", side_effect=write_output),
        ):
            version = self.store.save(
                "user-1", b"\x89PNG\r\n\x1a\n", None, AvatarCrop(0, 0, 100, 0)
            )
        self.assertNotEqual(version, "old-version")
        self.assertFalse(previous.exists())
        resolved = self.store.resolve("user-1", version)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[0].read_bytes(), b"new-avatar")
        self.assertIsNone(self.store.resolve("user-1", "old-version"))

    def test_remove_deletes_record_and_file(self):
        path = self._insert_avatar()
        self.store.remove("user-1")
        self.assertIsNone(self.store.version("user-1"))
        self.assertFalse(path.exists())

    def test_account_deletion_removes_avatar_file(self):
        path = self._insert_avatar()
        account = Account.__new__(Account)
        account.db = self.db
        self.assertTrue(account.delete("user-1"))
        self.assertFalse(path.exists())
        self.assertEqual(self.db.read_execute("SELECT * FROM user_avatars"), [])


if __name__ == "__main__":
    unittest.main()
