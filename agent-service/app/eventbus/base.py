"""EventBus abstraction over pub/sub messaging.

This module defines the transport-agnostic interface agents use to publish
and consume events. The active implementation is Redis pub/sub, which is a
first-pass STAND-IN FOR AZURE SERVICE BUS. A second implementation (e.g.
AzureServiceBusEventBus) can be swapped in without changing any agent
code -- agents only ever depend on the EventBus interface.
"""
from __future__ import annotations

import abc
import asyncio
import json
from datetime import datetime, timezone

from app.core.logging_config import get_logger
from app.core.observability import (
    EVENTBUS_CONSUME_COUNT,
    EVENTBUS_CONSUME_LATENCY,
    EVENTBUS_HANDLER_ERRORS,
    EVENTBUS_PUBLISH_COUNT,
    get_tracer,
)

logger = get_logger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventBus(abc.ABC):
    """Transport-agnostic pub/sub interface used by all agents.

    NOTE: this Redis-backed implementation is a stand-in for Azure Service
    Bus. A second implementation could be swapped in (same interface) without
    changing any agent code.
    """

    @abc.abstractmethod
    async def publish(self, topic: str, payload: dict) -> None:
        """Publish a JSON payload to a topic."""

    @abc.abstractmethod
    async def subscribe(self, topic: str, handler) -> None:
        """Register a handler coroutine(payload: dict) for a topic."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin consuming on all subscribed topics."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop consuming and release connections."""


class RedisEventBus(EventBus):
    """Redis pub/sub implementation of EventBus.

    NOTE: this is a stand-in for Azure Service Bus; swap via a second
    EventBus implementation without touching agent code.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis = None
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None
        self._handlers: dict[str, list] = {}
        self._running = False

    async def _connect(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def publish(self, topic: str, payload: dict) -> None:
        redis = await self._connect()
        envelope = {
            "topic": topic,
            "payload": payload,
            "published_at": utcnow_iso(),
        }
        tracer = get_tracer()
        with tracer.start_as_current_span(
            "eventbus.publish", attributes={"topic": topic}
        ):
            await redis.publish(topic, json.dumps(envelope))
        logger.info("Published to topic=%s", topic)
        EVENTBUS_PUBLISH_COUNT.labels(topic=topic).inc()

    async def subscribe(self, topic: str, handler) -> None:
        self._handlers.setdefault(topic, []).append(handler)
        logger.info("Subscribed handler to topic=%s", topic)

    async def start(self) -> None:
        if self._running:
            return
        redis = await self._connect()
        self._pubsub = redis.pubsub()
        for topic in self._handlers:
            await self._pubsub.subscribe(topic)
        self._running = True
        self._listener_task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        if self._pubsub:
            await self._pubsub.close()
            self._pubsub = None

    async def _listen(self) -> None:
        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                await self._dispatch(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the listener alive
                logger.error("EventBus listener error: %s", exc)

    async def _dispatch(self, message: dict) -> None:
        topic = message.get("channel", "")
        raw = message.get("data", "")
        tracer = get_tracer()
        with tracer.start_as_current_span(
            "eventbus.consume", attributes={"topic": topic}
        ):
            try:
                envelope = json.loads(raw)
            except (TypeError, ValueError):
                logger.error("Dropping malformed message on topic=%s", topic)
                return
            for handler in self._handlers.get(topic, []):
                start = __import__("time").monotonic()
                try:
                    result = handler(envelope["payload"])
                    if asyncio.iscoroutine(result):
                        await result
                    EVENTBUS_CONSUME_COUNT.labels(topic=topic).inc()
                    EVENTBUS_CONSUME_LATENCY.labels(topic=topic).observe(
                        __import__("time").monotonic() - start
                    )
                except Exception as exc:
                    EVENTBUS_HANDLER_ERRORS.labels(topic=topic).inc()
                    logger.error("Handler error on topic=%s: %s", topic, exc)
