import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from api.zenstream.version import Version, _main_version


class VersionTests(unittest.TestCase):
    def test_reads_main_counter(self):
        with patch(
            "api.zenstream.version.Path.read_text",
            return_value=json.dumps({"main": 127}),
        ):
            self.assertEqual(_main_version(), 127)

    def test_invalid_counter_defaults_to_zero(self):
        with patch(
            "api.zenstream.version.Path.read_text",
            return_value=json.dumps({"main": -1}),
        ):
            self.assertEqual(_main_version(), 0)

    def test_endpoint_returns_semantic_and_main_versions(self):
        with patch("api.zenstream.version._main_version", return_value=127):
            payload, status = Version().get()
        self.assertEqual(status, 200)
        self.assertEqual(payload["main"], 127)
        self.assertIn("version", payload)


if __name__ == "__main__":
    unittest.main()
