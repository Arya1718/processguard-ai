"""Request correlation middleware for the agent service.

Mirrors the .NET middleware: honors an incoming X-Correlation-Id (or mints
one), binds it to a contextvar so every JSON log line in the request carries
it, and echoes it on the response.
"""
from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging_config import correlation_id_var, get_logger

logger = get_logger(__name__)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, header_name: str = "X-Correlation-Id") -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next):
        # BaseHTTPMiddleware is deprecated in recent Starlette for new code, but
        # remains correct and stable for this FastAPI pin (0.115.x).
        incoming = request.headers.get(self.header_name, "").strip()
        correlation_id = incoming[:128] if incoming else uuid.uuid4().hex

        token = correlation_id_var.set(correlation_id)
        try:
            logger.info(
                "Request started %s %s",
                request.method,
                request.url.path,
                extra={"correlation_id": correlation_id},
            )
            response = await call_next(request)
            response.headers[self.header_name] = correlation_id
            logger.info(
                "Request finished %s %s status=%d",
                request.method,
                request.url.path,
                response.status_code,
                extra={"correlation_id": correlation_id},
            )
            return response
        finally:
            correlation_id_var.reset(token)
