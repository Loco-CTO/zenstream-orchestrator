from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ORCHESTRATOR_ROOT = str(Path(__file__).resolve().parents[1])
if _ORCHESTRATOR_ROOT not in sys.path:
    sys.path.insert(0, _ORCHESTRATOR_ROOT)

from api.zenstream.openapi import _annotate_section_parameter
from app.app import app


class OpenApiSectionContractTest(unittest.TestCase):
    """Regression coverage for curated section query contracts."""

    @classmethod
    def setUpClass(cls):
        """Build one schema for the section contract assertions."""
        app.openapi_schema = None
        cls.schema = app.openapi()

    def test_section_annotation_adds_enum_to_plain_string_schema(self):
        parameter = {"schema": {"type": "string"}}

        _annotate_section_parameter(parameter, "/api/catalog/home")

        self.assertEqual(
            parameter["schema"]["enum"],
            [
                "featured",
                "continueWatching",
                "nextUp",
                "recommendations",
                "derived",
                "library",
            ],
        )

    def test_section_annotation_leaves_unregistered_paths_unchanged(self):
        parameter = {"schema": {"type": "string"}}

        _annotate_section_parameter(parameter, "/api/catalog/unknown")

        self.assertEqual(parameter, {"schema": {"type": "string"}})

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
