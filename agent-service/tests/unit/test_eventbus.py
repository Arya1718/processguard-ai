"""Unit tests for the EventBus itself (Prompt 1 abstraction, Prompt 10 gap-fill).

The EventBus coordinates other components rather than doing obviously
testable work, which is exactly why it was under-tested: agents were
exercised against an InMemoryBus double, and RedisEventBus internals --
envelope parsing, malformed-message handling, handler error isolation --
were only hit implicitly on the live stack.

These tests drive RedisEventBus._dispatch directly through a fake pubsub,
so the real production dispatch path (the code that will parse Service Bus
messages after the swap) is proven hermetically.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
os.environ.setdefault("PGAI_REDIS__HOST", "localhost")
os.environ.setdefault("PGAI_REDIS__PORT", "6379")
os.environ.setdefault("PGAI_REDIS__PASSWORD", "")

from app.eventbus.base import RedisEventBus, utcnow_iso


class _FakePubSub:
    """Stand-in for redis.asyncio.PubSub: records subscriptions, replays
    queued messages, then blocks (returns None) so the listener idles."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.subscribed: list[str] = []

    async def subscribe(self, *topics: str) -> None:
        self.subscribed.extend(topics)

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def close(self):
        return None


class _FakeRedis:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub
        self.published: list[tuple[str, str]] = []

    def pubsub(self):
        return self._pubsub

    async def publish(self, topic: str, raw: str) -> int:
        self.published.append((topic, raw))
        return 1


def _bus_with(messages: list[dict]) -> tuple[RedisEventBus, _FakeRedis]:
    pubsub = _FakePubSub(messages)
    fake_redis = _FakeRedis(pubsub)
    bus = RedisEventBus("redis://localhost:6399/0")
    bus._redis = fake_redis  # inject: no live Redis in unit tests
    return bus, fake_redis


def _msg(topic: str, payload: dict) -> dict:
    envelope = {"topic": topic, "payload": payload, "published_at": utcnow_iso()}
    return {"channel": topic, "data": json.dumps(envelope)}


@pytest.mark.asyncio
async def test_dispatch_delivers_envelope_payload_to_subscribers():
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    bus, _ = _bus_with([_msg("pgai.anomalies", {"incident_id": "i-1"})])
    await bus.subscribe("pgai.anomalies", handler)
    await bus._dispatch(_msg("pgai.anomalies", {"incident_id": "i-1"}))

    assert received == [{"incident_id": "i-1"}]


@pytest.mark.asyncio
async def test_dispatch_drops_malformed_json_without_raising():
    """A malformed message must be logged and dropped, never crash the
    listener -- one bad publisher cannot take down consumption."""
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    bus, _ = _bus_with([])
    await bus.subscribe("pgai.anomalies", handler)
    # Neither raises nor delivers:
    await bus._dispatch({"channel": "pgai.anomalies", "data": "not-json{{"})
    await bus._dispatch({"channel": "pgai.anomalies", "data": 12345})
    assert received == []


@pytest.mark.asyncio
async def test_handler_error_is_isolated_from_other_handlers():
    """One failing handler must not prevent the second handler from
    receiving the same event, nor raise out of _dispatch."""
    received: list[dict] = []

    async def bad_handler(payload: dict) -> None:
        raise RuntimeError("agent crashed")

    async def good_handler(payload: dict) -> None:
        received.append(payload)

    bus, _ = _bus_with([])
    await bus.subscribe("pgai.anomalies", bad_handler)
    await bus.subscribe("pgai.anomalies", good_handler)
    await bus._dispatch(_msg("pgai.anomalies", {"incident_id": "i-2"}))
    assert received == [{"incident_id": "i-2"}]


@pytest.mark.asyncio
async def test_publish_wraps_payload_in_envelope():
    bus, fake_redis = _bus_with([])
    await bus.publish("pgai.anomalies", {"incident_id": "i-3"})
    assert len(fake_redis.published) == 1
    topic, raw = fake_redis.published[0]
    assert topic == "pgai.anomalies"
    envelope = json.loads(raw)
    assert envelope["topic"] == "pgai.anomalies"
    assert envelope["payload"] == {"incident_id": "i-3"}
    assert envelope["published_at"]  # envelope timestamp present


@pytest.mark.asyncio
async def test_start_subscribes_all_registered_topics_and_stop_cleans_up():
    pubsub = _FakePubSub([])
    fake_redis = _FakeRedis(pubsub)
    bus = RedisEventBus("redis://localhost:6399/0")
    bus._redis = fake_redis

    await bus.subscribe("topic-a", lambda p: None)
    await bus.subscribe("topic-b", lambda p: None)
    await bus.start()
    await asyncio.sleep(0.05)  # let the listener task spin once
    await bus.stop()

    assert set(pubsub.subscribed) == {"topic-a", "topic-b"}
    assert bus._running is False
    assert bus._listener_task is None


@pytest.mark.asyncio
async def test_listener_delivers_messages_end_to_end():
    """Full listener loop: start() -> queued message dispatched -> stop()."""
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    bus, _ = _bus_with([_msg("pgai.evidence", {"incident_id": "i-4"})])
    await bus.subscribe("pgai.evidence", handler)
    await bus.start()
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.02)
    await bus.stop()
    assert received == [{"incident_id": "i-4"}]


@pytest.mark.asyncio
async def test_unknown_topic_messages_are_ignored():
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    bus, _ = _bus_with([_msg("pgai.somebody.elses.topic", {"x": 1})])
    await bus.subscribe("pgai.anomalies", handler)
    await bus._dispatch(_msg("pgai.somebody.elses.topic", {"x": 1}))
    assert received == []
