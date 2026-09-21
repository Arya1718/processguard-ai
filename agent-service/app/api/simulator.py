"""Simulator control endpoints (dev/demo tooling).

The simulator runs as its own container; control flows through a short-TTL
Redis key so no process coupling is needed:

  SET pgai:simulator:control  trigger|reset   (consumed by the simulator)
  GET pgai:simulator:state                    (published by the simulator)

Endpoints (exposed via the .NET middleware passthrough):
  POST /api/v1/simulator/trigger-scenario/cooling-tower-incident
  POST /api/v1/simulator/reset
  GET  /api/v1/simulator/status
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.core.security import Principal, ensure_role, get_rbac_policy, require_principal

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/simulator", tags=["simulator"])

_redis: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url(), decode_responses=True)
    return _redis


@router.post("/trigger-scenario/cooling-tower-incident")
async def trigger_cooling_tower_incident(
    principal: Principal = Depends(require_principal),
) -> dict:
    # Second-layer RBAC: the .NET policy gates this already; re-checked here
    # from the forwarded token (defense in depth).
    ensure_role(principal, get_rbac_policy().simulator_roles, "control the simulator")
    redis = await _get_redis()
    await redis.set("pgai:simulator:control", "trigger", ex=60)
    logger.info("Cooling-tower incident scenario triggered (via control key)")
    return {"triggered": True, "scenario": "cooling-tower-incident"}


@router.post("/reset")
async def reset(principal: Principal = Depends(require_principal)) -> dict:
    ensure_role(principal, get_rbac_policy().simulator_roles, "control the simulator")
    redis = await _get_redis()
    await redis.set("pgai:simulator:control", "reset", ex=60)
    logger.info("Simulator reset to normal mode (via control key)")
    return {"reset": True, "mode": "normal"}


@router.get("/status")
async def status(principal: Principal = Depends(require_principal)) -> dict:
    redis = await _get_redis()
    raw = await redis.get("pgai:simulator:state")
    if raw:
        return json.loads(raw)
    return {
        "mode": "unknown",
        "note": "simulator publishes state to pgai:simulator:state asynchronously",
    }
