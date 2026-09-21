# Alerting Runbook

Prometheus alerting rules are in `config/alerting/rules/processguard.rules.yml`. AlertManager is configured in `config/alertmanager.yml`.

## Alert reference

| Alert | Severity | Condition | Runbook |
|-------|----------|-----------|---------|
| `MiddlewareDown` | critical | `up{job="processguard-middleware"} == 0` for 2m | See [Middleware down](#middleware-down) |
| `AgentServiceDown` | critical | `up{job="processguard-agent-service"} == 0` for 2m | See [Agent service down](#agent-service-down) |
| `RedisDown` | critical | `up{job="redis"} == 0` for 1m | See [Redis down](#redis-down) |
| `PostgresDown` | critical | `up{job="postgres"} == 0` for 1m | See [Postgres down](#postgres-down) |
| `OtelCollectorDown` | warning | `up{job="otel-collector"} == 0` for 2m | See [OTel collector down](#otel-collector-down) |
| `HighErrorRate` | warning | 5xx rate > 5% for 1m | See [High error rate](#high-error-rate) |
| `StreamConsumerLagHigh` | warning | `pgai_redis_stream_lag > 100` for 5m | See [Stream lag high](#stream-lag-high) |
| `IncidentsStuckInStatus` | warning | > 50 incidents in one status for 10m | See [Incidents stuck](#incidents-stuck-in-status) |
| `LowRootCauseSuccessRate` | warning | < 80% accepted (1h window) for 30m | See [Low root-cause success rate](#low-root-cause-success-rate) |
| `LowRecommendationSuccessRate` | warning | < 90% completed (1h window) for 30m | See [Low recommendation success rate](#low-recommendation-success-rate) |
| `TokenSpendSpike` | warning | cost rate > $0.50/min for 2m | See [Token spend spike](#token-spend-spike) |
| `AgentStageLatencyHigh` | warning | agent stage p95 > 30s for 5m | See [Agent stage latency high](#agent-stage-latency-high) |

---

## Middleware down
1. Check `docker compose ps` — is the container running?
2. Check logs: `docker compose logs middleware`
3. Common causes:
   - OIDC provider not healthy (middleware health depends on it in Prompt 7)
   - Postgres connection failure on startup
4. After fixing, the health check (`http://localhost:8080/api/v1/health/startup`) should return 200.

## Agent service down
1. Check `docker compose ps` — is the container running?
2. Check logs: `docker compose logs agent-service`
3. Common causes:
   - Database pool initialization failure (Postgres not ready)
   - Redis not reachable for EventBus
   - Missing GROQ_API_KEY when fake mode is off
4. The health check (`http://localhost:8001/api/v1/health/live`) should return 200.

## Redis down
1. `docker compose restart redis`
2. Check if the agent-service auto-reconnects to Redis (it should retry on the next publish/subscribe cycle).
3. Events published during downtime are lost (Redis pub/sub is fire-and-forget, matching the Azure Service Bus stand-in semantics).

## Postgres down
1. `docker compose restart postgres`
2. Wait for the health check (`pg_isready`) to pass.
3. The .NET middleware runs `db.Database.Migrate()` on startup — if Postgres was down during a restart, migrations may not have applied. Verify on restart.

## OTel collector down
1. The collector is a passive receiver — traces/metrics are buffered by the SDK and may be lost.
2. `docker compose restart otel-collector`
3. Not a data-loss risk for incidents; only affects observability data.

## High error rate
1. Check `/metrics` or Grafana dashboard for the erroring endpoints.
2. Correlate with trace IDs (the `X-Correlation-Id` header value is available in spans).
3. If errors are 500s, check the middleware and agent-service logs for exceptions.

## Stream lag high
1. Check if the Detection Agent is keeping up with the sensor-readings stream.
2. `redis-cli -h localhost XLEN sensor-readings` — check absolute lag.
3. Check Detection Agent logs for processing delays or backpressure.

## Incidents stuck in status
1. Check the incidents API: `GET /api/v1/incidents?status=investigating`
2. Verify the orchestrator is running and consuming `pgai.stage_completed` events.
3. Check if a downstream agent is failing (Root-Cause, Knowledge, Recommendation, or Action Agent).

## Token spend spike
1. Check the Grafana dashboard for the "LLM Cost Rate by Agent" panel.
2. Verify `PGAI_LLM__FAKEMODE` is set to `true` in development — real LLM calls cost money.
3. If running against real Groq, review the prompt sizes and retry attempts in the agent code.

## Agent stage latency high
1. Check the "Agent Stage Duration" panel in Grafana.
2. Identify which agent/stage is slow.
3. If the Root-Cause Agent is slow, check LLM response times.
4. If the Knowledge Agent is slow, check retrieval index performance.
