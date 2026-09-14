"""The human-facing Swagger UI wrapper.

FastAPI still owns the OpenAPI, OpenAPI JSON, and ReDoc routes. Only the
Swagger UI behavior is configured so the API contract remains available to
other tooling at its existing URL.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse


router = APIRouter()


@router.get("/api/swagger/", include_in_schema=False)
async def swagger_ui() -> HTMLResponse:
    """Serve Swagger UI with readable defaults and non-persistent auth."""

    response = get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="ZenStream API — Swagger UI",
        swagger_ui_parameters={
            "deepLinking": True,
            "displayRequestDuration": True,
            "filter": True,
            "defaultModelRendering": "model",
            "defaultModelExpandDepth": 1,
            "defaultModelsExpandDepth": 1,
            "docExpansion": "list",
            "operationsSorter": "none",
            "tagsSorter": "none",
            "persistAuthorization": False,
            "tryItOutEnabled": False,
        },
    )
    return HTMLResponse(response.body)
