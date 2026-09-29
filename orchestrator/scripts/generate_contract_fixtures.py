from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPOSITORY_ROOT / "contracts" / "openapi.json"
FIXTURES_PATH = REPOSITORY_ROOT / "contracts" / "fixtures" / "http.json"
SYNCPLAY_FIXTURES_PATH = REPOSITORY_ROOT / "contracts" / "fixtures" / "syncplay.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head"}
REDACTED = "<redacted>"
NULLABLE_SAMPLE_FIELDS = {
    "artistid",
    "avatarversion",
    "catalogitemid",
    "catalogseriesid",
    "navigationtarget",
    "nextcursor",
    "readat",
    "refreshattemptid",
    "seriesid",
    "seriestitle",
    "subtitle",
}


def resolve_pointer(document: dict, reference: str) -> dict:
    if not reference.startswith("#/"):
        raise ValueError(f"unsupported external OpenAPI reference: {reference}")
    value = document
    for token in reference[2:].split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]
    return value


def _sample(
    document: dict,
    schema: dict,
    *,
    field: str = "",
    required: bool = True,
    request: bool = False,
    owner: str = "",
    active_refs: frozenset[str] = frozenset(),
    depth: int = 0,
):
    if depth > 24:
        return None

    if "$ref" in schema:
        reference = schema["$ref"]
        if reference in active_refs:
            return {}
        return _sample(
            document,
            resolve_pointer(document, reference),
            field=field,
            required=required,
            request=request,
            owner=reference.rsplit("/", 1)[-1],
            active_refs=active_refs | {reference},
            depth=depth + 1,
        )

    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if "enum" in schema:
        return copy.deepcopy(schema["enum"][0])

    for composition in ("oneOf", "anyOf"):
        if composition in schema:
            variants = schema[composition]
            nullable = any(
                variant.get("type") == "null" or variant.get("const", object()) is None
                for variant in variants
            )
            if nullable and not required and field.lower() in NULLABLE_SAMPLE_FIELDS:
                return None
            candidates = [
                variant
                for variant in variants
                if variant.get("type") != "null"
                and variant.get("const", object()) is not None
            ]
            if candidates:
                return _sample(
                    document,
                    candidates[0],
                    field=field,
                    required=required,
                    request=request,
                    owner=owner,
                    active_refs=active_refs,
                    depth=depth + 1,
                )

    if "allOf" in schema:
        combined = {}
        for part in schema["allOf"]:
            value = _sample(
                document,
                part,
                field=field,
                required=required,
                request=request,
                owner=owner,
                active_refs=active_refs,
                depth=depth + 1,
            )
            if isinstance(value, dict):
                combined.update(value)
            elif not combined:
                return value
        return combined

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if (
            "null" in schema_type
            and not required
            and field.lower() in NULLABLE_SAMPLE_FIELDS
        ):
            return None
        schema_type = next((value for value in schema_type if value != "null"), None)

    if (
        schema.get("nullable")
        and not required
        and field.lower() in NULLABLE_SAMPLE_FIELDS
    ):
        return None

    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required_fields = set(schema.get("required", []))
        result = {}
        for name, property_schema in properties.items():
            if request and property_schema.get("readOnly"):
                continue
            if not request and property_schema.get("writeOnly"):
                continue
            result[name] = _sample(
                document,
                property_schema,
                field=name,
                required=name in required_fields,
                request=request,
                owner=owner,
                active_refs=active_refs,
                depth=depth + 1,
            )
        additional = schema.get("additionalProperties")
        if not properties and isinstance(additional, dict):
            result["fixtureKey"] = _sample(
                document,
                additional,
                field="fixtureValue",
                request=request,
                owner=owner,
                active_refs=active_refs,
                depth=depth + 1,
            )
        return result

    if schema_type == "array" or "items" in schema:
        minimum = max(1, int(schema.get("minItems", 0)))
        count = min(minimum, 2)
        return [
            _sample(
                document,
                schema.get("items", {}),
                field=field,
                request=request,
                owner=owner,
                active_refs=active_refs,
                depth=depth + 1,
            )
            for _ in range(count)
        ]

    if schema_type == "boolean":
        return True
    if schema_type == "integer":
        value = int(schema.get("default", schema.get("minimum", 1)))
        if "exclusiveMinimum" in schema:
            value = max(value, int(schema["exclusiveMinimum"]) + 1)
        if "maximum" in schema:
            value = min(value, int(schema["maximum"]))
        if "multipleOf" in schema:
            divisor = int(schema["multipleOf"])
            value = ((value + divisor - 1) // divisor) * divisor
        return value
    if schema_type == "number":
        value = float(schema.get("default", schema.get("minimum", 1.25)))
        if "exclusiveMinimum" in schema:
            value = max(value, float(schema["exclusiveMinimum"]) + 0.25)
        if "maximum" in schema:
            value = min(value, float(schema["maximum"]))
        return value
    if schema_type == "string" or "format" in schema:
        normalized = re.sub(r"[^a-z0-9]", "", field.lower())
        if owner == "CatalogItem" and normalized == "type":
            return "movie"
        if normalized in {
            "password",
            "currentpassword",
            "newpassword",
            "confirmnewpassword",
            "token",
            "refreshtoken",
            "accesstoken",
            "resourceticket",
            "artworkticket",
            "socketticket",
            "ticket",
            "secret",
            "apikey",
            "authorization",
            "access",
        }:
            return REDACTED
        string_format = schema.get("format")
        if string_format == "uuid":
            return "00000000-0000-4000-8000-000000000001"
        if string_format == "date-time":
            return "2026-09-29T12:00:00Z"
        if string_format == "date":
            return "2026-09-29"
        if string_format == "time":
            return "12:00:00Z"
        if string_format in {"uri", "uri-reference", "url"}:
            return "https://example.invalid/fixture"
        if string_format == "email" or normalized in {"email", "emailaddress"}:
            return "fixture@example.invalid"
        if string_format == "hostname":
            return "example.invalid"
        if string_format == "ipv4":
            return "192.0.2.10"
        if string_format == "duration":
            return "PT1M"
        if string_format == "binary":
            return "fixture-binary-payload"
        if normalized in {"url", "uri", "publicweburl"}:
            return "https://example.invalid/fixture"
        if normalized == "username":
            return "fixture-user"
        if normalized.endswith("id"):
            return "fixture-id"
        if normalized in {"path", "filepath", "mediapath", "sourcepath"}:
            return "fixture-media.mkv"
        if normalized == "filename":
            return "fixture.m3u8"
        if normalized.endswith("at") or normalized in {"timestamp", "servertime"}:
            return "2026-09-29T12:00:00Z"
        if normalized in {"date", "releasedate", "premieredate"}:
            return "2026-09-29"
        examples = schema.get("examples")
        if examples:
            return copy.deepcopy(examples[0])
        if normalized in {"name", "title", "artist", "album", "label"}:
            return "Fixture Item"
        minimum = max(1, int(schema.get("minLength", 1)))
        return "x" * min(minimum, 16)

    if "default" in schema:
        return copy.deepcopy(schema["default"])
    if schema.get("nullable"):
        return None
    return {}


def _content_sample(document: dict, content: dict, *, request: bool):
    if not content:
        return None
    media_type = next(
        (
            value
            for value in content
            if value == "application/json" or value.endswith("+json")
        ),
        next(iter(content)),
    )
    media = content[media_type]
    schema = media.get("schema", {})
    return media_type, _sample(document, schema, request=request)


def _success_response(document: dict, operation: dict) -> dict:
    responses = operation.get("responses", {})
    candidates = sorted(
        (code for code in responses if code.startswith("2")),
        key=lambda code: (int(code), code),
    )
    if not candidates:
        raise ValueError(
            f"{operation['operationId']} has no documented success response"
        )
    status = candidates[0]
    response = responses[status]
    content = _content_sample(document, response.get("content", {}), request=False)
    if content is None:
        return {"status": int(status)}
    media_type, body = content
    schema = response["content"][media_type].get("schema", {})
    result = {"status": int(status), "contentType": media_type, "body": body}
    if "$ref" in schema:
        result["schemaRef"] = schema["$ref"]
    return result


def build_fixtures(document: dict) -> dict:
    fixtures = []
    for path, path_item in document["paths"].items():
        if not path.startswith("/api/") or path.startswith("/api/admin/"):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            parameters = {}
            path_values = {}
            for parameter in path_item.get("parameters", []) + operation.get(
                "parameters", []
            ):
                location = parameter["in"]
                value = _sample(
                    document,
                    parameter.get("schema", {}),
                    field=parameter["name"],
                    required=parameter.get("required", False),
                    request=True,
                )
                parameters.setdefault(location, {})[parameter["name"]] = value
                if location == "path":
                    path_values[parameter["name"]] = quote(str(value), safe="")

            rendered_path = re.sub(
                r"\{([^{}]+)\}",
                lambda match, values=path_values: values[match.group(1)],
                path,
            )
            request = {
                "method": method.upper(),
                "pathTemplate": path,
                "path": rendered_path,
                "parameters": parameters,
            }
            body_content = operation.get("requestBody", {}).get("content", {})
            sampled_body = _content_sample(document, body_content, request=True)
            if sampled_body is not None:
                media_type, body = sampled_body
                request["body"] = {"contentType": media_type, "value": body}

            fixtures.append(
                {
                    "operationId": operation["operationId"],
                    "request": request,
                    "response": _success_response(document, operation),
                }
            )

    return {"version": 1, "operations": fixtures}


def build_syncplay_fixtures(document: dict) -> dict:
    group = _sample(
        document,
        {"$ref": "#/components/schemas/SyncplayGroup"},
        field="group",
    )
    group["id"] = "fixture-group"
    group["hostUserId"] = "fixture-user"
    group["hostName"] = "Fixture Host"
    group["members"] = []
    group["ended"] = False
    group["playing"] = False
    group["resumeWhenReady"] = False
    group["position"] = 0.0
    group["allowViewerControls"] = False
    group["playbackState"] = "paused"
    group["revision"] = 7
    return {
        "version": 1,
        "channel": "/api/ws/syncplay",
        "messages": [
            {
                "name": "initial-groups",
                "direction": "server-to-client",
                "payload": {"version": 1, "type": "groups", "groups": [group]},
            },
            {
                "name": "group-update",
                "direction": "server-to-client",
                "payload": {"version": 1, "type": "group", "group": group},
            },
            {
                "name": "group-ended",
                "direction": "server-to-client",
                "payload": {
                    "version": 1,
                    "type": "group-ended",
                    "id": "fixture-group",
                    "revision": 8,
                },
            },
            {
                "name": "participant-replaced",
                "direction": "server-to-client",
                "payload": {
                    "version": 1,
                    "type": "participant-replaced",
                    "id": "fixture-group",
                    "revision": 9,
                },
            },
            {
                "name": "clock-request",
                "direction": "client-to-server",
                "payload": {
                    "version": 1,
                    "type": "clock",
                    "clientSentAt": 1_790_683_200_000,
                },
            },
            {
                "name": "clock-response",
                "direction": "server-to-client",
                "payload": {
                    "version": 1,
                    "type": "clock",
                    "clientSentAt": 1_790_683_200_000,
                    "serverReceivedAt": 1_790_683_200.025,
                    "serverSentAt": 1_790_683_200.026,
                },
            },
        ],
    }


def validate_fixtures(document: dict, fixtures: dict) -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.validators import RefResolver

    errors = []
    operations = {
        operation["operationId"]: (path, method, operation)
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    }
    fixture_operations = {}
    resolver = RefResolver.from_schema(document)

    def validate_payload(payload, schema, label):
        if schema:
            validator = Draft202012Validator(schema, resolver=resolver)
            for error in validator.iter_errors(payload):
                errors.append(
                    f"{label}: {error.message} at {list(error.absolute_path)}"
                )

    for fixture in fixtures.get("operations", []):
        operation_id = fixture.get("operationId")
        if operation_id in fixture_operations:
            errors.append(f"duplicate fixture operationId: {operation_id}")
            continue
        fixture_operations[operation_id] = fixture
        match = operations.get(operation_id)
        if match is None:
            errors.append(f"fixture refers to unknown operationId: {operation_id}")
            continue
        path, method, operation = match
        request = fixture.get("request", {})
        if request.get("method") != method.upper():
            errors.append(f"{operation_id}: request method does not match OpenAPI")
        if request.get("pathTemplate") != path:
            errors.append(
                f"{operation_id}: request path template does not match OpenAPI"
            )

        path_parameters = request.get("parameters", {}).get("path", {})
        expected_path = re.sub(
            r"\{([^{}]+)\}",
            lambda found, parameters=path_parameters: quote(
                str(parameters.get(found.group(1), "")), safe=""
            ),
            path,
        )
        if request.get("path") != expected_path:
            errors.append(
                f"{operation_id}: rendered request path does not match parameters"
            )

        for parameter in path_item_parameters(document, path, operation):
            location, name = parameter["in"], parameter["name"]
            values = request.get("parameters", {}).get(location, {})
            if parameter.get("required") and name not in values:
                errors.append(
                    f"{operation_id}: missing required {location} parameter {name}"
                )
                continue
            if name in values:
                validate_payload(
                    values[name],
                    parameter.get("schema", {}),
                    f"{operation_id} parameter {name}",
                )

        body = request.get("body")
        request_content = operation.get("requestBody", {}).get("content", {})
        if bool(body) != bool(request_content):
            errors.append(
                f"{operation_id}: request body presence does not match OpenAPI"
            )
        elif body:
            content_type = body.get("contentType")
            media = request_content.get(content_type)
            if media is None:
                errors.append(
                    f"{operation_id}: unsupported request content type {content_type}"
                )
            else:
                validate_payload(
                    body.get("value"),
                    media.get("schema", {}),
                    f"{operation_id} request body",
                )

        response = fixture.get("response", {})
        status = str(response.get("status"))
        if status not in operation.get("responses", {}):
            errors.append(f"{operation_id}: response status {status} is not documented")
            continue
        response_spec = operation["responses"][status]
        content_type = response.get("contentType")
        response_content = response_spec.get("content", {})
        if content_type is None:
            if "body" in response:
                errors.append(
                    f"{operation_id}: body provided for a response without content"
                )
        elif content_type not in response_content:
            errors.append(
                f"{operation_id}: unsupported response content type {content_type}"
            )
        else:
            expected_schema_ref = (
                response_content[content_type].get("schema", {}).get("$ref")
            )
            if response.get("schemaRef") != expected_schema_ref:
                errors.append(
                    f"{operation_id}: response schema reference does not match OpenAPI"
                )
            validate_payload(
                response.get("body"),
                response_content[content_type].get("schema", {}),
                f"{operation_id} response body",
            )

    expected = {
        operation_id
        for operation_id, (path, _method, _operation) in operations.items()
        if path.startswith("/api/") and not path.startswith("/api/admin/")
    }
    missing = sorted(expected - set(fixture_operations))
    if missing:
        errors.append(f"missing fixtures for operations: {', '.join(missing)}")
    return errors


def validate_syncplay_fixtures(document: dict, fixtures: dict) -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.validators import RefResolver

    errors = []
    if fixtures.get("channel") != "/api/ws/syncplay":
        errors.append("Syncplay fixtures must use /api/ws/syncplay")
    messages = fixtures.get("messages", [])
    expected = {
        ("server-to-client", "groups"),
        ("server-to-client", "group"),
        ("server-to-client", "group-ended"),
        ("server-to-client", "participant-replaced"),
        ("server-to-client", "clock"),
        ("client-to-server", "clock"),
    }
    actual = {
        (message.get("direction"), message.get("payload", {}).get("type"))
        for message in messages
    }
    if not expected.issubset(actual):
        errors.append(f"missing Syncplay message fixtures: {sorted(expected - actual)}")
    resolver = RefResolver.from_schema(document)
    group_schema = {"$ref": "#/components/schemas/SyncplayGroup"}
    for message in messages:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"{message.get('name', 'unnamed')} payload must be an object")
            continue
        kind = payload.get("type")
        if message.get("direction") not in {"client-to-server", "server-to-client"}:
            errors.append(f"{message.get('name', 'unnamed')} has an invalid direction")
        if kind == "group" and isinstance(payload.get("group"), dict):
            for error in Draft202012Validator(
                group_schema, resolver=resolver
            ).iter_errors(payload["group"]):
                errors.append(f"{message.get('name')}: {error.message}")
        elif kind == "groups" and isinstance(payload.get("groups"), list):
            for group in payload["groups"]:
                for error in Draft202012Validator(
                    group_schema, resolver=resolver
                ).iter_errors(group):
                    errors.append(f"{message.get('name')}: {error.message}")
    return errors


def path_item_parameters(document: dict, path: str, operation: dict) -> list[dict]:
    path_item = document["paths"][path]
    return path_item.get("parameters", []) + operation.get("parameters", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate the synthetic fixtures from the OpenAPI snapshot",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check fixture freshness and validate references and payloads",
    )
    args = parser.parse_args()
    document = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    expected = build_fixtures(document)
    expected_syncplay = build_syncplay_fixtures(document)

    if args.write:
        FIXTURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURES_PATH.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        SYNCPLAY_FIXTURES_PATH.write_text(
            json.dumps(expected_syncplay, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"Wrote {FIXTURES_PATH} ({len(expected['operations'])} operations).")
        return 0

    try:
        actual_text = FIXTURES_PATH.read_text(encoding="utf-8")
        actual = json.loads(actual_text)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        print(f"Invalid or missing fixtures: {error}", file=sys.stderr)
        return 1

    errors = validate_fixtures(document, actual)
    try:
        actual_syncplay_text = SYNCPLAY_FIXTURES_PATH.read_text(encoding="utf-8")
        actual_syncplay = json.loads(actual_syncplay_text)
        errors.extend(validate_syncplay_fixtures(document, actual_syncplay))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        actual_syncplay_text = None
        errors.append(f"Invalid or missing Syncplay fixtures: {error}")
    if args.check and actual_text != (
        json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ):
        errors.append(
            "HTTP fixtures are stale; run generate_contract_fixtures.py --write"
        )
    if args.check and actual_syncplay_text != (
        json.dumps(expected_syncplay, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ):
        errors.append(
            "Syncplay fixtures are stale; run generate_contract_fixtures.py --write"
        )
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Validated {len(actual['operations'])} HTTP operation fixtures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
