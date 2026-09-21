# AnomalyDetected Event Schema (Prompt 2)

Published by the Detection Agent on the EventBus topic **`pgai.anomalies`**
(Redis pub/sub — the Service Bus stand-in) whenever a correlated anomaly is
persisted. Downstream agents (Prompt 3+: root-cause, risk, recommendation)
subscribe to this topic.

## Envelope

The `EventBus.publish(topic, payload)` envelope (added by the bus):

```json
{
  "topic": "pgai.anomalies",
  "payload": { "...the event below..." },
  "published_at": "2026-09-18T14:01:12.345678+00:00"
}
```

## Payload

```json
{
  "event": "AnomalyDetected",
  "event_id": "3f2b9c1e-...:2026-09-18T14:01:12.300000+00:00",
  "incident_id": "3f2b9c1e-8f4a-4c3d-9a1b-2e7d6c5b4a3f",
  "equipment_id": "9c1d2e3f-...",
  "severity": "medium",
  "detected_at": "2026-09-18T14:01:12.300000+00:00",
  "sensor_count": 5,
  "sensors": [
    {
      "sensor_id": "a1b2c3d4-...",
      "sensor_type": "temperature",
      "value": 34.8721,
      "unit": "degC"
    },
    {
      "sensor_id": "b2c3d4e5-...",
      "sensor_type": "flow_rate",
      "value": 112.4310,
      "unit": "m3/h"
    }
  ]
}
```

## Field reference

| Field | Type | Notes |
|-------|------|-------|
| `event` | string | Constant `"AnomalyDetected"`. |
| `event_id` | string | `"{incident_id}:{detected_at}"` — unique per publication; downstream consumers should treat duplicates of the same `event_id` as replays of one detection. |
| `incident_id` | string (uuid) | The `Incidents.Id` row the evidence lives on. Stable across re-detections of the same ongoing anomaly (idempotent upsert). |
| `equipment_id` | string (uuid) | Equipment the anomaly is attributed to. |
| `severity` | string | `low` / `medium` / `high`. Computed from how far outside the band (`margin / 0.5`) and how far off baseline (`z / 10`) the worst sensor is; ≥1.0 → high, ≥0.3 → medium, else low. |
| `detected_at` | string (ISO-8601 UTC) | When this detection pass ran. |
| `sensor_count` | int | Convenience equal to `len(sensors)`. |
| `sensors[]` | array | One entry per anomalous sensor in the correlated cluster. `value` is the latest reading; full per-reading evidence (timestamps, margins, sigma) is on the incident row's `AnomalySummary` jsonb — see below. |

## Incident row linkage

The `Incidents` row (Postgres, written before the event is published):

```jsonc
// AnomalySummary (jsonb)
{
  "equipment_id": "9c1d2e3f-...",
  "detected_at": "2026-09-18T14:01:12.300000+00:00",
  "sensor_count": 5,
  "sensors": [
    {
      "sensor_id": "a1b2c3d4-...",
      "sensor_type": "temperature",
      "value": 34.8721,
      "unit": "degC",
      "observed_at": "2026-09-18T14:01:09.512000+00:00",
      "reason": "outside normal band by 9.6%"
    }
  ]
}
```

`GET /api/v1/incidents/{id}` returns this row; the raw readings backing it are
in `SensorReadings` (persisted by the pipeline) and stream entries under
`sensor-readings` (bounded, non-authoritative).

## Future Azure mapping

| Component | Today | Later |
|-----------|-------|-------|
| Transport | Redis pub/sub topic `pgai.anomalies` | Azure Service Bus topic with one subscription per downstream agent |
| Delivery | at-most-once (best-effort) | durable, with dead-lettering per subscription |
| Schema | this document (informal contract) | JSON Schema validated at the producer, versioned (`schema_version` field arrives when the second consumer lands) |
