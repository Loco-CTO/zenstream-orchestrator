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
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"METADATA_PATH": directory}),
        ):
            self.assertEqual(metadata_directory(), Path(directory))
            self.assertEqual(log_directory(), metadata_directory() / "logs")

    def test_relative_metadata_path_is_resolved_from_the_project_root(self):
        with patch.dict(os.environ, {"METADATA_PATH": "persistent-data"}):
            self.assertEqual(metadata_directory(), PROJECT_ROOT / "persistent-data")


class LoggingConfigurationTest(unittest.TestCase):
    def test_rotating_log_is_created_under_metadata_path(self):
        root = logging.getLogger("zenstream")
        lumi_logger = logging.getLogger("lumi")
        existing_handlers = list(root.handlers)
        existing_lumi_handlers = list(lumi_logger.handlers)
        existing_lumi_level = lumi_logger.level
        existing_lumi_propagate = lumi_logger.propagate
        root.handlers.clear()
        lumi_logger.handlers.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                try:
                    with (
                        patch.dict(
                            os.environ,
                            {"METADATA_PATH": directory, "ZENSTREAM_LOG_LEVEL": "INFO"},
                        ),
                        patch.object(logging_config, "_configured", False),
                    ):
                        logging_config.configure_logging()

                        log_path = Path(directory) / "logs" / "orchestrator.log"
                        self.assertTrue(log_path.is_file())
                        self.assertEqual(lumi_logger.level, logging.INFO)
                        self.assertFalse(lumi_logger.propagate)
                        self.assertEqual(lumi_logger.handlers, root.handlers)

                        logging.getLogger("lumi.runtime.llama_cpp").info(
                            "Lumi runtime request started turn_id=test"
                        )
                        for handler in lumi_logger.handlers:
                            handler.flush()
                        self.assertIn(
                            "lumi.runtime.llama_cpp Lumi runtime request started turn_id=test",
                            log_path.read_text(encoding="utf-8"),
                        )
                finally:
                    configured_handlers = list(root.handlers)
                    root.handlers.clear()
                    lumi_logger.handlers.clear()
                    for handler in configured_handlers:
                        handler.close()
        finally:
            root.handlers[:] = existing_handlers
            lumi_logger.handlers[:] = existing_lumi_handlers
            lumi_logger.setLevel(existing_lumi_level)
            lumi_logger.propagate = existing_lumi_propagate


if __name__ == "__main__":
    unittest.main()
