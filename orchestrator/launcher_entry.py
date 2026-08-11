from __future__ import annotations

import multiprocessing
import os
import sys
import threading

import uvicorn
from dotenv import load_dotenv


def _watch_launcher(server: uvicorn.Server) -> None:
    """Stop Uvicorn when the launcher requests shutdown or closes its pipe."""
    try:
        while True:
            command = sys.stdin.readline()
            if not command or command.strip().lower() == "shutdown":
                server.should_exit = True
                return
    except (OSError, ValueError):
        server.should_exit = True


def main() -> int:
    load_dotenv()
    config = uvicorn.Config(
        "app.app:app",
        host=os.getenv("ORCHESTRATOR_HOST", "127.0.0.1"),
        port=int(os.getenv("ORCHESTRATOR_PORT", "9088")),
        reload=False,
    )
    server = uvicorn.Server(config)
    watcher = threading.Thread(
        target=_watch_launcher,
        args=(server,),
        name="zenstream-launcher-control",
        daemon=True,
    )
    watcher.start()
    server.run()
    return 0 if server.started else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
