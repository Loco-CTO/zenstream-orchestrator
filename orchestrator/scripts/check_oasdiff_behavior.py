from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPOSITORY_ROOT / "contracts" / "openapi.json"


def compare(oasdiff: str, base: dict, revision: dict, *, should_break: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="zenstream-oasdiff-") as directory:
        base_path = Path(directory) / "base.json"
        revision_path = Path(directory) / "revision.json"
        base_path.write_text(json.dumps(base), encoding="utf-8")
        revision_path.write_text(json.dumps(revision), encoding="utf-8")
        result = subprocess.run(
            [
                oasdiff,
                "breaking",
                str(base_path),
                str(revision_path),
                "--fail-on",
                "WARN",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    broke = result.returncode != 0
    if broke != should_break:
        expected = "blocking" if should_break else "non-breaking"
        actual = "blocking" if broke else "non-breaking"
        raise AssertionError(
            f"oasdiff classified the {expected} compatibility case as {actual}:\n"
            f"{result.stdout}{result.stderr}"
        )


def main() -> int:
    oasdiff = shutil.which("oasdiff")
    if not oasdiff:
        print("oasdiff is not installed", file=sys.stderr)
        return 2
    base = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))

    optional_field = copy.deepcopy(base)
    optional_field["components"]["schemas"]["VersionResponse"]["properties"][
        "contractFixtureOptional"
    ] = {"type": "string"}
    compare(oasdiff, base, optional_field, should_break=False)

    removed_operation = copy.deepcopy(base)
    del removed_operation["paths"]["/api/version"]["get"]
    compare(oasdiff, base, removed_operation, should_break=True)

    required_request_field = copy.deepcopy(base)
    credentials_ref = required_request_field["paths"]["/api/auth/login"]["post"]
    credentials_ref = credentials_ref["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"].rsplit("/", 1)[-1]
    credentials = required_request_field["components"]["schemas"][credentials_ref]
    credentials.setdefault("properties", {})["contractFixtureRequired"] = {
        "type": "string"
    }
    credentials.setdefault("required", []).append("contractFixtureRequired")
    compare(oasdiff, base, required_request_field, should_break=True)

    removed_response_field = copy.deepcopy(base)
    response_schema = removed_response_field["components"]["schemas"][
        "NotificationDeleteResponse"
    ]
    del response_schema["properties"]["id"]
    response_schema["required"].remove("id")
    compare(oasdiff, base, removed_response_field, should_break=True)

    changed_response_type = copy.deepcopy(base)
    main = changed_response_type["components"]["schemas"]["VersionResponse"][
        "properties"
    ]["main"]
    if "anyOf" in main:
        main["anyOf"][0] = {"type": "integer"}
    else:
        main["type"] = "integer"
    compare(oasdiff, base, changed_response_type, should_break=True)

    print("oasdiff compatibility acceptance cases passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
