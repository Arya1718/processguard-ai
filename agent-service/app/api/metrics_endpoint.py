"""Prometheus metrics endpoint for the agent service.

Serves the /metrics scrape endpoint in Prometheus text exposition format.
This is a standalone ASGI app (not wrapped by the correlation/metrics
middleware) so Prometheus scraping doesn't pollute the observability stack.
"""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response


async def metrics_endpoint(request: Request) -> Response:
    """Return Prometheus-format metrics for scraping."""
    data = generate_latest()
    return Response(
        content=data,
        media_type=CONTENT_TYPE_LATEST,
        headers={"Content-Encoding": "identity"},
    )
