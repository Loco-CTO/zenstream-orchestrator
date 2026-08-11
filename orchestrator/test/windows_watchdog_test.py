import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


@unittest.skipUnless(sys.platform == "win32", "native Windows Watchdog smoke test")
class WindowsWatchdogTest(unittest.TestCase):
    def test_native_observer_receives_a_new_media_event(self):
        received = threading.Event()

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory and Path(event.src_path).name == "episode.mkv":
                    received.set()

        with TemporaryDirectory() as directory:
            observer = Observer()
            observer.schedule(Handler(), directory, recursive=True)
            observer.start()
            try:
                time.sleep(0.25)
                (Path(directory) / "episode.mkv").write_bytes(b"watchdog-smoke")
                self.assertTrue(received.wait(10), "Watchdog did not receive the file event")
            finally:
                observer.stop()
                observer.join(5)


if __name__ == "__main__":
    unittest.main()
