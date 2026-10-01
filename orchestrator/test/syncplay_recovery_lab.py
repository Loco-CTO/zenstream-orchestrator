"""Loopback-only mixed-client lab; uses production SyncPlay routes on a temporary DB.

Run from orchestrator/: python test/syncplay_recovery_lab.py --port 19090.
The synthetic authentication and fault endpoints exist only in this test process.
"""

import argparse
import contextlib
import io
import os
import sys
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19090)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="zenstream-syncplay-recovery-") as root:
        os.environ["METADATA_PATH"] = root
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            from api.zenstream import application_routes as routes
            from app.config import Config
            from app.foreground import run_control
            from app.models.syncplay import SyncplayGroup

        import uvicorn
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse

        users = {
            "lab-web": {"id": "lab-web", "username": "Web"},
            "lab-mobile": {"id": "lab-mobile", "username": "Android"},
        }
        faults = Counter()
        attempts = Counter()
        phase = {"value": "join"}

        def account(request):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            user = users.get(token)
            if user is None:
                raise HTTPException(401, "Synthetic lab authentication required")
            return user, token

        routes.require_account = account
        routes.websocket_account = lambda socket: users.get(
            socket.query_params.get("ticket")
        )

        @asynccontextmanager
        async def lifespan(app):
            yield
            await routes.hub.shutdown()
            Config().database.close()

        app = FastAPI(lifespan=lifespan)

        @app.middleware("http")
        async def inject(request: Request, call_next):
            user = request.headers.get("authorization", "").removeprefix("Bearer ")
            kind = (
                "presence"
                if request.url.path.endswith("/presence")
                else "snapshot"
                if request.url.path == "/api/syncplay/groups"
                and request.method == "GET"
                else None
            )
            if kind:
                attempts[f"{user}:{kind}"] += 1
                if faults[f"{user}:{kind}"]:
                    faults[f"{user}:{kind}"] -= 1
                    return JSONResponse(
                        {"detail": "Injected transient failure"}, status_code=503
                    )
            return await call_next(request)

        @app.post("/api/auth/socket-ticket")
        async def ticket(request: Request):
            _, token = account(request)
            return {"ticket": token}

        @app.get("/__test/state")
        async def state():
            return {
                "groups": await run_control(SyncplayGroup.states),
                "attempts": dict(attempts),
                "phase": phase["value"],
            }

        @app.post("/__test/phase/{value}")
        async def change_phase(value: str):
            phase["value"] = value
            return {"phase": value}

        @app.post("/__test/disrupt")
        async def disrupt():
            for user in users:
                faults[f"{user}:snapshot"] = 1
                faults[f"{user}:presence"] = 1
            for socket, (user, _) in tuple(routes.hub.identities.items()):
                if user in users:
                    await socket.close(code=1012, reason="Injected reconnect")
            return {"disrupted": True}

        app.include_router(routes.router)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
