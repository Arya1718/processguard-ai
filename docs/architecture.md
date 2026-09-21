# ProcessGuard AI — Architecture (Prompt 1 Scaffold)

ProcessGuard AI is a three-tier agentic system for industrial anomaly
investigation, built on top of Buckman's Ackumen platform concept. This
document describes the Prompt 1 scaffold: the secure, observable, deployable
shell that every agent plugs into from Prompt 2 onward. **No agent reasoning,
anomaly detection, or RAG logic exists yet — by design.**

## The three tiers

```
┌──────────────────┐        ┌──────────────────────┐        ┌──────────────────┐
│  Tier 1: React   │  HTTP  │  Tier 2: .NET 8      │  HTTP  │  Tier 3: Python  │
│  frontend (Vite) ├───────►│  middleware (API)    ├───────►│  agent-service   │
│  nginx, port 5173│        │  Kestrel, port 8080  │        │  FastAPI, :8000  │
└──────────────────┘        └──────────┬───────────┘        └────────┬─────────┘
                                       │                             │
                          ┌────────────┴────────────┐   ┌────────────┴──────────┐
                          │ Postgres (:5432)        │   │ Postgres  Redis       │
                          │ EF Core + migrations    │   │ probes    pub/sub bus │
                          │ (stand-in: Azure SQL)   │   │ (Azure SQL, Service   │
                          │                         │   │  Bus / Redis stand-   │
                          │ Redis (:6379)           │   │  ins)                 │
                          │ health/ready ping       │   └───────────────────────┘
                          └─────────────────────────┘
```

| Tier | Technology | Responsibility in this scaffold |
|------|------------|--------------------------------|
| Frontend | React + Vite, served by nginx | Login via dev-token, health status view |
| Middleware | .NET 8 Web API | Auth (JWT stub), EF Core data model, health aggregation, typed client to agent service |
| Agent service | Python + FastAPI (async) | Health, EventBus (Redis pub/sub), agent package skeletons |

### Local stand-ins for Azure services (same interface shape, swappable)

| Local | Stands in for | Swap point |
|-------|---------------|-----------|
| Postgres | Azure SQL | EF Core provider (middleware) / `app/core/db.py` (agent) |
| Redis | Azure Redis Cache | `RedisHealthCheck` / connection setup |
| Redis pub/sub | Azure Service Bus | `EventBus` interface — second implementation drops in with zero agent changes |
| JSON file store | Azure AI Search | `VectorStore` interface in `app/agents/knowledge/vector_store.py` (RAG itself is Prompt 2+) |

## Correlation ID path (traced end to end)

Every request is traceable across all tiers from day one. The ID is either
taken from the incoming `X-Correlation-Id` header or generated (GUID / uuid4).

```
Browser
  │  (fetch with no header on first hop)
  ▼
nginx (frontend) ──► middleware
                       │ CorrelationIdMiddleware:
                       │   1. read or generate ID
                       │   2. stash in HttpContext.Items
                       │   3. open logging scope  → every JSON log line gets "CorrelationId"
                       │   4. echo header on response
                       │
                       ├─► IAgentServiceClient (typed HttpClient)
                       │     adds header: X-Correlation-Id: <id>
                       ▼
                     agent-service
                       │ CorrelationIdMiddleware (FastAPI):
                       │   1. read or generate ID
                       │   2. set contextvar → every JSON log line gets "correlation_id"
                       │   3. echo header on response
                       │
                       ├─► Postgres probe (SELECT 1)
                       └─► Redis probe (PING) + EventBus ping topic
```

### What you see in the logs for ONE request

Middleware (structured JSON, scope carries CorrelationId + Service):

```json
{"Timestamp":"...","Level":"Information","CorrelationId":"9f1c...","Service":"processguard-middleware","Message":"Request started GET /api/v1/health/ready"}
```

Agent service (same correlation ID, from the forwarded header):

```json
{"timestamp":"...","level":"INFO","service":"processguard-agent-service","correlation_id":"9f1c...","message":"Request started GET /api/v1/health/ready"}
```

The frontend also displays the ID it received (`X-Correlation-Id` response
header), so a user-reported issue maps straight to log lines in both services.

## Request flow for the one end-to-end path that exists

`GET /api/v1/health/ready` (via the logged-in shell):

1. Browser calls `frontend` nginx → proxied to middleware `/api/v1/health/ready`.
2. Middleware auth middleware validates the JWT (all non-health routes require it;
   the health route itself is anonymous but the UI sends the token anyway).
3. Correlation ID assigned + logged; response echoes it.
4. Middleware executes `SELECT 1` against Postgres (EF Core) — genuine check.
5. Middleware opens Redis and issues `PING` — genuine check; failure ⇒ 503 with
   per-dependency status.
6. Response: `{"status":"ready","dependencies":{"postgres":"ok","redis":"ok"}}`.

### EventBus path (proven by the ping stub)

On startup the agent service subscribes `handle_ping` to topic `pgai.ping`,
then publishes one `{"msg":"bus-ok"}` event over Redis pub/sub; the subscriber
logs what it receives. `docker compose logs agent-service | grep ping` shows
the roundtrip. `EventBus` is an interface; a future `AzureServiceBusEventBus`
swaps in without touching agent code.

## Security model (scaffold-level)

- **JWT auth (dev stub):** `POST /api/v1/auth/dev-token` issues tokens only when
  `ASPNETCORE_ENVIRONMENT=Development`. Every other middleware route requires a
  valid bearer token. Placeholder for Azure AD / Entra ID (later prompt).
- **Fail-fast config:** all settings via `PGAI_*` env vars; a missing variable
  aborts startup with a clear message. In staging/prod the same variables are
  backed by Azure Key Vault via managed identity (see docs/environment.md).
- **Non-root containers:** all three images run as non-root users.
- **No hardcoded secrets:** compose defaults are local-dev placeholders;
  overrides come from the repo-root `.env` (git-ignored).

## Deliberately NOT in this prompt

Anomaly detection, RAG/knowledge retrieval logic, agent reasoning, ERP
integration, real Service Bus / AI Search wiring. Extension points are marked
with `PROMPT 2+` comments in the code.
