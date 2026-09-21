"""Unit tests for CmmsClient's bounded retry/backoff (Prompt 5).

Uses an httpx MockTransport so the retry behavior of the real client is
proven without the network: 5xx responses and connection errors are retried
up to the attempt cap with linear backoff; 4xx responses are NEVER retried
(a rejected work order will not get better by resending).
"""
from __future__ import annotations

import httpx
import pytest

from app.core.cmms_client import CmmsClient, CmmsError

BASE = "http://cmms.test"


class CountingTransport(httpx.AsyncBaseTransport):
    """Serves a scripted list front-to-back; the LAST entry repeats for any
    attempt beyond the script (so a single failure script covers all tries)."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self._script = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        entry = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(entry, Exception):
            raise entry
        return entry

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        entry = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


def _client(transport: CountingTransport, attempts: int = 3, backoff: float = 0.01) -> CmmsClient:
    return CmmsClient(
        base_url=BASE, api_key="test-key", timeout_seconds=2.0,
        retry_attempts=attempts, retry_backoff_seconds=backoff,
        transport=transport,
    )


async def test_5xx_is_retried_up_to_the_cap_then_raises() -> None:
    transport = CountingTransport([
        httpx.Response(503, json={"detail": "overloaded"}),
    ])
    client = _client(transport, attempts=3)

    with pytest.raises(CmmsError) as exc:
        await client.get_work_order("wo-1")
    assert "503" in str(exc.value)
    assert len(transport.requests) == 3, "bounded retries: exactly the attempt cap"


async def test_4xx_is_never_retried() -> None:
    transport = CountingTransport([
        httpx.Response(422, json={"detail": "description is required"}),
    ])
    client = _client(transport, attempts=4)

    with pytest.raises(CmmsError) as exc:
        await client.create_work_order(
            equipment_id="eq-1", description="", priority="high",
            requested_by="test", source_incident_id="inc-1")
    assert "422" in str(exc.value)
    assert len(transport.requests) == 1, "a rejected request must not be resent"


async def test_success_after_transient_failure_succeeds() -> None:
    transport = CountingTransport([
        httpx.Response(503, json={"detail": "first attempt fails"}),
        httpx.Response(201, json={"id": "wo-9", "status": "open"}),
    ])
    client = _client(transport, attempts=3)

    result = await client.create_work_order(
        equipment_id="eq-1", description="Inspect pump", priority="high",
        requested_by="test", source_incident_id="inc-1")
    assert result["id"] == "wo-9"
    assert len(transport.requests) == 2


async def test_connection_error_is_retried_then_raises() -> None:
    transport = CountingTransport([
        httpx.ConnectError("connection refused"),
    ])
    client = _client(transport, attempts=2)

    with pytest.raises(CmmsError) as exc:
        await client.get_work_order("wo-1")
    assert "connection refused" in str(exc.value)
    assert len(transport.requests) == 2


async def test_api_key_header_is_sent() -> None:
    transport = CountingTransport([httpx.Response(200, json=[])])
    client = _client(transport)

    await client.list_work_orders()
    assert transport.requests[0].headers.get("X-Api-Key") == "test-key"
