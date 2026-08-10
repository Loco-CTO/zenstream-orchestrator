import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from app import library_watcher
from app.library_watcher import (
    DirectorySnapshot,
    LibraryWatcherManager,
    WatchStatus,
    _NativeHandler,
    _PollRegistration,
    choose_backend,
)


class _Event:
    def __init__(self, src_path, dest_path=None, is_directory=False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class BackendSelectionTest(unittest.TestCase):
    def test_explicit_mode_wins(self):
        with patch.object(
            library_watcher, "_configured_backend", return_value="native"
        ):
            self.assertEqual(
                choose_backend("/media", "polling"), ("polling", "explicit")
            )

    def test_remote_mounts_use_polling(self):
        with (
            patch.object(library_watcher, "_linux_mount_type", return_value="cifs"),
            patch.object(library_watcher, "_in_container", return_value=False),
        ):
            self.assertEqual(
                choose_backend("/media", "auto"), ("polling", "filesystem_cifs")
            )

    def test_unknown_container_mount_uses_polling(self):
        with (
            patch.object(
                library_watcher, "_linux_mount_type", return_value="fuse.grpcfuse"
            ),
            patch.object(library_watcher, "_in_container", return_value=True),
        ):
            self.assertEqual(
                choose_backend("/media", "auto"), ("polling", "unknown_container_mount")
            )

    def test_local_mount_uses_native(self):
        with (
            patch.object(library_watcher, "_linux_mount_type", return_value="ext4"),
            patch.object(library_watcher, "_in_container", return_value=False),
        ):
            self.assertEqual(
                choose_backend("/media", "auto"), ("native", "local_filesystem")
            )


class NativeHandlerTest(unittest.TestCase):
    def test_directory_and_file_events_are_forwarded_but_directory_modify_is_ignored(
        self,
    ):
        manager = Mock()
        handler = _NativeHandler(manager, "library-1", Path("/media"))

        handler.on_created(_Event("/media/Show", is_directory=True))
        handler.on_modified(_Event("/media/Show", is_directory=True))
        handler.on_modified(_Event("/media/Show/Episode.mkv"))
        handler.on_moved(_Event("/media/Old", "/media/New", is_directory=True))

        self.assertEqual(manager.emit.call_count, 3)
        manager.emit.assert_any_call(
            "library-1", Path("/media"), ("/media/Show",), full_scan=False
        )
        manager.emit.assert_any_call(
            "library-1", Path("/media"), ("/media/Old", "/media/New"), full_scan=False
        )


class PollingSnapshotTest(unittest.TestCase):
    def test_baseline_and_diff_emit_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "Episode.mkv"
            media.write_bytes(b"one")
            events = []
            manager = LibraryWatcherManager(
                lambda library_id, watched_root, paths, full_scan: events.append(
                    (library_id, watched_root, paths, full_scan)
                )
            )
            status = WatchStatus(backend="polling", state="active")
            registration = _PollRegistration("library-1", root, status)
            first = Future()
            first.set_result(DirectorySnapshot(str(root), recursive=True))
            manager._finish_poll(registration, first)
            self.assertEqual(events[-1][3], True)

            media.write_bytes(b"two")
            second = Future()
            second.set_result(DirectorySnapshot(str(root), recursive=True))
            manager._finish_poll(registration, second)
            self.assertFalse(events[-1][3])
            self.assertIn(str(media), events[-1][2])

    def test_failed_snapshot_keeps_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Episode.mkv").write_bytes(b"one")
            manager = LibraryWatcherManager(lambda *_args: None)
            status = WatchStatus(backend="polling", state="active")
            registration = _PollRegistration("library-1", root, status)
            first = Future()
            original = DirectorySnapshot(str(root), recursive=True)
            first.set_result(original)
            manager._finish_poll(registration, first)
            failed = Future()
            failed.set_exception(OSError("temporary mount failure"))
            manager._finish_poll(registration, failed)
            self.assertIs(registration.snapshot, original)
            self.assertEqual(status.state, "degraded")


if __name__ == "__main__":
    unittest.main()
