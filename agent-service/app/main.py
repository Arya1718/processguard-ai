"""ProcessGuard AI agent service entry point.

Wires configuration (fail-fast), structured JSON logging, correlation ID
middleware, the three health endpoints, the EventBus ping stub, the shared
Postgres pool + demo seed data, and the agents:

  * Detection Agent   (Prompt 2) -- sensor-readings stream consumer
  * Knowledge Agent   (Prompt 3) -- AnomalyDetected -> SOP/history evidence
  * Root-Cause Agent  (Prompt 3) -- evidence + anomaly -> cited hypothesis

plus the incidents / evidence / root-cause / simulator / grounded-ask API
routers.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from app.agents.detection.agent import DetectionAgent
from app.agents.action.agent import ActionAgent
from app.agents.knowledge.agent import KnowledgeAgent
from app.agents.recommendation.agent import RecommendationAgent
from app.agents.risk.agent import RiskAgent, load_hitl_thresholds
from app.agents.root_cause.agent import RootCauseAgent
from app.agents.orchestrator.agent import OrchestratorAgent
from app.agents.orchestrator.ping_subscriber import (
    publish_startup_ping,
    register_ping_stub,
)
from app.api.ask import router as ask_router
from app.api.auth import router as auth_router
from app.api.health import mark_startup_complete, router as health_router
from app.api.incidents import router as incidents_router
from app.api.metrics import MetricsMiddleware
from app.api.metrics_endpoint import metrics_endpoint
from app.api.middleware import CorrelationIdMiddleware
from app.api.simulator import router as simulator_router
from app.api.telemetry import router as telemetry_router
from app.core.observability import init_telemetry, instrument_fastapi_app
from app.core.config import get_settings
from app.core.db import close_db_pool, init_db_pool, seed_demo_data
from app.core.llm_client import get_llm_client
from app.core.logging_config import configure_logging, get_logger
from app.core.runtime import set_event_bus
from app.eventbus.base import EventBus, RedisEventBus
from app.rag.corpus import load_corpus
from app.rag.retrieval import RetrievalIndex
from app.simulator.service import SensorSimulator

logger = get_logger(__name__)

event_bus: EventBus | None = None
detection_agent: DetectionAgent | None = None
knowledge_agent: KnowledgeAgent | None = None
root_cause_agent: RootCauseAgent | None = None
risk_agent: RiskAgent | None = None
recommendation_agent: RecommendationAgent | None = None
orchestrator_agent: OrchestratorAgent | None = None
action_agent: ActionAgent | None = None
_sim_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global event_bus, detection_agent, knowledge_agent, root_cause_agent
    global risk_agent, recommendation_agent, orchestrator_agent, action_agent

    # Fail fast: load all required settings before serving any traffic.
    settings = get_settings()
    configure_logging(settings.log_level)

    pool = await init_db_pool(settings.db_dsn())
    await seed_demo_data()
    logger.info("Database pool ready and demo data seeded")

    event_bus = RedisEventBus(settings.redis_url())
    # API routers (the approve endpoint) publish lifecycle events through
    # this holder; main.py is the only place that sets it.
    set_event_bus(event_bus)

    # Prompt 3: retrieval index over the markdown knowledge base (local
    # TF-IDF stand-in for Azure AI Search) + the LLM client (Groq stand-in
    # for Azure OpenAI; fail-fast here if no key and fake mode is off).
    index = RetrievalIndex()
    indexed = index.index(load_corpus())
    llm = get_llm_client()
    logger.info("Retrieval index ready (%d chunks); LLM client resolved", indexed)

    # Agent subscriptions are registered BEFORE the bus listener starts so
    # no event is missed. Ordering between the two diagnosis agents is
    # deliberately flexible: Knowledge triggers on AnomalyDetected and
    # nudges Root-Cause via pgai.diagnosis; Root-Cause also subscribes to
    # AnomalyDetected directly and waits for evidence -- either finishing
    # first converges on the same result (see agents/root_cause/agent.py).
    knowledge_agent = KnowledgeAgent(index, event_bus)
    await knowledge_agent.start()
    root_cause_agent = RootCauseAgent(llm, event_bus)
    await root_cause_agent.start()

    # Prompt 4: load the HITL threshold table (fails fast if missing/corrupt)
    # and wire the Orchestrator as the coordination point. Stage completion
    # flows over the EventBus; the Orchestrator drives Risk -> Recommendation
    # and applies the final HITL gate.
    hitl_config = load_hitl_thresholds()
    risk_agent = RiskAgent(config=hitl_config)
    recommendation_agent = RecommendationAgent(llm, bus=event_bus)
    orchestrator_agent = OrchestratorAgent(
        event_bus, risk_agent=risk_agent, recommendation_agent=recommendation_agent
    )
    await orchestrator_agent.start()
    logger.info("Orchestrator wired: Risk + Recommendation agents ready (HITL config loaded)")

    # Prompt 5: the Action Agent -- the ONLY agent with external write
    # access (mock ERP/CMMS, separate credentials; see docs/security-model.md).
    # It acts strictly on pgai.incident_approved events emitted by the HITL
    # decision endpoint after a human approval.
    action_agent = ActionAgent(event_bus)
    await action_agent.start()
    logger.info("Action Agent wired: external execution gated on human approval")

    # Order matters: subscribe first, then start the listener, then publish
    # so the demo ping is actually received and logged by the subscriber.
    await register_ping_stub(event_bus)
    await event_bus.start()
    await publish_startup_ping(event_bus)

    redis_client = aioredis.from_url(settings.redis_url(), decode_responses=True)

    # Detection Agent: consumes the sensor-readings stream in the background.
    detection_agent = DetectionAgent(redis_client, pool, event_bus)
    await detection_agent.start()
    _sim_tasks.append(asyncio.create_task(detection_agent.consume_forever()))

    # Prompt 5: outcome tracking -- the Action Agent polls for CMMS work
    # orders resolved after execution and closes the learning loop
    # (incident -> resolved + a new HistoricalIncidents row).
    _sim_tasks.append(asyncio.create_task(action_agent.poll_resolutions_forever()))

    # Sensor simulator runs in a SEPARATE container in the compose stack
    # (see docker-compose.yml + docs/ingestion-pipeline.md). It can also be
    # embedded in this process via PGAI_SIMULATOR__EMBEDDED=true for tests.
    if settings.simulator_embedded:
        sim = SensorSimulator(redis_client, settings)
        await sim.load_sensors(pool)
        _sim_tasks.append(asyncio.create_task(sim.run()))

    logger.info("Agent service startup complete")
    mark_startup_complete()
    yield

    for task in _sim_tasks:
        task.cancel()
    await detection_agent.stop()
    await knowledge_agent.stop()
    await root_cause_agent.stop()
    await orchestrator_agent.stop()
    await action_agent.stop()
    set_event_bus(None)
    await event_bus.stop()
    await close_db_pool()
    logger.info("Agent service stopped")


app = FastAPI(
    title="ProcessGuard AI Agent Service",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(CorrelationIdMiddleware)
# Record HTTP metrics (request count, latency, errors per endpoint) BEFORE
# the metrics endpoint serves, so /metrics scraping is excluded from itself.
app.add_middleware(MetricsMiddleware)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(incidents_router)
app.include_router(ask_router)
app.include_router(simulator_router)
app.include_router(telemetry_router)

# Standalone Prometheus scrape endpoint (no correlation/metrics middleware wrap).
app.add_route("/metrics", metrics_endpoint)

# Prompt 9: OpenTelemetry distributed tracing across the service boundary.
# Traces flow to the OTLP collector (default http://otel-collector:4317).
init_telemetry("processguard-agent-service")
# instrument_fastapi_app also registers the FastAPI instrumentation that
# creates server spans tagged with route + trace_id for every request.
try:
    instrument_fastapi_app(app, "processguard-agent-service")
except Exception as exc:
    logger.warning("OTel FastAPI instrumentation skipped: %s", exc)


@app.get("/api/v1/agents/status")
async def agents_status() -> dict:
    """Agent roster; implemented agents are marked so the middleware can
    report readiness per agent. Unauthenticated status stub from Prompt 1."""
    return {
        "status": "ok",
        "agents": [
            {"name": "detection", "implemented": True},
            {"name": "knowledge", "implemented": True},
            {"name": "root_cause", "implemented": True},
            {"name": "risk", "implemented": True},
            {"name": "recommendation", "implemented": True},
            {"name": "orchestrator", "implemented": True},
            {"name": "action", "implemented": True},
        ],
    }
