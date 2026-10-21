import contextlib
import os
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path


class LauncherBackendTest(unittest.TestCase):
    @contextlib.contextmanager
    def _control_process(self):
        source = Path(__file__).resolve().parents[1]
        script = """
import os
import sys
import threading
import time
from types import SimpleNamespace
from launcher_entry import _watch_launcher

server = SimpleNamespace(should_exit=False)
threading.Thread(target=_watch_launcher, args=(server,), daemon=True).start()
time.sleep(0.2)
if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(0)
    if kernel32.GetFileType(handle) != 3:
        os._exit(3)
print("ready", flush=True)
deadline = time.monotonic() + 20
while not server.should_exit and time.monotonic() < deadline:
    time.sleep(0.01)
print("stopped", flush=True)
os._exit(0 if server.should_exit else 2)
"""
        environment = {**os.environ, "PYTHONPATH": str(source)}
        process = subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        lines = queue.Queue()

        def read_stdout():
            for line in process.stdout:
                lines.put(line.strip())

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            try:
                ready = lines.get(timeout=10)
            except queue.Empty:
                self.fail("idle launcher control pipe blocked native stdin inspection")
            self.assertEqual(ready, "ready")
            yield process, lines
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if not process.stdin.closed:
                process.stdin.close()
            reader.join(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_launcher_source_entry_point_is_present(self):
        root = Path(__file__).resolve().parents[2]
        entry = (root / "orchestrator" / "launcher_entry.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("multiprocessing.freeze_support()", entry)
        self.assertIn('command.strip().lower() == "shutdown"', entry)

    def test_idle_pipe_allows_native_inspection_and_fragmented_shutdown(self):
        with self._control_process() as (process, lines):
            process.stdin.write("ignored\nshut")
            process.stdin.flush()
            with self.assertRaises(queue.Empty):
                lines.get(timeout=0.15)
            self.assertIsNone(process.poll())
            process.stdin.write("down\r\n")
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(lines.get(timeout=5), "stopped")

    def test_control_pipe_eof_stops_backend(self):
        with self._control_process() as (process, lines):
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(lines.get(timeout=5), "stopped")


if __name__ == "__main__":
    unittest.main()
