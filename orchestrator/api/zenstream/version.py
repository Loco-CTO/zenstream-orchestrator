import json

from app.paths import PROJECT_ROOT
from version import __version__


class Version:
    def get(self):
        return {"version": __version__, "main": _main_version()}, 200


def _main_version():
    try:
        metadata = json.loads(
            (PROJECT_ROOT / ".main-version.json").read_text(encoding="utf-8")
        )
        main = metadata.get("main", 0)
        return main if isinstance(main, int) and main >= 0 else 0
    except (OSError, ValueError, TypeError):
        return 0
