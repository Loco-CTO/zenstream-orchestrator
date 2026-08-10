import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import logging_config
from app.logging_config import log_directory
from app.paths import PROJECT_ROOT, metadata_directory


class MetadataPathTest(unittest.TestCase):
    def test_default_metadata_directory_is_the_project_sqlite_directory(self):
        with patch.dict(os.environ, {"METADATA_PATH": ""}):
            self.assertEqual(metadata_directory(), PROJECT_ROOT / "sqlite")

    def test_absolute_metadata_path_is_used_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"METADATA_PATH": directory}):
                self.assertEqual(metadata_directory(), Path(directory))
                self.assertEqual(log_directory(), metadata_directory() / "logs")

    def test_relative_metadata_path_is_resolved_from_the_project_root(self):
        with patch.dict(os.environ, {"METADATA_PATH": "persistent-data"}):
            self.assertEqual(
                metadata_directory(), PROJECT_ROOT / "persistent-data"
            )


class LoggingConfigurationTest(unittest.TestCase):
    def test_rotating_log_is_created_under_metadata_path(self):
        root = logging.getLogger("zenstream")
        existing_handlers = list(root.handlers)
        root.handlers.clear()
        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {"METADATA_PATH": directory}),
                patch.object(logging_config, "_configured", False),
            ):
                logging_config.configure_logging()

                self.assertTrue(
                    (Path(directory) / "logs" / "orchestrator.log").is_file()
                )
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers[:] = existing_handlers


if __name__ == "__main__":
    unittest.main()
