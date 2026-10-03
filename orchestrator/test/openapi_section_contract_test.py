from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ORCHESTRATOR_ROOT = str(Path(__file__).resolve().parents[1])
if _ORCHESTRATOR_ROOT not in sys.path:
    sys.path.insert(0, _ORCHESTRATOR_ROOT)

from app.app import app


class OpenApiSectionContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.openapi_schema = None
        cls.schema = app.openapi()

    def test_home_and_detail_section_enums_match_their_operations(self):
        """Document the valid section values and nullable shape for both operations."""
        expected = {
            "/api/catalog/home": (
                [
                    "featured",
                    "continueWatching",
                    "nextUp",
                    "recommendations",
                    "derived",
                    "library",
                ],
                "featured",
            ),
            "/api/catalog/items/{entity_id}/detail": (
                ["header", "episodes", "similar", "credits"],
                "header",
            ),
        }
        for path, (expected_values, expected_example) in expected.items():
            operation = self.schema["paths"][path]["get"]
            section_parameters = [
                parameter
                for parameter in operation["parameters"]
                if parameter["name"] == "section"
            ]
            self.assertEqual(len(section_parameters), 1, path)
            section = section_parameters[0]
            self.assertEqual(section["example"], expected_example, path)
            self.assertFalse(section["required"], path)
            string_schemas = [
                variant
                for variant in section["schema"]["anyOf"]
                if variant.get("type") == "string"
            ]
            self.assertEqual(len(string_schemas), 1, path)
            string_schema = string_schemas[0]
            self.assertEqual(string_schema["enum"], expected_values, path)
            self.assertIn({"type": "null"}, section["schema"]["anyOf"], path)
