"""FastAPI middleware that records Prometheus HTTP metrics per endpoint.

This is a lightweight middleware (not OTel) that fills the HTTP_REQUEST_COUNT,
HTTP_REQUEST_DURATION, and HTTP_REQUEST_ERRORS counters. The /metrics endpoint
is registered separately as a raw ASGI app so Prometheus can scrape it.
"""
from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import Response

from app.core.observability import HTTP_REQUEST_COUNT, HTTP_REQUEST_DURATION


async def _record_error(path: str, method: str) -> None:
    HTTP_REQUEST_COUNT.labels(method=method, path=path, status_code="5xx").inc()


METRIC_PATHS = {
    "/api/v1/health/live",
    "/api/v1/health/ready",
    "/api/v1/health/startup",
    "/metrics",
}


class MetricsMiddleware:
    """Records per-endpoint HTTP metrics: request count, latency, errors."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        method = request.method

        # Don't record metrics for Prometheus's own scrape or health checks.
        if path in METRIC_PATHS:
            await self._app(scope, receive, send)
            return

        start = time.monotonic()
        status_code = "500"

        async def _send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = str(message.get("status", 500))
            await send(message)

        try:
            await self._app(scope, receive, _send_wrapper)
        except Exception:
            HTTP_REQUEST_COUNT.labels(method=method, path=path, status_code="500").inc()
            raise
        finally:
            elapsed = time.monotonic() - start
            HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(elapsed)
            HTTP_REQUEST_COUNT.labels(method=method, path=path, status_code=status_code).inc()
