# Ingestion Pipeline (Prompt 2)

How raw sensor telemetry becomes a correlated anomaly incident. Two distinct
Redis transports stand in for two distinct Azure services — they are **not
interchangeable**:

| Redis mechanism | Stands in for | Why this shape |
|-----------------|---------------|----------------|
| **Streams** (`sensor-readings` key, consumer group `detection-agent`) | **Azure Event Hub** | High-volume, append-only telemetry with persistent, replayable offsets and a consumer-group cursor. Fire-and-forget ingestion that survives consumer restarts. |
| **Pub/Sub** (`pgai.anomalies` topic, via the Prompt 1 `EventBus`) | **Azure Service Bus** | Discrete business events (AnomalyDetected) fanned out to N downstream agents. Delivery is best-effort today; Service Bus queues/topics add durability + competing consumers later. |

If we conflated them we would lose exactly the properties each Azure service
gives us: streams give us *replay and backpressure for telemetry*, pub/sub
gives us *fan-out for decisions*.

## Flow

```
 simulator (own container, producer)
   │  every 2-5s per sensor, ~2.5 readings/s total for the demo site
   │  XADD sensor-readings {sensor_id, equipment_id, sensor_type,
   │                        value, unit, timestamp}
   ▼
 Redis Stream "sensor-readings"            (Event Hub stand-in)
   │  consumer group: detection-agent (at-least-once, XACK after processing)
   ▼
 Detection Agent (in agent-service container)
   1. decode + registry lookup (thresholds from Sensors table)
   2. rolling baseline update  (Redis list baseline:{sensor_id},
      last 40 values, TTL 1h -> moving average + sigma)
   3. score: static band check + drift-vs-baseline check
   4. anomalous? -> append to in-memory correlation buffer
   5. correlate: anomalies on the same equipment within
      CORRELATION_WINDOW_SECONDS (chain-linked) = ONE incident
   6. persist (idempotent upsert into Incidents) then
      PUBLISH pgai.anomalies AnomalyDetected   (Service Bus stand-in)
   ▼
 downstream agents (Prompt 3+: root-cause, risk, ...)
```

## Sensor readings (stream event shape)

Stream `sensor-readings`, one entry per reading:

```json
{
  "sensor_id": "uuid",
  "equipment_id": "uuid",
  "sensor_type": "temperature",
  "value": "31.4200",
  "unit": "degC",
  "timestamp": "2026-09-18T14:00:00.000000+00:00"
}
```

## Scoring

A reading is anomalous when **either** check fires:

1. **Static band check** — value outside `[NormalMin, NormalMax]` by more than
   `PGAI_DETECTION__STATIC_Z` × band width. `0.05` = tolerate 5% overshoot
   past the band edge (sensor noise), flag beyond that.
2. **Baseline drift check** — `|value − baseline_mean| / baseline_sigma` above
   `PGAI_DETECTION__DRIFT_Z` (default 3σ). The baseline is the sensor's own
   last `PGAI_DETECTION__WINDOW_SIZE` (40) readings from Redis, so a sensor
   drifting within its band still gets caught; a permanently miscalibrated
   sensor settles into its new baseline and stops flapping.

Sigma floors at `1e-3 × max(1, |mean|)` so flat lines cannot divide by zero.

## Correlation window logic

Goal of the demo: **5 drifting sensors produce ONE incident, not five.**

* Every anomalous reading appends an observation to an in-memory buffer.
* Buffer entries for the same equipment are chain-linked into clusters:
  consecutive observations whose timestamps are ≤ `CORRELATION_WINDOW_SECONDS`
  (default 30 s) apart join the same cluster. Chain-linking matters: sensors
  rarely drift in perfect lockstep, so A@t0, B@t0+4s, C@t0+7s still group even
  though A–C alone would exceed a strict pairwise window.
* Only the freshest cluster per equipment is "active"; the cluster is dropped
  once its newest observation is older than `MAX_GAP_SECONDS` (default 90 s).
* Each active cluster maps to one incident row per equipment via the idempotent
  upsert below.

## Idempotency (one anomaly = one open incident)

When persisting, the agent looks for an existing `open`/`investigating`
incident **for the same equipment** (`SELECT ... FOR UPDATE` inside a
transaction to be safe under the at-least-once stream semantics):

* found  → `UPDATE` its `AnomalySummary` + `Severity` (and publish the event
  with the same `incident_id`)
* absent → `INSERT` a new open incident

So while an anomaly is ongoing, repeated detections refresh the evidence on
the same row; after `resolved`, a genuinely new anomaly opens a new incident.

## Persistence of evidence

Every scored reading is also written to the `SensorReadings` table
(`sensor_id, value, occurred_at`) as the durable proof-of-evidence backing
`GET /api/v1/incidents/{id}` — the stream is capped (`MAXLEN ~100k`) and is
not a system of record.

## Mode: normal vs scenario

The simulator emits noisy-but-in-range values in normal mode. The
cooling-tower scenario ramps CP-04's five sensors toward the reference values
(temperature 38 °C, pH 6.8, flow 106 m³/h, vibration 4.6 mm/s, conductivity
1280 µS/cm) with a smoothstep over `RAMP_SECONDS` (30 s), then holds until
reset — see `docs/anomaly-event-schema.md` for what the resulting
AnomalyDetected event looks like.
