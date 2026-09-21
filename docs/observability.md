# Observability (Prompt 9)

ProcessGuard AI uses Prometheus metrics + Grafana dashboards + OpenTelemetry distributed tracing as a **stand-in for Azure Monitor / Application Insights**. The same concepts (metrics, traces, correlation by trace-id) map directly to the Azure services; the data plane is just open-source for local dev.

## Architecture

```
          Middleware (.NET 8)          Agent Service (Python/FastAPI)
          ┌─────────────────────┐        ┌──────────────────────────────────┐
          │ OTel SDK            │        │ OTel SDK                         │
          │  - ASP.NET instr.    │        │  - FastAPI instr.                │
          │  - HttpClient instr. │        │  - Requests instr.               │
          │  - HttpMetrics       │        │  - Prometheus counters           │
          │  - prometheus-net    │        │  - Agent/agent stage metrics     │
          │  /metrics endpoint   │        │  /metrics endpoint               │
          │                     │        │                                  │
          │  Trace context:      │        │  Trace context:                 │
          │  X-Correlation-Id    │        │  X-Correlation-Id               │
          └──────────┬───────────┘        └──────────┬───────────────────────┘
                     │                               │
                     ▼                               ▼
          ┌──────────────────────────────────────────────────────┐
          │           otel-collector (OTLP gRPC 4317)            │
          │  Receives traces from both services, forwards to       │
          │  debug exporter (console) and Prometheus via           │
          │  remote_write                                        │
          └──────────────┬───────────────────────────┬────────────┘
                         │                           │
                         ▼                           ▼
          ┌──────────────────────┐     ┌────────────────────────┐
          │    prometheus        │     │       grafana          │
          │  :9090               │     │  :3000                 │
          │  - Scrapes /metrics  │     │  - Dashboards          │
          │  - Rule evaluation   │     │  - Alerting UI         │
          │  - AlertManager →    │     │                        │
          └──────────────────────┘     └────────────────────────┘
```

## Correlation model

The existing `X-Correlation-Id` header (carried since Prompt 1) is the trace-context carrier. Both services log it, and the OTel instrumentation tags every span with it. The middleware proxies all calls to the agent-service (never direct), so every cross-service request is a single trace.

## Prometheus metrics

### HTTP metrics (middleware + agent-service)
| Metric | Labels | Description |
|--------|--------|-------------|
| `http_requests_total` | method, path, status_code | Total HTTP requests |
| `http_request_duration_seconds` | method, path | Request latency histogram |
| `http_request_duration_seconds_bucket` | method, path, le | Latency buckets |

### Agent-specific metrics (agent-service)
| Metric | Labels | Description |
|--------|--------|-------------|
| `pgai_llm_calls_total` | agent, provider, model, incident_id | LLM call count |
| `pgai_llm_tokens_total` | agent, provider, model, incident_id | Token consumption |
| `pgai_llm_cost_usd_total` | agent, provider, model, incident_id | Estimated cost (USD) |
| `pgai_agent_outcomes_total` | agent, outcome | accepted / needs_human_review / rejected |
| `pgai_root_cause_confidence` | — | Confidence score distribution (0-100) |
| `pgai_agent_stage_duration_seconds` | agent, stage | Pipeline stage latency |
| `pgai_hitl_decisions_total` | decision, hitl_level | Human approval decisions |
| `pgai_incident_cost_usd_total` | incident_id | Cumulative cost per incident |

### EventBus metrics (agent-service)
| Metric | Labels | Description |
|--------|--------|-------------|
| `pgai_eventbus_publish_total` | topic | Events published |
| `pgai_eventbus_consume_total` | topic | Events consumed |
| `pgai_eventbus_consume_duration_seconds` | topic | Handler latency |
| `pgai_eventbus_handler_errors_total` | topic | Handler errors |

### Redis metrics (agent-service)
| Metric | Labels | Description |
|--------|--------|-------------|
| `pgai_redis_stream_lag` | stream, group, consumer | Consumer group lag |

## OpenTelemetry traces

Traces are exported via OTLP gRPC to `otel-collector:4317`. The collector forwards to:
- **Debug exporter** (console output in local dev)
- **Prometheus remote_write** (for metric correlation if needed)

Key spans:
- `knowledge_retrieve` — SOP + history retrieval (Knowledge Agent)
- `root_cause_diagnose` — LLM diagnosis call (Root-Cause Agent)
- `recommendation_plan` — planner tool-calling loop (Recommendation Agent)
- `eventbus.publish` / `eventbus.consume` — event flow around the pipeline

## Endpoints

| Service | Path | Purpose |
|---------|------|---------|
| Middleware | `GET /metrics` | Prometheus scrape (prometheus-net + OTel) |
| Agent Service | `GET /metrics` | Prometheus scrape (prometheus-client) |
| Agent Service | `GET /api/v1/telemetry/summary` | Aggregate telemetry over time window |
| Agent Service | `GET /api/v1/incidents/{id}/cost` | Cost breakdown for one incident |
| OTLP Collector | `4317` (gRPC) | Trace ingestion |

## Local development

```bash
docker compose up -d
# Then:
open http://localhost:3000   # Grafana (admin/admin)
open http://localhost:9090   # Prometheus
open http://localhost:9093   # AlertManager
```

## Mapping to Azure

| Local stand-in | Azure equivalent |
|---------------|-----------------|
| prometheus | Azure Monitor Metrics |
| grafana | Azure Monitor workspaces (or Grafana managed) |
| otel-collector | Azure Monitor collector |
| alertmanager | Azure Monitor Action Groups |
| /metrics endpoints | Application Insights metrics |
