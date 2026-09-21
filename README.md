# ProcessGuard AI

**Agentic anomaly investigation and resolution for industrial process
operations**, built on Buckman's Ackumen platform concept. A three-tier
system — React frontend, .NET 8 middleware, Python FastAPI agent service —
where specialist multi-agent AI (detection, root-cause, knowledge, risk,
recommendation, action) collaborates over an event bus to investigate plant
anomalies end-to-end: from sensor anomaly to a cited root-cause hypothesis,
to a deterministic HITL-gated recommendation, to an approved CMMS work-order
execution, with the resolution fed back as learned historical knowledge.

```
   ┌──────────────────┐  HTTP   ┌────────────────────┐  HTTP   ┌──────────────────┐
   │  Tier 1: React   │  ────►  │  Tier 2: .NET 8     │  ────►  │  Tier 3: Python   │
   │  frontend (Vite) │         │  middleware (API)   │         │  FastAPI agents   │
   │  nginx, :5173    │         │  Kestrel, :8080      │         │  :8000            │
   └──────────────────┘         │                      │         │                    │
                                │  ┌─────────────────┐ │         │  ┌──────────────┐ │
                                │  │ EF Core (SQL)   │ │         │  │  EventBus    │ │
                                │  │                 │ │         │  │  pub/sub     │ │
                                │  └─────────────────┘ │         │  └──────────────┘ │
                                │                      │         │        │           │
                                │  ┌────────┐  ┌─────┐ │         │   ┌────────┐  ┌───┴──┐ │
                                │  │ OIDC   │  │RBAC │ │         │  │Groq/ O │  │Redis │ │
                                │  │(Entra) │  │pol. │ │         │  │AI Open │s│(EH/ │ │
                                │  └────────┘  └─────┘ │         │  │AI stby)│ ││SB)  │ │
                                │        │            │         │  └────────┘  └──────┘ │
                                │  ┌─────┴────┐      │         │        │            │
                                │  │ EF Health│      │         │  ┌─────┴────┐       │
                                │  │ checks   │      │         │  │  RAG     │       │
                                │  └──────────┘      │         │  │  TF-IDF  │       │
                                └────────────────────┘         │  │  (AI Srh)│       │
                                                               │  └─────┬────┘       │
                                          ┌────────────────────┐        │            │
                                          │  Postgres (SQL DB)  │        │            │
                                          │  + pgvector        │◄───────┘            │
                                          └──────────────────┘                       │
                                                                                     │
                                          ┌─────────────────────┐                     │
                                          │  mock-erp-cmms      │◄────────────────────┘
                                          │  (Buckman CMMS)     │
                                          └─────────────────────┘
```

> **Buckman/Ackumen positioning:** ProcessGuard AI is the prototype for an
> automated reasoning layer that would sit atop Ackumen's live sensor
> telemetry. In a real deployment, Ackumen's real sensors feed the ingestion
> pipeline, and approved actions execute against Buckman's real ERP/CMMS.

## Quickstart

```bash
cp .env.example .env
docker compose up --build
```

All eight containers come up with no manual steps. Then open
http://localhost:5173 and log in with any demo account.

### Trigger the reference scenario (programmatic access)

For API testing, obtain a token via the OIDC provider's password-grant flow:

```bash
# 1. Get an access token for maintenance@site12.demo (password: maintenance-pass)
TOKEN=$(curl -s -X POST http://localhost:8090/token \
  -d 'grant_type=password&client_id=processguard-frontend' \
  -d 'username=maintenance@site12.demo&password=maintenance-pass' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# 2. Trigger the cooling-tower incident (5 sensors on CP-04 drift over ~30s)
curl -X POST http://localhost:8001/api/v1/simulator/trigger-scenario/cooling-tower-incident

# Wait ~60s, then check the investigation pipeline:
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents?status=open'
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents/{id}'
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents/{id}/root-cause'
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents/{id}/state-history'

# 3. Approve (only MaintenanceEngineer or PlantManager can approve Level 2+)
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' \
  http://localhost:8080/api/v1/incidents/{id}/approve

# 4. Resolve the work order on the mock CMMS (through the agent container — the
# CMMS has no published ports, network-isolated)
docker compose exec -T agent-service python -c "
import httpx; r = httpx.post('http://mock-erp-cmms:8100/work-orders/{woId}/resolve',
  json={'resolution_note':'Strainer cleared, vibration normal'}, headers={'X-Api-Key':'cmms-dev-key-change-me'}); print(r.text)"

# 5. Back to normal mode
curl -X POST http://localhost:8001/api/v1/simulator/reset
```

## Service URLs (local)

| Service | URL | Purpose |
|---|---|---|
| Frontend | http://localhost:5173 | React SPA, login, dashboard |
| Middleware | http://localhost:8080 | .NET 8 API gateway, auth, data |
| Agent service | http://localhost:8001 | Agents, EventBus, incidents API |
| Simulator | (container only) | Emits sensor readings |
| Mock ERP/CMMS | (isolated network) | Stand-in for Buckman's real ERP/CMMS |
| Postgres | localhost:5433 | Stand-in for Azure SQL |
| Redis | localhost:6379 | Stand-in for Azure Redis / Event Hub / Service Bus |
| Grafana | http://localhost:3000 (admin/admin) | Dashboards |
| Prometheus | http://localhost:9090 | Metrics |
| AlertManager | http://localhost:9093 | Alert routing |
| OTel Collector | http://localhost:4317 | Trace/metrics collection |

## What's built 

| Sno | Deliverable | Status |
|---|---|---|
| 1 | Scaffold: three-tier stack, health endpoints, correlation IDs, fail-fast config | ✅ |
| 2 | Sensor simulator + Detection Agent: correlated multi-sensor anomaly detection, Redis Stream ingestion | ✅ |
| 3 | Knowledge/RAG Agent + Root-Cause Agent: TF-IDF retrieval, citation-checked LLM hypothesis | ✅ |
| 4 | Risk Agent (deterministic), Recommendation Agent (LLM + tool calling), Orchestrator (state machine), 3-level HITL | ✅ |
| 5 | Action Agent + mock-erp-cmms: typed CMMS execution, outcome tracking, learning loop | ✅ |
| 6 | React dashboard: alert feed, investigation timeline, "Why?" chat, evidence panels | ✅ |
| 7 | Real OIDC auth: JWT validation, 3 roles, site-scoping, cross-site isolation | ✅ |
| 8 | Real data integration: TEP (Tennessee Eastman Process) replay, source-type provenance | ✅ |
| 9 | Observability: Prometheus/Grafana metrics, OpenTelemetry traces, alerting, cost tracking | ✅ |
| 10 | Testing/QA: unit/integration/contract/regression/load layers, 75% coverage, contract tests, k6 load tests, LLM regression set | ✅ |
| 11 | CI/CD (GitHub Actions), CD (staging auto + gated production), Azure Bicep IaC, data governance, Azure migration doc | ✅ |

## Running tests

```bash
# Python agent service — unit + contract + regression (no live stack needed)
cd agent-service
PGAI_OTEL__DISABLE=true python -m pytest tests/unit/ tests/contract/ tests/regression/ -q -m "not integration and not data"

# Integration tests (requires `docker compose up -d` first)
python -m pytest tests/integration/ -v

# Run a single test category
python -m pytest tests/unit/ -q --cov=app --cov-report=term-missing

# k6 load tests (requires `docker compose up -d` + k6 installed)
cd agent-service
k6 run load/sensor_ingestion.js
k6 run load/incident_reads.js
k6 run load/concurrent_incidents.js

# .NET middleware tests (requires .NET SDK 8.0)
cd middleware/tests
dotnet test --verbosity minimal
```

## CI/CD

- **CI**: `.github/workflows/ci-{frontend,middleware,agent}.yml` — lint, test,
  coverage (75% gate), security scan (npm audit / dotnet-vulnerable / pip-audit / Trivy)
- **CD**: `.github/workflows/cd-staging.yml` (auto-deploy on merge) +
  `cd-production.yml` (manual approval gate)
- **IaC**: `infra/main.bicep` — Azure Bicep for all production resources
- See [`.github/workflows/README.md`](.github/workflows/README.md) (CI/CD overview),
  [`docs/cd-rollback.md`](docs/cd-rollback.md) (rollback procedure)

## Documentation index

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Three-tier request flow, correlation-ID path |
| [docs/runbook.md](docs/runbook.md) | Start/stop/rebuild + demo scenario |
| [docs/environment.md](docs/environment.md) | Every env var and its future Key Vault name |
| [docs/ingestion-pipeline.md](docs/ingestion-pipeline.md) | Event Hub stand-in vs Service Bus stand-in |
| [docs/anomaly-event-schema.md](docs/anomaly-event-schema.md) | The AnomalyDetected event shape |
| [docs/failure-modes.md](docs/failure-modes.md) | Dropped readings, write failures, duplicates, restarts |
| [docs/rag-corpus.md](docs/rag-corpus.md) | Knowledge base contents, indexing, how to add docs |
| [docs/diagnosis-provenance.md](docs/diagnosis-provenance.md) | How root-cause claims trace to cited evidence |
| [docs/llm-provider.md](docs/llm-provider.md) | The llm_client interface and the Azure OpenAI swap |
| [docs/agent-components.md](docs/agent-components.md) | The six-agent picture and the orchestrator's event flow |
| [docs/hitl-thresholds.md](docs/hitl-thresholds.md) | Severity × confidence → HITL level table + safety-floor rationale |
| [docs/state-machine.md](docs/state-machine.md) | The full incident status state machine, diagrammed |
| [docs/erp-integration.md](docs/erp-integration.md) | Mock CMMS, typed action mapping, what changes for real ERP/CMMS |
| [docs/security-model.md](docs/security-model.md) | Write-access boundary, trust architecture |
| [docs/auth-flow.md](docs/auth-flow.md) | OIDC auth flow, claims, Entra ID migration |
| [docs/rbac-policy.md](docs/rbac-policy.md) | Role → capability table, enforcement layers |
| [docs/real-data-sources.md](docs/real-data-sources.md) | TEP real process data vs. authored content |
| [docs/observability.md](docs/observability.md) | Prometheus/Grafana/OTel, metric definitions |
| [docs/agent-slo-sis.md](docs/agent-slo-sis.md) | Service-level objectives and error budgets |
| [docs/alerting-runbook.md](docs/alerting-runbook.md) | Alert definitions and response procedures |
| [docs/frontend-architecture.md](docs/frontend-architecture.md) | React component tree, state management, polling, "Why?" grounding |
| [docs/data-governance.md](docs/data-governance.md) | Backup, retention, deletion, RPO/RTO |
| [docs/data-governance.md](docs/data-governance.md) | Backup, retention, deletion, RPO/RTO |
| [docs/azure-migration-map.md](docs/azure-migration-map.md) | Stand-in → Azure service mapping table |
| [.github/workflows/README.md](.github/workflows/README.md) | CI/CD pipeline documentation |
| [docs/ci-cd.md](docs/ci-cd.md) | CI/CD pipeline documentation |
| [docs/cd-rollback.md](docs/cd-rollback.md) | Rollback procedure and test record |

## What's real vs. simulated

This project is honest about what is real data, real industrial content, and
what is authored for this case study. There are no hidden stand-ins — every
one is documented and labeled in-band.

### Real components

| Component | Real data source | Where it lives |
|---|---|---|
| **TEP sensor values** (temperature, flow rate) | Rieth et al. 2016, Harvard Dataverse (DOI: 10.7910/DVN/6C3JR1) | `data/tep/*.csv`, streamed by `app/simulator/tep_replay.py` — `source_type: public_real` |
| **Cooling-tower SOP** | U.S. DOE FEMP BMP #10 | `app/rag/knowledge_base/PUB-DOE-FEMP-COOLING-TOWER.md` — `source_type: public_real` |
| **Pump sourcebook** | U.S. DOE Industrial Technologies (with AEE/HI/NASA) | `app/rag/knowledge_base/PUB-DOE-PUMP-SOURCEBOOK.md` — `source_type: public_real` |
| **CSB MFG Chemical incident** | U.S. Chemical Safety Board report 2004-9-I-GA | Seeded in `middleware/src/Data/DemoSeeder.cs` — `source_type: public_real` |

### Simulated / authored components

| Component | What it stands in for | Where it lives |
|---|---|---|
| **pH / conductivity / vibration sensors** | Correlated synthetic overlay on real TEP fault axis | `app/simulator/tep_replay.py::_Overlay` — `source_type: illustrative` |
| **Authored SOPs** (SOP-COOL-014, etc.) | Modeled on real industrial patterns but written for this project | `app/rag/knowledge_base/SOP-*.md` — `source_type: illustrative` |
| **Demo historical incidents** (HIST-2026-0141, etc.) | Authored scenario rows | `middleware/src/Data/DemoSeeder.cs` — `source_type: illustrative` |
| **OIDC identity provider** | Protocol-compatible stand-in for Microsoft Entra ID | `oidc-provider/` — same OIDC/OAuth2 wire protocol |
| **mock-erp-cmms** | Stand-in for Buckman's real ERP/CMMS | `mock-erp-cmms/` — separate FastAPI app on isolated Docker network |
| **Postgres / Redis / Prometheus / Grafana / OTel Collector** | Stand-ins for Azure SQL, Azure Redis Cache, Azure Monitor, Azure Managed Grafana | `docker-compose.yml` containers |
| **Groq API** | Stand-in for Azure OpenAI | `app/core/llm_client.py` — OpenAI-compatible API |

See [`docs/real-data-sources.md`](docs/real-data-sources.md) for the full
provenance accounting and the CI tradeoff that keeps the distinction
verifiable (not implied).

## Repository layout

```
processguard-ai/
├── frontend/                    # React SPA (Vite) + Playwright e2e tests
├── middleware/                  # .NET 8 Web API — auth, data, typed agent client
│   ├── src/                     # Controllers, Auth, Health, Data (EF Core + migrations)
│   └── tests/                   # XUnit unit tests (Auth, AgentServiceClient, Config)
├── agent-service/               # Python FastAPI — the six agents
│   ├── app/
│   │   ├── agents/              # detection/, knowledge/, root_cause/, risk/,
│   │   │                       # recommendation/, action/, orchestrator/
│   │   ├── api/                 # FastAPI routers (incidents, evidence, ask, health, telemetry)
│   │   ├── core/                # config, security, db, llm_client, observability
│   │   ├── eventbus/            # EventBus ABC + Redis implementation
│   │   ├── rag/                 # TF-IDF RetrievalIndex + knowledge base
│   │   └── simulator/           # Synthetic + TEP replay sensor generators
│   ├── load/                    # k6 load test scripts + README
│   └── tests/                   # unit/, integration/, contract/, regression/
├── mock-erp-cmms/               # Mock ERP/CMMS FastAPI app (isolated network)
├── oidc-provider/               # Local OIDC provider (protocol-compatible stand-in)
├── infra/                       # Azure Bicep IaC (all production resources)
├── config/                      # Prometheus, Grafana, AlertManager, RBAC, OTel config
├── docs/                        # All documentation (index above)
├── scripts/                     # TEP data download, public doc preparation
├── .github/workflows/           # CI/CD: 3 CI workflows + staging + production
├── docker-compose.yml           # Base local stack (8 containers)
├── docker-compose.staging.yml   # Staging overrides (CI/CD)
├── docker-compose.prod.yml      # Production overrides (CI/CD)
├── .env.example                 # Local dev defaults (no real secrets)
└── AGENTS.md                    # Common commands for this repo
```
