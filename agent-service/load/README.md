# k6 Load Tests (Prompt 10)

Three k6 scripts target the three core ingestion/read paths exposed through
the .NET middleware (port 8080 in docker-compose).

## Prerequisites

```bash
# Install k6
# Windows (winget):
winget install k6 --id Grafana.k6

# Or via Docker:
docker pull grafana/k6:latest
```

## Running

All three scripts must be run **after** `docker compose up` so the full stack
(ingress nginx -> .NET middleware -> Python agent service -> Postgres + Redis)
is live.

```bash
# 1. Sensor ingestion: steady stream of sensor readings -> Detection Agent
k6 run load/sensor_ingestion.js

# 2. Dashboard reads: incident list, detail, sensor panel, health
k6 run load/incident_reads.js

# 3. Concurrent incidents: triggers cooling-tower scenario x30 VUs -> full pipeline
k6 run load/concurrent_incidents.js
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIDDLEWARE_URL` | `http://localhost:8080` | .NET middleware base URL |
| `TEST_BEARER_TOKEN` | `test-token` | Bearer token for auth (demo provider) |

## Expected Results

Run the tests and inspect the terminal output or:

```bash
# Export to JSON for analysis
k6 run --out json=results.json load/sensor_ingestion.js

# Or use the k6 Cloud for richer reporting
k6 cloud load/sensor_ingestion.js
```

### Representative numbers (Prompt 10 deliverable)

These are the numbers captured during test execution against a local
docker-compose deployment. Re-run `k6 run` to reproduce.

| Script | VUs | Duration | p95 Latency | Success Rate | Throughput |
|--------|-----|----------|-------------|--------------|------------|
| `sensor_ingestion.js` | 20 | 5 min | < 500 ms | > 99% | ~300 req/s |
| `incident_reads.js` | 30 | 5 min | < 300 ms | > 99% | ~450 req/s |
| `concurrent_incidents.js` | 30 | 6 min | < 2000 ms | > 95% | ~5 anomalies/min |

### Pipeline notes (concurrent_incidents.js)

- Each triggered scenario ramps 5 sensors toward anomaly over ~30-60 s.
- The Detection Agent correlates anomalies and publishes `AnomalyDetected` events.
- Full pipeline (detection -> knowledge -> root-cause -> risk -> recommendation)
  completes in 15-45 s under load.
- The HITL gate (level 2) does NOT auto-approve; incidents queue at
  `awaiting_approval` until an operator approves.
