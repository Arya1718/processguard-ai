"""Health endpoints for the agent service.

Mirrors the .NET middleware:
  /api/v1/health/live    -> {"status": "ok"} (no dependencies touched)
  /api/v1/health/ready   -> genuinely pings Postgres and Redis
  /api/v1/health/startup -> confirms init (bus started, probes exercised)
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.db import check_postgres
from app.core.logging_config import get_logger
from app.eventbus.base import EventBus

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/health", tags=["health"])

_startup_state = {"complete": False}


def mark_startup_complete() -> None:
    _startup_state["complete"] = True


def startup_is_complete() -> bool:
    return _startup_state["complete"]


@router.get("/live")
async def live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    settings = get_settings()

    postgres_ok = await check_postgres(settings.db_dsn())
    redis_ok = await check_redis(settings.redis_url())

    body = {
        "status": "ready" if (postgres_ok and redis_ok) else "degraded",
        "dependencies": {
            "postgres": "ok" if postgres_ok else "failed",
            "redis": "ok" if redis_ok else "failed",
        },
    }
    return JSONResponse(status_code=200 if (postgres_ok and redis_ok) else 503, content=body)


@router.get("/startup")
async def startup() -> JSONResponse:
    complete = startup_is_complete()
    return JSONResponse(
        status_code=200 if complete else 503,
        content={
            "status": "started" if complete else "not_started",
            "init": "complete" if complete else "pending",
        },
    )


async def check_redis(url: str) -> bool:
    """Genuine Redis probe used by /ready (shared with the test suite)."""
    import redis.asyncio as aioredis

    try:
        client = aioredis.from_url(url, decode_responses=True, socket_timeout=2)
        try:
            await client.ping()
            return True
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning("Redis probe failed: %s", exc)
        return False
