"""Structured JSON logging for the agent service.

Every log line is a single-line JSON object carrying at minimum:
timestamp, level, service, and correlation_id. Correlation IDs are bound
per request via contextvars (set by the correlation middleware).
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# Set by correlation middleware for each request; defaults for out-of-band logs.
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class JsonFormatter(logging.Formatter):
    """Formats every record as one JSON line with the required fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "processguard-agent-service"),
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", correlation_id_var.get()),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger (idempotent for tests)."""
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.LoggerAdapter:
    """Logger that tags every record with the service name."""
    return logging.LoggerAdapter(logging.getLogger(name), {"service": "processguard-agent-service"})
