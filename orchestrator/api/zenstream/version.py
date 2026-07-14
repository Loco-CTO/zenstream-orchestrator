import json
from pathlib import Path

from flask_restx import Resource

from version import __version__
from . import api_namespace_zs


@api_namespace_zs.route("version")
class Version(Resource):
    def get(self):
        return {"version": __version__, "main": _main_version()}, 200


def _main_version():
    try:
        metadata = json.loads((Path(__file__).parents[3] / ".main-version.json").read_text(encoding="utf-8"))
        main = metadata.get("main", 0)
        return main if isinstance(main, int) and main >= 0 else 0
    except (OSError, ValueError, TypeError):
        return 0
