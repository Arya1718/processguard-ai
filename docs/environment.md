# ProcessGuard AI — Environment Variables

All configuration is supplied via environment variables — nothing is hardcoded
in source. Copy `.env.example` to `.env` for local development.

## Azure Key Vault strategy

In staging/prod this exact same set of variables is backed by **Azure Key
Vault via managed identity** instead of a `.env` file:

1. Secrets live in Key Vault under names mirroring the `PGAI_*` variables
   (table below).
2. Each container/App gets a managed identity with `Get` on those secrets.
3. Startup (Bicep/CLI or a small bootstrapper) injects them as plain
   environment variables — the applications only ever read `PGAI_*` from the
   process environment, so no application code changes when the source swaps.
4. `PGAI_JWT__SIGNINGKEY` disappears entirely when Entra ID lands (Prompt 2+).

Fail-fast behavior is intentional: if a variable is missing at startup both
services abort with `Missing required environment variable '<name>'` rather
than starting silently misconfigured.

## Shared

| Variable | Used for | Local default | Key Vault name (later) |
|----------|----------|---------------|------------------------|
| `PGAI_CORRELATION_HEADER` | Header name for request correlation | `X-Correlation-Id` | `PGAI--CORRELATION-HEADER` (App Setting, not secret) |
| `PGAI_SERVICE_NAME` | Agent-service name in logs | `processguard-agent-service` | (App Setting) |
| `PGAI_LOG_LEVEL` | Agent-service log level | `INFO` | (App Setting) |

## .NET middleware

| Variable | Used for | Local default | Key Vault name (later) |
|----------|----------|---------------|------------------------|
| `ASPNETCORE_ENVIRONMENT` | Enables dev-token endpoint when `Development` | `Development` (compose) | `Production` (App Setting) |
| `PGAI_JWT__SIGNINGKEY` | Signs dev JWTs (dev only; replaced by Entra ID) | `dev-only-signing-key-change-me-0123456789abcdef` | `PGAI--JWT--SIGNINGKEY` (removed once Entra ID lands) |
| `PGAI_JWT__ISSUER` | Expected JWT issuer | `processguard-dev` | `PGAI--JWT--ISSUER` |
| `PGAI_JWT__AUDIENCE` | Expected JWT audience | `processguard-api` | `PGAI--JWT--AUDIENCE` |
| `PGAI_JWT__ACCESSMINUTES` | Dev token lifetime | `480` | (App Setting) |
| `PGAI_CONNECTIONSTRINGS__POSTGRES` | EF Core / health checks (stand-in: Azure SQL) | `Host=postgres;Port=5432;...` | `PGAI--ConnectionStrings--Postgres` (Key Vault ref) |
| `PGAI_CONNECTIONSTRINGS__REDIS` | health/ready ping (stand-in: Azure Redis) | `redis:6379` | `PGAI--ConnectionStrings--Redis` (Key Vault ref) |
| `PGAI_AGENTSERVICE__BASEURL` | Typed client target for agent service | `http://agent-service:8000` | (App Setting) |

## Python agent service

| Variable | Used for | Local default | Key Vault name (later) |
|----------|----------|---------------|------------------------|
| `PGAI_DATABASE__HOST` | Postgres host (stand-in: Azure SQL) | `postgres` | (App Setting) |
| `PGAI_DATABASE__PORT` | Postgres port | `5432` | (App Setting) |
| `PGAI_DATABASE__NAME` | Database name | `processguard` | (App Setting) |
| `PGAI_DATABASE__USER` | Database user | `pgai` | `PGAI--DATABASE--USER` |
| `PGAI_DATABASE__PASSWORD` | Database password | `pgai_dev_password` | `PGAI--DATABASE--PASSWORD` (Key Vault ref) |
| `PGAI_REDIS__HOST` | Redis host (cache + pub/sub stand-in) | `redis` | (App Setting) |
| `PGAI_REDIS__PORT` | Redis port | `6379` | (App Setting) |
| `PGAI_REDIS__PASSWORD` | Redis password (empty locally) | *(empty)* | `PGAI--REDIS--PASSWORD` (Key Vault ref) |

## LLM (agent service, Prompt 3)

| Variable | Used for | Local default | Key Vault name (later) |
|----------|----------|---------------|------------------------|
| `GROQ_API_KEY` | Groq API key (stand-in for Azure OpenAI); required unless fake mode is on — the service fails fast without it | *(empty)* | `PGAI--OPENAI--APIKEY` (Key Vault ref via managed identity) |
| `LLM_MODEL` | Chat model id | `llama-3.3-70b-versatile` | (App Setting; becomes the Azure deployment name) |
| `PGAI_LLM__BASE_URL` | OpenAI-compatible chat-completions base URL | `https://api.groq.com/openai/v1` | (App Setting) |
| `PGAI_LLM__TIMEOUT_SECONDS` | LLM call timeout | `30` | (App Setting) |
| `PGAI_LLM__FAKEMODE` | Deterministic scripted client for offline dev/CI (never for staging/prod) | `true` locally | *(not set in staging/prod)* |
| `PGAI_LLM__FAKE_RESPONSES` | Optional scripted replies (JSON array, FIFO) for the fake client | *(unset)* | *(n/a)* |

## Mock ERP/CMMS (Prompt 5)

Stand-in for Buckman's real ERP/CMMS. The CMMS key is a SEPARATE credential
— the only one in the stack that can cause an external write, used
exclusively by the Action Agent (see docs/security-model.md).

| Variable | Used for | Local default | Key Vault name (later) |
|----------|----------|---------------|------------------------|
| `CMMS_API_KEY` | Key the mock CMMS itself validates on every request (`X-Api-Key`) | `cmms-dev-key-change-me` | `pgai--cmms--api-key` |
| `PGAI_CMMS__BASE_URL` | Base URL the agent-service uses to reach the CMMS (internal network only) | `http://mock-erp-cmms:8100` | (App Setting; becomes the real ERP/CMMS endpoint) |
| `PGAI_CMMS__API_KEY` | Key the agent-service presents (must equal `CMMS_API_KEY`) | `cmms-dev-key-change-me` | `pgai--cmms--api-key` (managed-identity scoped) |
| `PGAI_CMMS__TIMEOUT_SECONDS` | Per-request CMMS timeout | `10` | (App Setting) |
| `PGAI_CMMS__RETRY_ATTEMPTS` | Bounded retry cap (4xx never retried) | `4` | (App Setting) |
| `PGAI_CMMS__RETRY_BACKOFF_SECONDS` | Linear backoff base between attempts | `2` | (App Setting) |

## Frontend (Vite)

Only variables prefixed `VITE_` are exposed to browser code. **Never put real
secrets here** — the frontend is public surface.

| Variable | Used for | Local default | Key Vault name (later) |
|----------|----------|---------------|------------------------|
| `VITE_API_BASE_URL` | Middleware base URL the browser calls directly | `http://localhost:8080/api/v1` | (App Setting) |

> Note: in the compose stack the browser actually talks to nginx
> (http://localhost:5173), which proxies `/api/v1/*` to the middleware — so
> the default already works without CORS setup. Change `VITE_API_BASE_URL`
> only if you point the shell at a deployed middleware.

## Adding a new variable

1. Add it to `.env.example` with a safe local default.
2. Load it in `middleware/src/Infrastructure/MiddlewareConfig.cs` or
   `agent-service/app/core/config.py` — required variables must fail fast.
3. Add a row here with its future Key Vault name.
