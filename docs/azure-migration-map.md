# Azure Migration Map (Prompt 11)

This document is the direct, credible answer to: **"is this really designed for
Azure, or did you just build something generic?"**

Every local stand-in used across Prompts 1–9 was deliberately built behind an
abstraction boundary. The table below lists **every** stand-in, the code/config
that changes (should be minimal to none), and what does **not** change at all.

The Bicep infrastructure definitions live in [`infra/`](../infra/). Deploy
with:

```bash
az deployment group create \
  --resource-group rg-processguard-prod \
  --template-file main.bicep \
  --parameters @main.parameters.json
```

---

## The mapping

| # | Local stand-in | Azure service | Bicep module | Code/config changes needed | What does NOT change |
|---|---|---|---|---|---|
| 1 | **Postgres** (`postgres:16` container) | Azure Database for PostgreSQL — Flexible Server | [`infra/modules/postgres.bicep`](../infra/modules/postgres.bicep) | Connection string (`PGAI_CONNECTIONSTRINGS__POSTGRES` / `PGAI_DATABASE__*` env vars → Azure connection string). EF Core provider name stays the same (`Npgsql`). | All SQL queries, schema, EF Core models, asyncpg pool logic. |
| 2 | **Redis** (`redis:7-alpine` container) | Azure Cache for Redis — Premium | [`infra/modules/redis.bicep`](../infra/modules/redis.bicep) | `redis_url` changes from `redis://redis:6379` to `rediss://<name>.redis.cache.windows.net:6384?ssl=true` + password from Key Vault. | RedisStream consumer group logic, pub/sub EventBus code. |
| 3 | **Redis Streams** (`sensor-readings` stream) | Azure Event Hubs + consumer groups | [`infra/modules/eventhub.bicep`](../infra/modules/eventhub.bicep) | `RedisEventBus` swaps to an `AzureEventHubEventBus` implementation of the same `EventBus` ABC. The Detection Agent's `XREADGROUP` logic becomes `EventHubConsumerClient.receive()`. Env: `PGAI_EVENTHUB__CONNECTION` + `PGAI_EVENTHUB__NAME` + `PGAI_EVENTHUB__CONSUMER_GROUP`. | Agent logic, scoring, correlation window, idempotency. |
| 4 | **Redis pub/sub** (EventBus) | Azure Service Bus (topics + subscriptions) | [`infra/modules/servicebus.bicep`](../infra/modules/servicebus.bicep) | `RedisEventBus` swaps to `AzureServiceBusEventBus` implementing the same `EventBus` interface. Same topics (`pgai.anomalies`, `pgai.stage_completed`, etc.) become SB topics. | All agent subscribe/publish calls (`bus.publish(...)`, `bus.subscribe(...)`). |
| 5 | **TF-IDF RetrievalIndex** (`scikit-learn`, in-memory, `app/rag/retrieval.py`) | Azure AI Search (vector + keyword) | [`infra/modules/aisearch.bicep`](../infra/modules/aisearch.bicep) | `RetrievalIndex` class swaps its backend: `index(docs)` and `search(query, top_k)` call Azure AI Search REST API instead of computing TF-IDF vectors locally. Env: `PGAI_AISEARCH__ENDPOINT` + `PGAI_AISEARCH__KEY`. | Citation metadata (`docId`, `title`, `section`, `source_type`, `source_url`) — the Knowledge Agent and Root-Cause Agent see the same metadata shape. |
| 6 | **Groq API** (`llm_client.py`, `GROQ_API_KEY`) | Azure OpenAI Service | [`infra/modules/cognitive.bicep`](../infra/modules/cognitive.bicep) | `get_llm_client()` adds an `AzureOpenAIClient(LLMClient)` branch selected by `PGAI_LLM__PROVIDER=azure-openai`. Same `generate(system_prompt, user_prompt, tools)` interface. Env: `PGAI_LLM__AZURE_DEPLOYMENT` + `PGAI_LLM__AZURE_ENDPOINT` + `AZURE_OPENAI_API_KEY` (from Key Vault). | System prompts, tool schemas, `LLMResponse` dataclass, citation enforcement, `FakeScriptedLLMClient` (still used for CI). |
| 7 | **Local OIDC provider** (`oidc-provider/`, `http://localhost:8090`) | Microsoft Entra ID | `infra/modules/` (no dedicated module — config-only) | `Authority` URL changes from `http://localhost:8090` to `https://login.microsoftonline.com/<tenant>/v2.0`. `JWKS_URL` changes from `http://oidc-provider:8090/jwks.json` to `https://login...azure.com/<tenant>/discovery/v2.0/keys`. Client ID, audience stay the same. Env: `PGAI_OIDC__AUTHORITY`, `PGAI_OIDC__JWKS_URL`. | `require_principal` decorator, `ensure_site`, `ensure_approval_level`, `RbacPolicies`. The JWT validation code is provider-agnostic (standard `JwtBearer` on .NET, `PyJWKClient` + `PyJWT` on Python). |
| 8 | **Docker Compose** (8 containers) | AKS + Helm charts | [`infra/modules/aks.bicep`](../infra/modules/aks.bicep) | Each container becomes a Helm `Deployment` + `Service`. Env vars injected from Key Vault via workload identity. Images built by CI and pushed to Azure Container Registry. | Container image contents, multi-stage Dockerfiles, non-root user setup. |
| 9 | **nginx** (frontend proxy) | Azure Front Door + Application Gateway | `infra/modules/apim.bicep` (APIM handles ingress) | Frontend SPA served from Azure Storage static website + CDN. `/api/v1/*` proxies to APIM → AKS middleware. OIDC redirect URI changes to the Front Door domain. | All frontend routing, AuthContext, OIDC PKCE client logic. |
| 10 | **OTel Collector** (`otel-collector:0.110.0` container) | Azure Monitor Collector | `infra/modules/monitoring.bicep` | Remove the OTel Collector container. Agents export OTLP directly to Azure Monitor (Application Insights). Env: `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317` → `https://<region>.monitor.azure.com/<instrumentation-key>/`. | `init_telemetry()`, metric definitions (`Counter`, `Histogram`, `Gauge`), span names (`eventbus.publish`, `eventbus.consume`, `knowledge_retrieve`, etc.). |
| 11 | **Prometheus** (`prom/prometheus` container) | Azure Monitor Metrics + Managed Prometheus | `infra/modules/monitoring.bicep` | `/metrics` endpoint stays (prometheus-net on .NET, prometheus-client on Python). Scrape config moves to Azure Monitor agent. Retention: `--storage.tsdb.retention.time=168h` → App Insights 90-day retention. | All metric names and labels (`http_requests_total`, `pgai_llm_calls_total`, etc.). |
| 12 | **Grafana** (`grafana:11.2.0` container) | Azure Managed Grafana | `infra/modules/monitoring.bicep` | Provision dashboards via Azure Managed Grafana API (same JSON model). Data source changes to Azure Monitor + Managed Prometheus. | Dashboard JSON structure, panel queries (PromQL → Azure Monitor query language is compatible). |
| 13 | **AlertManager** (`prom/alertmanager` container) | Azure Monitor Action Groups + Metric Alerts | `infra/modules/monitoring.bicep` | Prometheus alerting rules (`config/alerting/*.rules.yml`) become Azure Monitor metric alerts. Alertmanager routes → Action Groups (email + Slack webhook). | Alert logic (same thresholds, same conditions). |
| 14 | **mock-erp-cmms** (separate FastAPI app) | Buckman's real ERP/CMMS | `docs/erp-integration.md` | `PGAI_CMMS__BASE_URL` → real CMMS endpoint. `PGAI_CMMS__API_KEY` → OAuth2 client-credentials / mTLS. Only `app/core/cmms_client.py` changes. | `cmms_client.py` interface, `IncidentActions` table, `map_recommendation_to_cmms()` mapping, audit chain, idempotency logic. |
| 15 | **RBAC policy file** (`config/rbac-policy.json`) | Same file (mounted via Key Vault) | `infra/modules/keyvault.bicep` (same file deployed as a Key Vault secret or mounted) | No change — the JSON structure is identical. Deployed via CD pipeline as a Key Vault secret or mounted ConfigMap (AKS). | Both services load the same file; role → level mapping is unchanged. |
| 16 | **TEP real data** (`download_tep_data.py`, `data/tep/*.csv`) | Azure Blob Storage (via mounted volume) | `infra/modules/storage.bicep` | The TEP replay engine reads from a mounted Azure Files share instead of a host bind mount. Env: `PGAI_TEP__DATA_DIR=/mnt/tep-data`. | TEP replay logic, fault window detection, sensor mapping. |

## What does NOT change (at all)

These are the invariants — the parts of the system that are **already
cloud-native** in their design and require zero code changes to run on Azure:

1. **The agent logic** — all six agents (Detection, Knowledge, Root-Cause,
   Risk, Recommendation, Action, Orchestrator) are pure business logic. They
   call `EventBus.publish()`, `LLMClient.generate()`, and `get_pool()` — all
   of which are abstraction interfaces. The *orchestration* of agents over the
   EventBus, the citation enforcement in Root-Cause, the deterministic
   severity × confidence mapping in Risk, the typed action mapping in Action
   — none of this touches any Azure-specific code.

2. **The incident state machine** (`VALID_TRANSITIONS` in
   `app/agents/orchestrator/agent.py`) — this is a pure Python dict. It maps
   status → set of valid next statuses. Azure does not change state machine
   semantics.

3. **The HITL policy** (`config/hitl_thresholds.json`) — same file, same
   loading logic (`load_hitl_thresholds()`). Azure doesn't touch it.

4. **The RBAC model** (`config/rbac-policy.json`) — same file, loaded by both
   .NET and Python. Azure doesn't touch it.

5. **The correlation ID path** (`X-Correlation-Id`) — already end-to-end.
   Azure Monitor preserves correlation via trace context; the header stays.

6. **The provenance contract** — evidence citation structure, evidence
   validation, "uncited claims are rejected" — this is pure code.

7. **The CMMS write-access boundary** — `app/core/cmms_client.py` is the only
   module with an outbound external write. Azure doesn't change this invariant.

8. **The Docker images** — multi-stage, non-root. Azure runs the same images
   in AKS. No rebuild needed.

9. **The fail-fast config pattern** — `MiddlewareConfig.Load()` and
   `Settings.__init__()` both raise on missing env vars. Azure injects env
   vars from Key Vault via managed identity — same variables, different source.

10. **The test suite** — the same `pytest` unit tests, the same contract tests,
    the same k6 load tests. They assert behavior at the abstraction boundary,
    not at the Azure boundary.

## The single config change

If you read docs/environment.md, every `PGAI_*` variable has a "Key Vault name
(later)" column. The migration to Azure is:

1. **Provision** the Azure resources (Bicep templates above).
2. **Store secrets** in Key Vault under the names listed in
   `docs/environment.md`.
3. **Give the AKS cluster's managed identity** `Get` on those secrets.
4. **Set `PGAI_LLM__PROVIDER=azure-openai`** and deploy the
   `AzureOpenAIClient` implementation (one branch added to `get_llm_client()`).
5. **Set `EventBus` implementation** to `AzureServiceBusEventBus` (implement the
   same ABC, ~150 lines) and the Detection Agent's stream consumer to
   `EventHubConsumerClient` (the `RedisEventBus` → `AzureServiceBusEventBus`
   swap is ~20 lines since the interface is already defined).

That's it. The system is designed so the **abstraction boundary is the
deployment boundary** — you swap implementations, not logic.
