"""Process-wide runtime handles shared between the lifespan wiring and the
API routers (a tiny service-locator so routers never import app.main).

Currently holds the EventBus only: the HITL decision endpoints publish
lifecycle events (e.g. pgai.incident_approved) on it. It is None in unit
tests, which is why every consumer guards with `if bus is not None`.
"""
from __future__ import annotations

from app.eventbus.base import EventBus

_event_bus: EventBus | None = None


def set_event_bus(bus: EventBus | None) -> None:
    global _event_bus
    _event_bus = bus


def get_event_bus() -> EventBus | None:
    return _event_bus
