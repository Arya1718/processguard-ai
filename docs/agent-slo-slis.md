# Agent SLOs & SLIs

## Service-level objectives

### Incident investigation pipeline end-to-end
- **SLO**: Time from sensor anomaly detection to root-cause hypothesis recorded is at most 60 seconds for 95% of incidents.
- **SLI**: `pgai_agent_stage_duration_seconds` histogram, summed across stages (root_cause + knowledge).
- **Query**: `histogram_quantile(0.95, rate(sum(rate(pgai_agent_stage_duration_seconds_bucket[5m])) by (le)))`
- **Error budget**: 5% of incidents exceed 60s.

### Root-Cause Agent quality
- **SLO**: At least 80% of root-cause diagnoses are accepted (have cited evidence, confidence > 0).
- **SLI**: `pgai_agent_outcomes_total{agent="root_cause"}`
- **Query**: `rate(pgai_agent_outcomes_total{agent="root_cause",outcome="accepted"}[1h]) / rate(pgai_agent_outcomes_total{agent="root_cause"}[1h])`
- **Error budget**: < 80% accepted over a 1-hour window.

### Recommendation Agent completion
- **SLO**: At least 90% of recommendation generations complete without hitting max tool rounds.
- **SLI**: `pgai_agent_outcomes_total{agent="recommendation"}`
- **Query**: `rate(pgai_agent_outcomes_total{agent="recommendation",outcome="completed"}[1h]) / rate(pgai_agent_outcomes_total{agent="recommendation"}[1h])`
- **Error budget**: < 90% completed.

### API availability
- **SLO**: 99.9% uptime for all public API endpoints (middleware + agent-service).
- **SLI**: `up{job=~"processguard-middleware|processguard-agent-service"}`
- **Error budget**: 0.1% downtime (≈ 43 seconds per month).

### API latency
- **SLO**: 95th percentile request latency < 500ms for GET endpoints, < 2s for POST endpoints.
- **SLI**: `http_request_duration_seconds_bucket`
- **Query**: `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))`
- **Error budget**: 5% of requests exceed the target.

### LLM cost per incident
- **SLO**: Average LLM cost per incident ≤ $0.10.
- **SLI**: `pgai_incident_cost_usd_total` / total incident count.
- **Error budget**: average exceeds $0.10 over a 24-hour window.

### EventBus reliability
- **SLO**: 99% of events published are consumed within 10 seconds.
- **SLI**: `pgai_eventbus_consume_duration_seconds` histogram.
- **Query**: `histogram_quantile(0.99, rate(pgai_eventbus_consume_duration_seconds_bucket[5m]))`
- **Error budget**: 1% of events take longer than 10s to consume or fail.

## Alerting integration

These SLOs feed directly into the Prometheus alerting rules in `config/alerting/rules/processguard.rules.yml`:

| SLO | Alert | Threshold |
|-----|-------|-----------|
| Root-Cause quality | `LowRootCauseSuccessRate` | < 80% accepted, 30m window |
| Recommendation quality | `LowRecommendationSuccessRate` | < 90% completed, 30m window |
| API availability | `MiddlewareDown` / `AgentServiceDown` | service down 2m |
| Pipeline latency | `AgentStageLatencyHigh` | p95 > 30s, 5m window |
| LLM cost | `TokenSpendSpike` | > $0.50/min, 2m window |
| Stream reliability | `StreamConsumerLagHigh` | lag > 100 messages, 5m window |

## Measuring SLO burn-down

The error budget is consumed at the rate of `1 - (good_event_fraction)`. If the burn-down rate exceeds 100% of the budget in a 24-hour window, the on-call engineer should:

1. Check the Grafana dashboard for the offending component.
2. Review the alerting runbook for the specific alert.
3. If a regression, roll back the last deploy.
4. If an infrastructure issue, follow the runbook steps for the specific alert.
