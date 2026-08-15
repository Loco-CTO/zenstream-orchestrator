import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager

from api.zenstream.application_routes import (
    _static_roots,
    hub,
)
from api.zenstream.application_routes import (
    router as application_router,
)
from api.zenstream.client_routes import router as client_router
from api.zenstream.library_routes import router as library_router
from app.catalog_read_model import CatalogReadModel
from app.client_auth import browser_origins
from app.config import Config, load_config
from app.foreground import (
    active_auth_work,
    active_control_work,
    active_requests,
    metrics as foreground_metrics,
    run_auth,
    run_control,
    run_foreground,
)
from app.foreground import shutdown as shutdown_foreground
from app.jobs import scheduler as job_scheduler
from app.library import runtime as library_runtime
from app.logging_config import get_logger
from app.metadata_services import asset_executor
from app.models.account import Account
from app.playback import PlaybackManager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from version import __version__

request_logger = get_logger("http")


def _authenticate_bearer(token: str):
    return Account().authenticate_token(token)


def _database_metrics():
    instance = Config._instance
    return instance.database.metrics() if instance is not None else {}


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

    async def monitor_event_loop():
        """Report event-loop stalls without adding work to request handlers."""
        loop = asyncio.get_running_loop()
        interval = 0.1
        expected = loop.time() + interval
        while True:
            await asyncio.sleep(interval)
            current = loop.time()
            lag = current - expected
            if lag >= 0.25:
                socket_metrics = await hub.queue_metrics()
                get_logger("event_loop").warning(
                    "event loop lag lag_ms=%.1f foreground_active=%s control_active=%s auth_active=%s foreground_metrics=%s sqlite_metrics=%s websocket_metrics=%s",
                    lag * 1000,
                    active_requests(),
                    active_control_work(),
                    active_auth_work(),
                    foreground_metrics(),
                    _database_metrics(),
                    socket_metrics,
                )
            expected = current + interval

    maintenance_task = asyncio.create_task(maintain_sessions())
    event_loop_task = asyncio.create_task(monitor_event_loop())
    try:
        yield
    finally:
        for task in (maintenance_task, event_loop_task):
            task.cancel()
        for task in (maintenance_task, event_loop_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await run_control(Account.flush_session_activity)
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=browser_origins(),
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

        token = bearer_token(authorization)
        if token:
            request.state.authenticated = await run_auth(_authenticate_bearer, token)
            auth_ms = (time.perf_counter() - auth_started) * 1000
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    log = request_logger.warning if duration_ms >= 500 else request_logger.debug
    log(
        "request complete method=%s path=%s status=%s duration_ms=%.1f auth_ms=%.1f foreground_active=%s control_active=%s auth_active=%s foreground_metrics=%s sqlite_metrics=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        auth_ms,
        active_requests(),
        active_control_work(),
        active_auth_work(),
        foreground_metrics(),
        _database_metrics(),
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
