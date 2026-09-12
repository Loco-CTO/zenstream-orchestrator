import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.filesystem_watcher import create_library_observer
from watchdog.events import FileSystemEventHandler


@unittest.skipUnless(sys.platform == "win32", "native Windows Watchdog smoke test")
class WindowsWatchdogTest(unittest.TestCase):
    def test_library_observer_does_not_request_last_access_notifications(self):
        from app import filesystem_watcher
        from watchdog.observers import winapi

        self.assertEqual(
            filesystem_watcher._LIBRARY_NOTIFY_FLAGS
            & winapi.FILE_NOTIFY_CHANGE_LAST_ACCESS,
            0,
        )

    def test_native_observer_receives_a_new_media_event(self):
        received = threading.Event()

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if (
                    not event.is_directory
                    and Path(event.src_path).name == "episode.mkv"
                ):
                    received.set()

        with TemporaryDirectory() as directory:
            observer = create_library_observer()
            self.assertIsNotNone(observer)
            observer.schedule(Handler(), directory, recursive=True)
            observer.start()
            try:
                time.sleep(0.25)
                (Path(directory) / "episode.mkv").write_bytes(b"watchdog-smoke")
                self.assertTrue(
                    received.wait(10), "Watchdog did not receive the file event"
                )
            finally:
                observer.stop()
                observer.join(5)

    def test_access_time_only_update_does_not_emit_modified_event(self):
        modified = threading.Event()

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if not event.is_directory:
                    modified.set()

        with TemporaryDirectory() as directory:
            path = Path(directory) / "track.flac"
            path.write_bytes(b"watchdog-access-time-smoke")
            observer = create_library_observer()
            self.assertIsNotNone(observer)
            observer.schedule(Handler(), directory, recursive=True)
            observer.start()
            try:
                time.sleep(0.5)
                path.read_bytes()
                self.assertFalse(
                    modified.wait(2),
                    "read-only file access generated a watcher modification",
                )
            finally:
                observer.stop()
                observer.join(5)


if __name__ == "__main__":
    unittest.main()
