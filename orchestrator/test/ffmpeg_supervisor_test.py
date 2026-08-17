import io
import subprocess
import time
import unittest
from unittest.mock import patch

from app.ffmpeg_supervisor import (
    FFmpegCancelled,
    FFmpegTimedOut,
    run_ffmpeg,
)


class FakeProcess:
    def __init__(self, *, stdout=b"", stderr=b"", running=False, resist=False):
        self.stdout = stdout if hasattr(stdout, "read") else io.BytesIO(stdout)
        self.stderr = stderr if hasattr(stderr, "read") else io.BytesIO(stderr)
        self.returncode = None if running else 0
        self.resist = resist
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if not self.resist:
            self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode


class DelayedTextStream(io.StringIO):
    """Make process exit win the race against stdout reader completion."""

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(0.05)
        line = self.readline()
        if not line:
            raise StopIteration
        return line


class FFmpegSupervisorTest(unittest.TestCase):
    @patch("app.ffmpeg_supervisor.subprocess.Popen")
    def test_binary_stdout_is_preserved_and_stdin_is_detached(self, popen):
        process = FakeProcess(stdout=b"\x00\xff\n\x01", stderr=b"")
        popen.return_value = process

        output = run_ffmpeg(["ffmpeg", "-f", "chromaprint", "-"])

        self.assertEqual(output, b"\x00\xff\n\x01")
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(popen.call_args.kwargs["text"])
        self.assertIn(None, process.wait_calls)

    @patch("app.ffmpeg_supervisor.subprocess.Popen")
    def test_cancellation_terminates_and_reaps_the_child(self, popen):
        process = FakeProcess(running=True)
        popen.return_value = process

        with self.assertRaises(FFmpegCancelled):
            run_ffmpeg(["ffmpeg"], should_terminate=lambda: True)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertTrue(process.wait_calls)
        self.assertIsNotNone(process.returncode)

    @patch("app.ffmpeg_supervisor.subprocess.Popen")
    @patch("app.ffmpeg_supervisor.time.monotonic", side_effect=[0.0, 2.0])
    def test_timeout_kills_a_child_that_resists_termination(self, _clock, popen):
        process = FakeProcess(running=True, resist=True)
        popen.return_value = process

        with self.assertRaises(FFmpegTimedOut):
            run_ffmpeg(["ffmpeg"], timeout_seconds=1)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertIsNotNone(process.returncode)
        self.assertGreaterEqual(len(process.wait_calls), 2)

    @patch("app.ffmpeg_supervisor.subprocess.Popen")
    def test_delayed_final_progress_record_is_delivered(self, popen):
        stdout = DelayedTextStream("out_time_ms=2500000\nprogress=end\n")
        process = FakeProcess(
            stdout=stdout,
            stderr=io.StringIO(""),
        )
        popen.return_value = process
        records = []

        run_ffmpeg(["ffmpeg", "-progress", "pipe:1"], progress=records.append)

        self.assertEqual(
            records,
            [{"out_time_ms": "2500000", "progress": "end"}],
        )

    @patch("app.ffmpeg_supervisor.subprocess.Popen")
    def test_progress_callback_failure_still_stops_and_reaps_child(self, popen):
        process = FakeProcess(
            stdout=io.StringIO("out_time_ms=1000000\nprogress=continue\n"),
            stderr=io.StringIO(""),
            running=True,
        )
        popen.return_value = process

        def fail(_record):
            raise ValueError("progress sink failed")

        with self.assertRaisesRegex(ValueError, "progress sink failed"):
            run_ffmpeg(["ffmpeg", "-progress", "pipe:1"], progress=fail)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.wait_calls)


if __name__ == "__main__":
    unittest.main()
