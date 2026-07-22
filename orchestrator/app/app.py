"""FastAPI application composition and service lifespan."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.zenstream.application_routes import _static_roots, hub, router as application_router
from api.zenstream.client_routes import router as client_router
from api.zenstream.library_routes import router as library_router
from app.config import load_config
from app.jobs import scheduler as job_scheduler
from app.library import runtime as library_runtime
from version import __version__


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_config()
    if not os.getenv("SECRET_KEY"):
        raise RuntimeError("Environment variable `SECRET_KEY` not set")
    library_runtime.start()
    job_scheduler.start()
    try:
        yield
    finally:
        job_scheduler.stop()
        library_runtime.stop()
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

origins = [value.strip() for value in os.getenv("CORS_ORIGINS", "").split(",") if value.strip()]
origins += [value for value in ("http://localhost:3000", "http://127.0.0.1:3000") if value not in origins]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["TOKEN"],
)

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
