from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_ROOT = REPOSITORY_ROOT / "orchestrator"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "contracts" / "openapi.json"


def render_openapi() -> str:
    """Render the live app schema without touching a developer's database."""
    previous_metadata_path = os.environ.get("METADATA_PATH")
    original_path = list(sys.path)

    with tempfile.TemporaryDirectory(prefix="zenstream-openapi-") as metadata_path:
        os.environ["METADATA_PATH"] = metadata_path
        sys.path.insert(0, str(ORCHESTRATOR_ROOT))
        try:
            # Importing the app initializes its metadata database and bootstraps
            # a root admin on a fresh database. Keep that isolated and silent.
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                from app.app import app

                schema = app.openapi()
        finally:
            config = sys.modules.get("app.config")
            config_type = getattr(config, "Config", None)
            if config_type is not None and config_type._instance is not None:
                config_type._instance.database.close()
            logger = logging.getLogger("zenstream")
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()
            sys.path[:] = original_path
            if previous_metadata_path is None:
                os.environ.pop("METADATA_PATH", None)
            else:
                os.environ["METADATA_PATH"] = previous_metadata_path

    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output path (default: {DEFAULT_OUTPUT.relative_to(REPOSITORY_ROOT)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in snapshot differs from the running app",
    )
    args = parser.parse_args()

    rendered = render_openapi()
    if args.check:
        try:
            current = args.output.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current != rendered:
            print(
                f"{args.output} is stale; run the exporter to refresh it.",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} matches the running Orchestrator app.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
