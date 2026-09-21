"""Observability: Prometheus metrics + OpenTelemetry tracing for the agent service.

This module is the single source of truth for metrics definitions and the
tracer provider. It is a stand-in for Azure Monitor / Application Insights:
same concepts (metrics, traces, correlated by trace-id), real open-source
backends via docker-compose (Prometheus + Grafana + OTLP collector).

Prometheus metrics are exposed on /metrics (Prometheus scrape target).
OpenTelemetry traces are exported to the OTLP endpoint configured by
PGAI_OTEL__EXPORTER_URL (default: http://otel-collector:4317).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

from prometheus_client import Counter, Histogram, Gauge
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
except ImportError:  # opentelemetry-exporter-otlp-proto-grpc may not be installed in CI
    OTLPSpanExporter = None

# ContextVars: set by agents when they begin handling an incident, so the
# LLM client can tag token/cost metrics with the calling agent + incident.
current_agent_var: ContextVar[str | None] = ContextVar("current_agent", default=None)
current_incident_var: ContextVar[str | None] = ContextVar("current_incident", default=None)

# ---------------------------------------------------------------------------
# Prometheus metrics definitions
# ---------------------------------------------------------------------------

# Standard HTTP metrics (per endpoint)
HTTP_REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Request latency in seconds",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

# EventBus metrics
EVENTBUS_PUBLISH_COUNT = Counter(
    "pgai_eventbus_publish_total",
    "Total events published per topic",
    ["topic"],
)
EVENTBUS_CONSUME_COUNT = Counter(
    "pgai_eventbus_consume_total",
    "Total events consumed per topic",
    ["topic"],
)
EVENTBUS_CONSUME_LATENCY = Histogram(
    "pgai_eventbus_consume_duration_seconds",
    "Event handler execution latency per topic",
    ["topic"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)
EVENTBUS_HANDLER_ERRORS = Counter(
    "pgai_eventbus_handler_errors_total",
    "Total handler errors per topic",
    ["topic"],
)

# Redis Stream consumer lag (updated by the Detection Agent)
REDIS_STREAM_LAG = Gauge(
    "pgai_redis_stream_lag",
    "Messages behind the latest in the Redis Stream consumer group",
    ["stream", "group", "consumer"],
)

# Agent-specific telemetry
# LLM token consumption per call
LLM_TOKEN_USAGE = Counter(
    "pgai_llm_tokens_total",
    "Token consumption per LLM call",
    ["agent", "provider", "model", "incident_id"],
)

# LLM call count
LLM_CALL_COUNT = Counter(
    "pgai_llm_calls_total",
    "Number of LLM calls",
    ["agent", "provider", "model", "incident_id"],
)

# Estimated cost per LLM call (USD)
LLM_COST_USD = Counter(
    "pgai_llm_cost_usd_total",
    "Estimated cumulative LLM cost per agent (USD)",
    ["agent", "provider", "model", "incident_id"],
)

# Agent stage outcomes: success (no correction needed) vs needs_human_review vs rejected
AGENT_OUTCOME_COUNT = Counter(
    "pgai_agent_outcomes_total",
    "Agent output outcomes (accepted / needs_human_review / rejected)",
    ["agent", "outcome"],
)

# Root-Cause Agent confidence distribution (histogram buckets: 0-20, 20-40, ..., 80-100)
ROOT_CAUSE_CONFIDENCE = Histogram(
    "pgai_root_cause_confidence",
    "Root-Cause Agent confidence score distribution",
    buckets=[0, 20, 40, 60, 80, 100],
)

# Human override rate: approved vs rejected at HITL
HITL_DECISION_COUNT = Counter(
    "pgai_hitl_decisions_total",
    "HITL approval decisions",
    ["decision", "hitl_level"],
)

# Cost stored per incident (for cost-per-incident reporting)
INCIDENT_COST_USD = Counter(
    "pgai_incident_cost_usd_total",
    "Cumulative LLM cost attributed to a single incident",
    ["incident_id"],
)

# Agent pipeline stage duration
AGENT_STAGE_DURATION = Histogram(
    "pgai_agent_stage_duration_seconds",
    "Duration of each agent stage per incident",
    ["agent", "stage"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

# ---------------------------------------------------------------------------
# Cost calculation logic
# ---------------------------------------------------------------------------

# Groq pricing per 1M tokens (USD), configurable at runtime.
# Source: https://groq.com/pricing (as of 2026-09-20).
# This is a default; override via PGAI_LLM__COST_PER_1M_INPUT and
# PGAI_LLM__COST_PER_1M_OUTPUT environment variables.
DEFAULT_COST_PER_1M: dict[str, dict[str, float]] = {
    "llama-3.3-70b-versatile": {
        "input": 0.05,   # $0.05 / 1M input tokens
        "output": 0.10,  # $0.10 / 1M output tokens
    },
    "llama-3.1-8b": {
        "input": 0.01,
        "output": 0.03,
    },
    "llama-3.1-405b": {
        "input": 0.70,
        "output": 0.70,
    },
    # Fallback for any model not explicitly listed; conservative default.
    "default": {
        "input": 0.05,
        "output": 0.10,
    },
}


def _cost_config() -> dict[str, dict[str, float]]:
    """Build the cost table from env vars, falling back to defaults.

    PGAI_LLM__COST_PER_1M_INPUT and PGAI_LLM__COST_PER_1M_OUTPUT set a
    single flat rate applied to all models (useful for quick overrides).
    """
    flat_input = os.environ.get("PGAI_LLM__COST_PER_1M_INPUT")
    flat_output = os.environ.get("PGAI_LLM__COST_PER_1M_OUTPUT")
    if flat_input and flat_output:
        return {"_flat": {"input": float(flat_input), "output": float(flat_output)}}
    return DEFAULT_COST_PER_1M


def estimate_cost_usd(model: str, usage: dict) -> float:
    """Compute the estimated USD cost for one LLM call from token usage.

    Usage dict shape (from Groq/OpenAI chat-completions response):
        {"prompt_tokens": N, "completion_tokens": M, "total_tokens": T}

    Cost = (prompt_tokens / 1M * input_rate) + (completion_tokens / 1M * output_rate).

    For an untested model not in the cost table, falls back to 'default'.
    """
    config = _cost_config()
    rates = config.get("_flat") or config.get(model, config["default"])
    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    if prompt == 0 and completion == 0:
        total = usage.get("total_tokens", 0)
        prompt = total * 0.8
        completion = total * 0.2
    input_cost = (prompt / 1_000_000) * rates["input"]
    output_cost = (completion / 1_000_000) * rates["output"]
    return round(input_cost + output_cost, 8)


# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------

_OTEL_INITIALIZED = False


def init_telemetry(service_name: str, otlp_endpoint: str | None = None) -> None:
    """Initialize the OTLP tracer provider.

    otlp_endpoint defaults to http://otel-collector:4317 (the in-compose
    OpenTelemetry collector). Set PGAI_OTEL__EXPORTER_URL to override; set
    PGAI_OTEL__DISABLE=true to skip init entirely (e.g. unit tests).
    """
    global _OTEL_INITIALIZED
    if _OTEL_INITIALIZED:
        return
    if os.environ.get("PGAI_OTEL__DISABLE", "false").lower() == "true":
        return

    endpoint = otlp_endpoint or os.environ.get(
        "PGAI_OTEL__EXPORTER_URL", "http://otel-collector:4317"
    )

    resource = Resource.create({
        "service.name": service_name,
        "service.instance.id": os.environ.get("HOSTNAME", "unknown"),
    })

    provider = TracerProvider(resource=resource)
    if OTLPSpanExporter is not None:
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _OTEL_INITIALIZED = True


def get_tracer(name: str = "processguard"):
    """Return the configured tracer (no-op-safe; returns a no-op tracer if
    init_telemetry was not called)."""
    return trace.get_tracer(name)


@contextmanager
def record_agent_stage(agent_name: str, stage: str):
    """Context manager that records agent stage duration as a histogram
    sample and returns the elapsed seconds on exit."""
    import time
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        AGENT_STAGE_DURATION.labels(agent=agent_name, stage=stage).observe(elapsed)


def instrument_fastapi_app(app, service_name: str) -> None:
    """Apply FastAPI OTel instrumentation. Call after app creation."""
    init_telemetry(service_name)
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, tracer_provider=trace.get_tracer_provider())
    except ImportError:
        # Instrumentation packages not installed (e.g. CI unit tests).
        pass
