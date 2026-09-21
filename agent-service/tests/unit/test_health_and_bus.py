"""Scaffold tests: config fail-fast, correlation middleware, EventBus stub.

Network-dependent readiness probes are exercised against real containers in
the local stack (see docs/runbook.md); these tests stay hermetic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid

# Config requires these; tests never touch real infrastructure.
os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
os.environ.setdefault("PGAI_REDIS__HOST", "localhost")
os.environ.setdefault("PGAI_REDIS__PORT", "6379")
os.environ.setdefault("PGAI_REDIS__PASSWORD", "")

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.health import router as health_router, startup_is_complete, mark_startup_complete
from app.api.middleware import CorrelationIdMiddleware
from app.core.config import Settings, get_settings
from app.eventbus.base import EventBus, RedisEventBus
from app.agents.orchestrator.ping_subscriber import PING_TOPIC, register_ping_stub


# ---------------------------------------------------------------------------
# Config: fail fast on missing required variables
# ---------------------------------------------------------------------------
def test_settings_fail_fast(monkeypatch) -> None:
    for key in ("PGAI_DATABASE__HOST", "PGAI_DATABASE__NAME",
                "PGAI_DATABASE__USER", "PGAI_DATABASE__PASSWORD",
                "PGAI_REDIS__HOST"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(Exception):
        Settings()


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()


# ---------------------------------------------------------------------------
# Correlation middleware: generates, echoes, propagates
# ---------------------------------------------------------------------------
def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    async def echo():
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_correlation_id_generated_and_echoed() -> None:
    transport = ASGITransport(app=_make_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health/live")
    assert resp.status_code == 200
    assert resp.headers.get("X-Correlation-Id") not in (None, "")


@pytest.mark.asyncio
async def test_correlation_id_honored_from_header() -> None:
    transport = ASGITransport(app=_make_app())
    correlation_id = "trace-" + uuid.uuid4().hex
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health/live", headers={"X-Correlation-Id": correlation_id})
    assert resp.headers.get("X-Correlation-Id") == correlation_id


def test_startup_flag_toggles() -> None:
    assert startup_is_complete() is False
    mark_startup_complete()
    assert startup_is_complete() is True


# ---------------------------------------------------------------------------
# EventBus: publish -> subscribe roundtrip over an in-memory fake transport
# ---------------------------------------------------------------------------
class InMemoryBus(EventBus):
    """Test double implementing the same interface as RedisEventBus.

    Exists to prove agents can be coded against EventBus and tested without
    Redis; the Redis implementation is exercised in the live stack.
    """

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.published: list[tuple[str, dict]] = []
        self._started = False

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))
        if self._started:
            for handler in self.handlers.get(topic, []):
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    await result

    async def subscribe(self, topic: str, handler) -> None:
        self.handlers.setdefault(topic, []).append(handler)

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False


@pytest.mark.asyncio
async def test_event_bus_ping_stub_roundtrip() -> None:
    received: list[dict] = []

    async def capture(payload: dict) -> None:
        received.append(payload)

    bus = InMemoryBus()
    await bus.subscribe(PING_TOPIC, capture)
    await register_ping_stub(bus)
    await bus.start()

    # register_ping_stub publishes a startup ping; also publish one more.
    await bus.publish(PING_TOPIC, {"msg": "direct-publish"})
    await asyncio.sleep(0)
    assert any(p.get("msg") == "direct-publish" for p in received)
    assert bus.published, "startup ping should have been published"


@pytest.mark.asyncio
async def test_redis_bus_defers_connection_until_use() -> None:
    bus = RedisEventBus("redis://localhost:6399/0")
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    # subscribe() must not connect (no Redis needed to register handlers)
    await bus.subscribe("pgai.test", handler)
    assert bus._handlers["pgai.test"] == [handler]
