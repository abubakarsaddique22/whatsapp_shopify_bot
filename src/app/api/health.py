"""Liveness and readiness. Neither returns secrets and neither calls external APIs."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db.session import check_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: the database is reachable."""
    if await check_db():
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "database_unavailable"}, status_code=503)
