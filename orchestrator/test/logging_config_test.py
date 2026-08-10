import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paths import PROJECT_ROOT, metadata_directory


class MetadataPathTest(unittest.TestCase):
    def test_default_metadata_directory_is_the_project_sqlite_directory(self):
        with patch.dict(os.environ, {"METADATA_PATH": ""}):
            self.assertEqual(metadata_directory(), PROJECT_ROOT / "sqlite")

    def test_absolute_metadata_path_is_used_directly(self):
        configured = Path("C:/zenstream-data").resolve()
        with patch.dict(os.environ, {"METADATA_PATH": str(configured)}):
            self.assertEqual(metadata_directory(), configured)

    def test_relative_metadata_path_is_resolved_from_the_project_root(self):
        with patch.dict(os.environ, {"METADATA_PATH": "persistent-data"}):
            self.assertEqual(
                metadata_directory(), PROJECT_ROOT / "persistent-data"
            )


if __name__ == "__main__":
    unittest.main()
