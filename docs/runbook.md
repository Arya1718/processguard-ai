# ProcessGuard AI — Local Runbook

Everything needed to start, stop, rebuild, and verify the five-container
local stack. Prerequisites: Docker with Compose v2. No SDKs required on the
host — the .NET build happens inside its container.

## Start the full stack

```bash
cp .env.example .env      # first time only; defaults are fine for local dev
docker compose up --build # first build takes a few minutes
```

Wait for all five services to be healthy:

```bash
docker compose ps
# NAME               STATUS
# frontend           Up (healthy)
# middleware         Up (healthy)
# agent-service      Up (healthy)
# postgres           Up (healthy)
# redis              Up (healthy)
```

## Demo scenario: sensor simulator + Detection Agent

The simulator runs as its own container (`processguard-ai-simulator-1`) and
writes readings to the `sensor-readings` Redis Stream every few seconds. The
Detection Agent (inside agent-service) consumes the stream, scores readings,
and correlates multi-sensor drift into ONE incident.

```bash
# Normal mode: readings inside range, no incidents are created
curl http://localhost:8001/api/v1/simulator/state

# Trigger the reference cooling-tower incident (temperature climbs to ~38C,
# pH drops, flow drops, vibration rises, conductivity rises over ~45s)
curl -X POST http://localhost:8001/api/v1/simulator/trigger-scenario/cooling-tower-incident

# ~5s later, exactly one open incident exists (Site 12 / CP-04, 5 sensors in
# anomaly_summary, AnomalyDetected event on pgai.anomalies). Within ~20s the
# Knowledge Agent attaches SOP + historical evidence and the Root-Cause
# Agent records a cited hypothesis (status -> investigating):
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/dev-token \
  -H 'Content-Type: application/json' -d '{"userName":"demo"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents?status=open'
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents/{id}'                 # + evidence + rootCause
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents/{id}/evidence'        # SOP chunks + matched history
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/incidents/{id}/root-cause'      # hypothesis, confidence, citations
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8080/api/v1/sites/{siteId}/sensors/latest'

# Re-triggering while the incident is open is idempotent (updates, no duplicates,
# no second LLM diagnosis).

# Back to normal mode
curl -X POST http://localhost:8001/api/v1/simulator/reset
```

### LLM provider

The Root-Cause Agent runs with the deterministic fake client by default
(`PGAI_LLM__FAKEMODE=true`) so the demo works offline. For real Groq
inference set `GROQ_API_KEY` in `.env` and `PGAI_LLM__FAKEMODE=false`.
See `docs/llm-provider.md` for the swap path to Azure OpenAI.

Run the end-to-end proof (needs the stack up; repeatable):

```bash
cd agent-service && python -m pytest tests/test_incident_flow.py -m integration
```

## Full chain incl. execution (Prompt 5)

After the incident reaches `awaiting_approval`, approve it and watch the
Action Agent execute on the mock ERP/CMMS, then resolve the work order to
close the learning loop:

```bash
# Approve (identity comes from the JWT)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' \
  http://localhost:8080/api/v1/incidents/{id}/approve

# What the Action Agent executed (CMMS work-order reference, outcomes)
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8080/api/v1/incidents/{id}/action

# Resolve the work order on the CMMS (through the agent container -- the CMMS
# has no published ports; it is only reachable on its isolated network)
docker compose exec -T agent-service python -c "
import httpx
r = httpx.post('http://mock-erp-cmms:8100/work-orders/{woId}/resolve',
               json={'resolution_note': 'Strainer cleared, vibration normal'},
               headers={'X-Api-Key': 'cmms-dev-key-change-me'})
print(r.text)
"

# ~10s later (the Action Agent's resolution poll): incident -> resolved and
# a new HistoricalIncidents row exists (the learning loop)
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8080/api/v1/incidents/{id}/state-history
```

The mock CMMS is unreachable from the host, the frontend, and the
middleware -- by Docker network design, not by convention (see
docs/security-model.md).

## Stop / reset

```bash
docker compose down              # stop, keep data volumes
docker compose down -v           # stop and wipe Postgres/Redis data
```

## Rebuild after code changes

```bash
docker compose up --build middleware   # rebuild one service
docker compose build && docker compose up -d   # rebuild everything
```

## Verify health endpoints

Both backends expose identical health contracts:

```bash
# .NET middleware (port 8080)
curl -s http://localhost:8080/api/v1/health/live
curl -s http://localhost:8080/api/v1/health/ready
curl -s http://localhost:8080/api/v1/health/startup

# Python agent service (port 8001; 8000 is often taken by other stacks)
curl -s http://localhost:8001/api/v1/health/live
curl -s http://localhost:8001/api/v1/health/ready
curl -s http://localhost:8001/api/v1/health/startup
```

`ready` genuinely pings Postgres and Redis. To prove it fails when a
dependency is down:

```bash
docker compose stop redis
curl -s http://localhost:8080/api/v1/health/ready     # -> 503, redis: "failed"
docker compose start redis
```

## Verify the dev-token auth flow

```bash
# Protected route without a token -> 401
curl -i http://localhost:8080/api/v1/telemetry/agent-live | head -1

# Get a dev token (only works while ASPNETCORE_ENVIRONMENT=Development)
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/dev-token \
  -H 'Content-Type: application/json' -d '{"userName":"dev-user"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# Same route with the token -> 200 (also proves middleware -> agent call)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/telemetry/agent-live
# {"agentStatus":"ok"}
```

## Verify the correlation ID across services

```bash
# The response echoes the ID:
curl -si -H 'X-Correlation-Id: trace-abc-123' \
  http://localhost:8080/api/v1/health/ready | grep -i x-correlation-id
# X-Correlation-Id: trace-abc-123

# The same ID appears in BOTH services' logs:
docker compose logs middleware | grep trace-abc-123
docker compose logs agent-service | grep trace-abc-123
```

## Verify the EventBus ping stub

```bash
docker compose logs agent-service | grep -i ping
# ... "Subscribed handler to topic=pgai.ping"
# ... "Published to topic=pgai.ping"
# ... "EventBus ping received: {'msg': 'bus-ok', ...}"
```

Publish another test message directly through Redis:

```bash
docker compose exec redis redis-cli PUBLISH pgai.ping '{"topic":"pgai.ping","payload":{"msg":"manual"},"published_at":"now"}'
# see it logged by the subscriber in `docker compose logs -f agent-service`
```

## Run the agent-service tests

```bash
cd agent-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

(Or run them inside a built image: `docker compose run --rm agent-service pytest`.)

## Frontend

Open http://localhost:5173 — the shell is served by nginx, which proxies
`/api/v1/*` to the middleware. Log in (any user name) to get a dev token,
then the health panel polls `/api/v1/health/ready` and displays per-dependency
status plus the correlation ID.

## Common problems

| Symptom | Cause | Fix |
|---------|-------|-----|
| Middleware exits: `Missing required environment variable ...` | `.env` missing or incomplete | `cp .env.example .env` |
| `ready` returns 503 for postgres | Postgres not healthy yet | Wait for `docker compose ps` to show healthy, or restart middleware |
| 404 on `dev-token` | `ASPNETCORE_ENVIRONMENT` isn't Development | Only compose dev runs set this — check `docker compose config` |
| Port conflict on 5432/6379/8080 | Local Postgres/Redis running on host | Stop them or edit the port mappings in docker-compose.yml |
| Postgres/agent ports differ from defaults | Host ports 5432 and 8000 were already in use, so compose maps them to 5433 and 8001 | Internal service-to-service traffic is unaffected; only host access changes |
