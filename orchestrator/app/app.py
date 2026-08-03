import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.zenstream.application_routes import (
    _static_roots,
    hub,
    router as application_router,
)
from api.zenstream.client_routes import router as client_router
from api.zenstream.library_routes import router as library_router
from app.config import load_config
from app.foreground import active_requests, run_foreground, shutdown as shutdown_foreground
from app.catalog_read_model import CatalogReadModel
from app.jobs import scheduler as job_scheduler
from app.library import runtime as library_runtime
from app.metadata_services import asset_executor
from app.logging_config import get_logger
from app.playback import PlaybackManager
from app.models.account import Account
from version import __version__


request_logger = get_logger("http")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_config()
    if not os.getenv("SECRET_KEY"):
        raise RuntimeError("Environment variable `SECRET_KEY` not set")
    await asyncio.to_thread(CatalogReadModel().bootstrap)
    library_runtime.start()
    job_scheduler.start()
    async def maintain_sessions():
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(Account.flush_session_activity)

    maintenance_task = asyncio.create_task(maintain_sessions())
    try:
        yield
    finally:
        maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance_task
        await asyncio.to_thread(Account.flush_session_activity)
        PlaybackManager.stop_all()
        job_scheduler.stop()
        library_runtime.stop()
        asset_executor.shutdown()
        shutdown_foreground()
        await hub.broadcast({"type": "system", "event": "shutdown"})


app = FastAPI(
    title="ZenStream API",
    description="ZenStream Orchestrator API",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/swagger/",
    redoc_url="/api/redoc/",
    openapi_url="/api/openapi.json",
)

origins = [
    value.strip() for value in os.getenv("CORS_ORIGINS", "").split(",") if value.strip()
]
origins += [
    value
    for value in ("http://localhost:3000", "http://127.0.0.1:3000")
    if value not in origins
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["TOKEN"],
)


@app.middleware("http")
async def request_timing(request, call_next):
    started = time.perf_counter()
    authorization = request.headers.get("authorization")
    auth_started = time.perf_counter()
    auth_ms = 0.0
    if authorization:
        from app.client_auth import bearer_token
        from app.models.account import Account

        token = bearer_token(authorization)
        if token:
            request.state.authenticated = await run_foreground(Account().authenticate_token, token)
            auth_ms = (time.perf_counter() - auth_started) * 1000
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    log = request_logger.warning if duration_ms >= 500 else request_logger.debug
    log(
        "request complete method=%s path=%s status=%s duration_ms=%.1f auth_ms=%.1f foreground_active=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        auth_ms,
        active_requests(),
    )
    return response

app.include_router(client_router)
app.include_router(library_router)
app.include_router(application_router)

web_root, assets_root = _static_roots()
if assets_root.is_dir():
    app.mount("/assets", StaticFiles(directory=assets_root), name="assets")
if (web_root / "_next").is_dir():
    app.mount("/_next", StaticFiles(directory=web_root / "_next"), name="next-assets")
if (web_root / "icons").is_dir():
    app.mount("/icons", StaticFiles(directory=web_root / "icons"), name="web-icons")
