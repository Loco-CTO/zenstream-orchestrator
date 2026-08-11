import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import library_watcher
from app.library_watcher import LibraryWatcherManager, WatchStatus, _NativeHandler, choose_backend


class _Event:
    def __init__(self, src_path, dest_path=None, is_directory=False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class BackendSelectionTest(unittest.TestCase):
    def test_explicit_delta_mode_is_preserved(self):
        self.assertEqual(choose_backend("/media", "polling"), ("polling", "explicit"))

    def test_auto_is_native_even_for_shared_mounts(self):
        with patch.object(library_watcher, "_windows_remote_drive", return_value=True):
            self.assertEqual(choose_backend("/media", "auto"), ("native", "native_first_remote"))

    def test_environment_override(self):
        with patch.object(library_watcher, "_configured_backend", return_value="polling"):
            self.assertEqual(choose_backend("/media", "auto"), ("polling", "environment_override"))


class NativeHandlerTest(unittest.TestCase):
    def test_meaningful_events_are_forwarded_and_noise_is_ignored(self):
        manager = Mock()
        handler = _NativeHandler(manager, "library-1", Path("/media"))
        handler.on_created(_Event("/media/Show", is_directory=True))
        handler.on_modified(_Event("/media/Show", is_directory=True))
        handler.on_modified(_Event("/media/Show/Episode.mkv"))
        handler.on_closed_no_write(_Event("/media/Show/Episode.mkv"))
        handler.on_moved(_Event("/media/Old", "/media/New", is_directory=True))
        self.assertEqual(manager.emit.call_count, 3)
        manager.emit.assert_any_call("library-1", Path("/media"), ("/media/Show",), full_scan=False)
        manager.emit.assert_any_call("library-1", Path("/media"), ("/media/Old", "/media/New"), full_scan=False)


class ManagerStatusTest(unittest.TestCase):
    def test_native_event_marks_capability_verified(self):
        manager = LibraryWatcherManager(lambda *_args: None)
        manager._statuses["library-1"] = WatchStatus(backend="native", state="active", capability="listening")
        manager.emit("library-1", Path("/media"), ("/media/Show/Episode.mkv",))
        self.assertEqual(manager.status({"id": "library-1", "type": "tv_series", "directory": "/media"})["capability"], "verified")


if __name__ == "__main__":
    unittest.main()
