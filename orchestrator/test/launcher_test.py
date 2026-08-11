import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import paths


class LauncherBackendTest(unittest.TestCase):
    def test_frozen_metadata_default_uses_local_app_data(self):
        with TemporaryDirectory() as directory:
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.dict(os.environ, {"LOCALAPPDATA": directory}, clear=False),
                patch.dict(os.environ, {"METADATA_PATH": ""}, clear=False),
            ):
                self.assertEqual(
                    paths.metadata_directory(),
                    Path(directory) / "ZenStream Orchestrator" / "metadata",
                )

    def test_packaging_spec_contains_launcher_resources(self):
        root = Path(__file__).resolve().parents[2]
        spec = (root / "orchestrator.spec").read_text(encoding="utf-8")
        self.assertIn('"orchestrator/launcher_entry.py"', spec)
        self.assertIn('(str(dashboard), "web")', spec)
        self.assertIn('(str(project_root / "migrations"), "migrations")', spec)
        self.assertIn('name="zenstream-orchestrator-backend"', spec)


if __name__ == "__main__":
    unittest.main()
