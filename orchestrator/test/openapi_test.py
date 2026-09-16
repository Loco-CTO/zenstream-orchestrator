from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ORCHESTRATOR_ROOT = str(Path(__file__).resolve().parents[1])
if _ORCHESTRATOR_ROOT not in sys.path:
    sys.path.insert(0, _ORCHESTRATOR_ROOT)

from api.zenstream.openapi import (
    DOCUMENTATION_EXCLUDED_PATHS,
    NO_REQUEST_BODY_ROUTES,
    OPENAPI_TAGS,
    REALTIME_CHANNELS,
    _iter_api_routes,
)
from app.app import app

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _operations(schema: dict):
    return [
        (path, method.upper(), operation)
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method.upper() in HTTP_METHODS
    ]


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _examples(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"example", "examples"}:
                yield from _walk(child)
            else:
                yield from _examples(child)
    elif isinstance(value, list):
        for child in value:
            yield from _examples(child)


class OpenApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.openapi_schema = None
        cls.schema = app.openapi()
        cls.operations = _operations(cls.schema)

    def test_curated_tags_are_ordered_and_every_operation_is_tagged(self):
        self.assertEqual(
            [tag["name"] for tag in self.schema["tags"]],
            [tag["name"] for tag in OPENAPI_TAGS],
        )
        self.assertNotIn("default", {tag["name"] for tag in self.schema["tags"]})
        for path, method, operation in self.operations:
            self.assertEqual(len(operation.get("tags", [])), 1, (method, path))
            self.assertIn(operation["tags"][0], {tag["name"] for tag in OPENAPI_TAGS})
            self.assertNotIn("default", operation["tags"])

    def test_documented_routes_match_api_routes_and_static_allowlist(self):
        expected = set()
        for route in _iter_api_routes(app):
            if not route.include_in_schema:
                continue
            expected.update(
                (method, route.path)
                for method in (route.methods or set())
                if method in HTTP_METHODS
            )
        actual = {(method, path) for path, method, _ in self.operations}
        self.assertEqual(actual, expected)
        for path in DOCUMENTATION_EXCLUDED_PATHS:
            self.assertNotIn(path, self.schema["paths"])

    def test_operation_ids_are_explicit_stable_and_unique(self):
        operation_ids = [
            operation["operationId"] for _, _, operation in self.operations
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        for path, method, operation in self.operations:
            self.assertRegex(
                operation["operationId"], rf"^{method.lower()}_[a-z0-9_]+$"
            )
            self.assertTrue(operation.get("summary"), (method, path))
            self.assertTrue(operation.get("description"), (method, path))

    def test_write_operations_have_documented_request_bodies(self):
        for path, method, operation in self.operations:
            if method not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            if (method, path) in NO_REQUEST_BODY_ROUTES:
                continue
            self.assertIn("requestBody", operation, (method, path))
            content = operation["requestBody"].get("content", {})
            self.assertTrue(content, (method, path))
            for media_type, value in content.items():
                self.assertIn("schema", value, (method, path, media_type))

    def test_schema_references_resolve(self):
        schemas = self.schema["components"]["schemas"]
        for value in _examples(self.schema):
            if not isinstance(value, dict) or "$ref" not in value:
                continue
            reference = value["$ref"]
            self.assertTrue(reference.startswith("#/components/schemas/"), reference)
            self.assertIn(reference.rsplit("/", 1)[-1], schemas, reference)

    def test_json_responses_have_reusable_schemas_or_explicit_empty_status(self):
        for path, method, operation in self.operations:
            for status, response in operation["responses"].items():
                if status == "204":
                    self.assertNotIn("content", response, (method, path, status))
                    continue
                for media_type, content in response.get("content", {}).items():
                    if media_type != "application/json":
                        continue
                    self.assertIn("schema", content, (method, path, status))
                    schema = content["schema"]
                    if "$ref" in schema:
                        self.assertIn(
                            schema["$ref"].rsplit("/", 1)[-1],
                            self.schema["components"]["schemas"],
                        )
                    else:
                        self.assertTrue(
                            schema.get("type") or schema.get("oneOf"),
                            (method, path, status),
                        )

    def test_auth_schemes_and_realtime_guidance_are_present(self):
        schemes = self.schema["components"]["securitySchemes"]
        for name in (
            "UserBearerAuth",
            "UserSessionCookie",
            "ResourceTicket",
            "ArtworkTicket",
            "SocketTicket",
            "AdminSessionCookie",
            "AdminTokenHeader",
            "AdminUsernameHeader",
        ):
            self.assertIn(name, schemes)
        self.assertEqual(
            self.schema["paths"]["/api/auth/login"]["post"]["security"], []
        )
        self.assertTrue(
            any(
                "UserBearerAuth" in requirement
                for requirement in self.schema["paths"]["/api/catalog/items"]["get"][
                    "security"
                ]
            )
        )
        self.assertIn(
            "ResourceTicket",
            self.schema["paths"]["/api/playback/items/{entity_id}/stream"]["get"][
                "security"
            ][-1],
        )
        self.assertIn(
            "AdminSessionCookie",
            self.schema["paths"]["/api/admin/libraries"]["get"]["security"][0],
        )
        self.assertEqual(
            set(self.schema["x-zenstream-websockets"]), set(REALTIME_CHANNELS)
        )
        self.assertIn("/api/ws/catalog", self.schema["info"]["description"])
        self.assertIn("/api/ws/syncplay", self.schema["info"]["description"])

    def test_media_contracts_and_pending_headers_are_documented(self):
        stream = self.schema["paths"]["/api/playback/items/{entity_id}/stream"]
        self.assertIn("206", stream["get"]["responses"])
        self.assertIn("416", stream["get"]["responses"])
        self.assertIn("Accept-Ranges", stream["get"]["responses"]["206"]["headers"])
        self.assertIn("Content-Range", stream["get"]["responses"]["416"]["headers"])
        self.assertIn(
            "application/vnd.apple.mpegurl",
            self.schema["paths"]["/api/playback/sessions/{session_id}/{filename}"][
                "get"
            ]["responses"]["200"]["content"],
        )
        self.assertIn(
            "video/mp2t",
            self.schema["paths"]["/api/playback/sessions/{session_id}/{filename}"][
                "get"
            ]["responses"]["200"]["content"],
        )
        self.assertIn(
            "text/vtt",
            self.schema["paths"][
                "/api/playback/items/{entity_id}/subtitles/{media_file_id}.vtt"
            ]["get"]["responses"]["200"]["content"],
        )
        image = self.schema["paths"][
            "/api/catalog/items/{entity_id}/images/{image_type}"
        ]["get"]["responses"]
        self.assertIn("202", image)
        self.assertIn("Retry-After", image["202"]["headers"])
        self.assertIn("X-ZenStream-Image-State", image["202"]["headers"])
        image_parameters = {
            parameter["name"]: parameter
            for parameter in self.schema["paths"][
                "/api/catalog/items/{entity_id}/images/{image_type}"
            ]["get"]["parameters"]
        }
        self.assertEqual(image_parameters["w"]["schema"]["enum"], [160, 320])
        self.assertNotIn("av", image_parameters)
        trickplay = self.schema["paths"]["/api/playback/items/{entity_id}/trickplay"][
            "get"
        ]["responses"]
        self.assertIn("Retry-After", trickplay["202"]["headers"])
        artwork_status = self.schema["paths"][
            "/api/admin/artwork-variants/status"
        ]["get"]
        self.assertIn(
            "AdminSessionCookie",
            artwork_status["security"][0],
        )
        status_schema = artwork_status["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(
            status_schema["$ref"],
            "#/components/schemas/ArtworkVariantStatus",
        )
        self.assertEqual(
            self.schema["components"]["schemas"]["ArtworkVariantStatus"]["properties"][
                "state"
            ]["enum"],
            ["starting", "warming", "ready", "degraded", "unavailable"],
        )

    def test_documented_success_statuses_match_known_mutations(self):
        no_content = (
            ("POST", "/api/account/password"),
            ("POST", "/api/auth/logout"),
            ("DELETE", "/api/account/watch-history"),
            ("POST", "/api/admin/logout"),
            ("DELETE", "/api/admin/users/{user_id}"),
            ("DELETE", "/api/admin/libraries/{library_id}"),
            ("DELETE", "/api/admin/invites/{invite_id}"),
            ("DELETE", "/api/syncplay/groups/{group_id}"),
        )
        for method, path in no_content:
            responses = self.schema["paths"][path][method.lower()]["responses"]
            self.assertIn("204", responses, (method, path))
            self.assertNotIn("200", responses, (method, path))
            self.assertNotIn("content", responses["204"], (method, path))

        created = (
            ("POST", "/api/user/register"),
            ("POST", "/api/admin/accounts"),
            ("POST", "/api/admin/invites"),
            ("POST", "/api/admin/users"),
            ("POST", "/api/admin/libraries"),
            ("POST", "/api/syncplay/groups"),
        )
        for method, path in created:
            self.assertIn(
                "201", self.schema["paths"][path][method.lower()]["responses"]
            )

        accepted = (
            ("POST", "/api/admin/libraries/{library_id}/scan"),
            ("POST", "/api/admin/jobs/{job_id}/run"),
        )
        for method, path in accepted:
            self.assertIn(
                "202", self.schema["paths"][path][method.lower()]["responses"]
            )

        self.assertIn(
            "audioLanguages",
            self.schema["components"]["schemas"]["PlaybackPreferences"]["properties"],
        )
        self.assertIn(
            "initialPage",
            self.schema["components"]["schemas"]["CatalogLibrariesResponse"][
                "properties"
            ],
        )

    def test_examples_are_synthetic_and_have_no_absolute_paths_or_real_secret_shapes(
        self,
    ):
        forbidden = (
            re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]"),
            re.compile(r"(?:^|/)(?:Users|home|var|tmp)(?:/|$)", re.IGNORECASE),
            re.compile(r"(?:sk|ghp|AIza)[-_][A-Za-z0-9_-]{8,}"),
            re.compile(r"^eyJ[A-Za-z0-9_-]{20,}$"),
        )
        for value in _examples(self.schema):
            if not isinstance(value, str):
                continue
            for pattern in forbidden:
                self.assertIsNone(pattern.search(value), value)


if __name__ == "__main__":
    unittest.main()
