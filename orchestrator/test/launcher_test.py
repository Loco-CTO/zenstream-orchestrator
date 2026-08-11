import unittest
from pathlib import Path


class LauncherBackendTest(unittest.TestCase):
    def test_launcher_source_entry_point_is_present(self):
        root = Path(__file__).resolve().parents[2]
        entry = (root / "orchestrator" / "launcher_entry.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("multiprocessing.freeze_support()", entry)
        self.assertIn('command.strip().lower() == "shutdown"', entry)


if __name__ == "__main__":
    unittest.main()
