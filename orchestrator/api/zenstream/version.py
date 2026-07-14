import json
from pathlib import Path

from flask_restx import Resource

from version import __version__
from . import api_namespace_zs


@api_namespace_zs.route("version")
class Version(Resource):
    def get(self):
        try:
            metadata = json.loads((Path(__file__).parents[3] / ".main-version.json").read_text(encoding="utf-8"))
            main = metadata.get("main", 0)
            if not isinstance(main, int) or main < 0:
                main = 0
        except (OSError, ValueError, TypeError):
            main = 0
        return {"version": __version__, "main": main}, 200
