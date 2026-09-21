"""Stub ping subscriber -- the EventBus smoke test.

Subscribes a handler that logs everything it receives on the ping topic,
and exposes a helper that publishes a one-off ping. Together these prove
the Redis pub/sub path works before any real agent logic exists (Prompt 2+).
"""
from __future__ import annotations

from app.core.logging_config import get_logger
from app.eventbus.base import EventBus, utcnow_iso

logger = get_logger(__name__)

PING_TOPIC = "pgai.ping"


async def handle_ping(payload: dict) -> None:
    """Stub subscriber handler: logs what it receives."""
    logger.info("EventBus ping received: %s", payload)


async def register_ping_stub(bus: EventBus) -> None:
    """Subscribe the stub handler. Call before bus.start()."""
    await bus.subscribe(PING_TOPIC, handle_ping)


async def publish_startup_ping(bus: EventBus) -> None:
    """Publish one ping. Call AFTER bus.start() so the subscriber receives it."""
    await bus.publish(PING_TOPIC, {"msg": "bus-ok", "at": utcnow_iso()})
