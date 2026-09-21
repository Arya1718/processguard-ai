"""Detection Agent (Prompt 2) -- the first real agent.

Consumes raw sensor readings from the Redis Stream "sensor-readings" (the
stand-in for Azure Event Hub), scores them with app.agents.detection.scoring,
correlates multi-sensor anomalies per equipment, writes incidents to Postgres
and publishes AnomalyDetected events on the EventBus (the pub/sub stand-in
for Azure Service Bus -- NOT the same transport as the stream; see
docs/ingestion-pipeline.md).

Idempotency: while an incident for the same equipment is still open, the
anomaly updates that incident's anomaly_summary instead of creating a new
row (one underlying anomaly == one open incident).

Dead-letter path: when persistence or publishing fails after retries, the
full anomaly context is logged to the "pgai-dlq" log channel rather than
being silently dropped.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import asyncpg

from app.agents.detection.scoring import (
    AnomalyObservation,
    BaselineStats,
    Reading,
    SensorThresholds,
    compute_baseline,
    correlate_anomalies,
    score_reading,
    severity_from_score,
)
from app.core.config import get_settings
from app.core.db import get_pool
from app.core.logging_config import get_logger
from app.eventbus.base import EventBus, utcnow_iso

logger = get_logger(__name__)

STREAM_NAME = "sensor-readings"
STREAM_CONSUMER_GROUP = "detection-agent"
BASELINE_TTL_SECONDS = 3600


class DetectionAgent:
    def __init__(self, redis_client, pool: asyncpg.Pool, bus: EventBus) -> None:
        self._redis = redis_client
        self._pool = pool
        self._bus = bus
        settings = get_settings()
        self._static_z = settings.detection_static_z
        self._drift_z = settings.detection_drift_z
        self._window_size = settings.detection_window_size
        self._min_samples = settings.detection_min_samples
        self._correlation_window = settings.detection_correlation_window_seconds
        self._max_gap = settings.detection_max_gap_seconds
        self._retry_attempts = settings.detection_db_retry_attempts
        self._correlation_buffer: list[AnomalyObservation] = []
        self._thresholds: dict[str, SensorThresholds] = {}
        self._sensor_meta: dict[str, dict] = {}
        self._running = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self._load_sensor_registry()
        await self._ensure_consumer_group()
        self._running = True
        logger.info(
            "Detection agent started (stream=%s group=%s window=%ss)",
            STREAM_NAME, STREAM_CONSUMER_GROUP, self._correlation_window,
        )

    async def stop(self) -> None:
        self._running = False

    # -- stream plumbing ----------------------------------------------------

    async def _ensure_consumer_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM_NAME, STREAM_CONSUMER_GROUP, id="0", mkstream=True)
            logger.info("Created consumer group %s on %s", STREAM_CONSUMER_GROUP, STREAM_NAME)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                logger.info("Consumer group %s already exists", STREAM_CONSUMER_GROUP)
            else:
                raise

    async def _load_sensor_registry(self) -> None:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT "Id", "EquipmentId", "SensorType", "Unit", "NormalMin", "NormalMax" FROM "Sensors"'
            )
        self._thresholds = {
            str(r["Id"]): SensorThresholds(normal_min=float(r["NormalMin"]), normal_max=float(r["NormalMax"]))
            for r in rows
        }
        self._sensor_meta = {
            str(r["Id"]): {
                "equipment_id": str(r["EquipmentId"]),
                "sensor_type": r["SensorType"],
                "unit": r["Unit"],
                # Normal band stored with the meta so every anomaly-summary
                # entry can carry "value vs normal range" directly -- the
                # Detection panel (and any later consumer) renders it without
                # a second lookup.
                "normal_min": float(r["NormalMin"]),
                "normal_max": float(r["NormalMax"]),
            }
            for r in rows
        }
        logger.info("Loaded sensor registry: %d sensors", len(self._sensor_meta))

    async def consume_forever(self) -> None:
        """Read new entries from the stream as a group consumer (at-least-once)."""
        last_id = ">"  # only new, never-delivered entries
        while self._running:
            try:
                entries = await self._redis.xreadgroup(
                    STREAM_CONSUMER_GROUP, "consumer-1", {STREAM_NAME: last_id}, count=50, block=1000
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Stream read failed: %s", exc)
                await asyncio.sleep(1)
                continue
            for _stream, messages in entries or []:
                for message_id, fields in messages:
                    try:
                        await self._process_reading(message_id, fields)
                    except Exception as exc:
                        logger.error("Failed processing %s: %s", message_id, exc)
                    finally:
                        try:
                            await self._redis.xack(STREAM_NAME, STREAM_CONSUMER_GROUP, message_id)
                        except Exception as exc:
                            logger.error("Failed acking %s: %s", message_id, exc)

    # -- scoring ------------------------------------------------------------

    async def _process_reading(self, message_id, fields) -> None:
        data = self._decode(fields)
        if data is None:
            return

        sensor_id = data["sensor_id"]
        thresholds = self._thresholds.get(sensor_id)
        meta = self._sensor_meta.get(sensor_id)
        if thresholds is None or meta is None:
            logger.warning("Unknown sensor %s; dropping reading", sensor_id)
            return

        occurred_at = _parse_ts(data.get("timestamp"))
        reading = Reading(
            sensor_id=sensor_id,
            equipment_id=meta["equipment_id"],
            sensor_type=meta["sensor_type"],
            value=float(data["value"]),
            unit=data.get("unit", meta["unit"]),
            occurred_at=occurred_at,
        )

        baseline = await self._update_baseline(sensor_id, reading.value)

        # Durable proof-of-evidence for every reading (backing
        # GET /api/v1/incidents/{id} and the latest-readings endpoint).
        await self._persist_reading(reading)

        scored = score_reading(reading, thresholds, baseline, self._static_z, self._drift_z)
        if scored.anomalous:
            await self._register_anomaly(scored, meta)

    async def _persist_reading(self, reading: Reading) -> None:
        """Best-effort evidence write: a failed reading insert must never
        block the pipeline (the incident path has its own retries)."""
        try:
            await self._pool.execute(
                'INSERT INTO "SensorReadings" ("Id", "SensorId", "Value", "OccurredAt", "CreatedAt") '
                "VALUES ($1, $2, $3, $4, $4)",
                __import__("uuid").uuid4(),
                __import__("uuid").UUID(reading.sensor_id),
                reading.value,
                reading.occurred_at,
            )
        except Exception as exc:
            logger.warning("SensorReadings insert failed for %s: %s", reading.sensor_id, exc)

    def _decode(self, fields) -> dict | None:
        try:
            return {
                "sensor_id": fields["sensor_id"],
                "equipment_id": fields["equipment_id"],
                "sensor_type": fields["sensor_type"],
                "value": float(fields["value"]),
                "unit": fields["unit"],
                "timestamp": fields["timestamp"],
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("Dropping malformed stream entry %s: %s", fields, exc)
            return None

    async def _update_baseline(self, sensor_id: str, value: float) -> BaselineStats:
        key = f"baseline:{sensor_id}"
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.lpush(key, value)
            pipe.ltrim(key, 0, self._window_size - 1)
            pipe.lrange(key, 0, self._window_size - 1)
            pipe.expire(key, BASELINE_TTL_SECONDS)
            results = await pipe.execute()
        values = [float(v) for v in (results[2] or [])]
        return compute_baseline(values)

    # -- correlation + persistence ------------------------------------------

    async def _register_anomaly(self, scored, meta: dict) -> None:
        obs = AnomalyObservation(
            equipment_id=meta["equipment_id"],
            sensor_id=scored.reading.sensor_id,
            sensor_type=meta["sensor_type"],
            observed_at=scored.reading.occurred_at,
            value=scored.reading.value,
            unit=meta["unit"],
            reason=scored.reason,
            static_margin=scored.static_margin,
            drift_z=scored.drift_z,
        )
        self._correlation_buffer.append(obs)
        self._prune_buffer()

        correlated = correlate_anomalies(
            self._correlation_buffer, self._correlation_window, self._max_gap
        )
        for anomaly in correlated:
            if anomaly.equipment_id == meta["equipment_id"]:
                await self._persist_incident(anomaly)

    def _prune_buffer(self) -> None:
        cutoff = (datetime.now(timezone.utc)).timestamp() - (self._max_gap * 3)
        self._correlation_buffer = [
            o for o in self._correlation_buffer if o.observed_at.timestamp() > cutoff
        ]

    async def _persist_incident(self, anomaly) -> None:
        # One observation per sensor: keep only the freshest of the ongoing
        # drift so a sustained anomaly stays a single incident with a compact
        # summary (5 sensors -> 5 entries), not an ever-growing list.
        freshest: dict[str, AnomalyObservation] = {}
        for o in anomaly.sensors:
            if o.sensor_id not in freshest or o.observed_at > freshest[o.sensor_id].observed_at:
                freshest[o.sensor_id] = o
        sensors_payload = [_sensor_payload(o, self._sensor_meta) for o in freshest.values()]
        # Numeric scores straight off the observations -- never parse reason text.
        margins = [o.static_margin for o in freshest.values()]
        drifts = [o.drift_z for o in freshest.values()]
        severity = severity_from_score(margins, drifts)

        summary = _correlated_summary(anomaly.equipment_id, sensors_payload)

        try:
            incident_id = await self._upsert_incident(anomaly.equipment_id, severity, summary)
            await self._publish_event(incident_id, summary, severity)
        except Exception as exc:
            await self._dead_letter(anomaly, summary, severity, exc)

    async def _upsert_incident(self, equipment_id: str, severity: str, summary: dict) -> str:
        """Idempotent write: update the still-open incident or create one."""
        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        # Include needs_human_review: a diagnosis-pipeline
                        # failure must not cause a duplicate incident either
                        # -- later correlated updates still fold into the
                        # same row (one underlying anomaly == one incident).
                        # Prompt 4: the pipeline now ADVANCES incidents past
                        # 'investigating' (risk_assessed/recommended/
                        # awaiting_approval/...) while the anomaly may still
                        # be active, so fold into ANY non-resolved incident;
                        # only 'resolved' allows a genuinely new one.
                        open_row = await conn.fetchrow(
                            'SELECT "Id" FROM "Incidents" '
                            "WHERE \"EquipmentId\" = $1 AND \"Status\" <> 'resolved' "
                            'ORDER BY "DetectedAt" DESC LIMIT 1 FOR UPDATE',
                            __import__("uuid").UUID(equipment_id),
                        )
                        if open_row:
                            await conn.execute(
                                'UPDATE "Incidents" '
                                'SET "AnomalySummary" = $2, "Severity" = $3, "UpdatedAt" = $4 '
                                'WHERE "Id" = $1',
                                __import__("uuid").UUID(str(open_row["Id"])),
                                json.dumps(summary),
                                severity,
                                datetime.now(timezone.utc),
                            )
                            return str(open_row["Id"])
                        return str(
                            await conn.fetchval(
                                'INSERT INTO "Incidents" '
                                '("Id", "Name", "SiteId", "EquipmentId", "Status", "Severity", "DetectedAt", "AnomalySummary", "CreatedAt", "UpdatedAt") '
                                "VALUES ($1, $2, (SELECT \"SiteId\" FROM \"Equipment\" WHERE \"Id\" = $3), $3, "
                                "'open', $4, $5, $6, $5, $5) RETURNING \"Id\"",
                                __import__("uuid").uuid4(),
                                _incident_name(summary),
                                __import__("uuid").UUID(equipment_id),
                                severity,
                                datetime.now(timezone.utc),
                                json.dumps(summary),
                            )
                        )
            except Exception as exc:
                last_error = exc
                logger.warning("Incident write attempt %d/%d failed: %s", attempt, self._retry_attempts, exc)
                await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"Incident write failed after {self._retry_attempts} attempts") from last_error

    async def _publish_event(self, incident_id: str, summary: dict, severity: str) -> None:
        event = {
            "event": "AnomalyDetected",
            "event_id": f"{incident_id}:{summary['detected_at']}",
            "incident_id": incident_id,
            "equipment_id": summary["equipment_id"],
            "severity": severity,
            "detected_at": summary["detected_at"],
            "sensor_count": summary["sensor_count"],
            "sensors": [
                {"sensor_id": s["sensor_id"], "sensor_type": s["sensor_type"], "value": s["value"], "unit": s["unit"]}
                for s in summary["sensors"]
            ],
        }
        await self._bus.publish("pgai.anomalies", event)
        logger.info("AnomalyDetected published: incident=%s severity=%s sensors=%d",
                    incident_id, severity, len(event["sensors"]))

    async def _dead_letter(self, anomaly, summary: dict, severity: str, exc: Exception) -> None:
        """Dead-letter path: never silently drop a detected anomaly."""
        logger.error(
            "DEAD-LETTER [anomaly dropped]: equipment=%s severity=%s sensors=%s error=%s summary=%s",
            anomaly.equipment_id,
            severity,
            [o.sensor_id for o in anomaly.sensors],
            exc,
            json.dumps(summary),
        )


def _parse_ts(value) -> datetime:
    """Parse ISO-8601 timestamps from stream events; fall back to now."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _sensor_payload(o: AnomalyObservation, sensor_meta: dict) -> dict:
    """Wire shape of one flagged sensor inside anomalySummary (contract:
    tests/contract/test_api_contracts.py + docs/anomaly-event-schema.md).
    Shared by the correlated-summary builder so the schema cannot drift."""
    meta = sensor_meta.get(o.sensor_id) or {}
    return {
        "sensor_id": o.sensor_id,
        "sensor_type": o.sensor_type,
        "value": o.value,
        "unit": o.unit,
        "observed_at": o.observed_at.isoformat(),
        "reason": o.reason,
        "static_margin": round(o.static_margin, 4) if o.static_margin is not None else None,
        "drift_z": round(o.drift_z, 2) if o.drift_z is not None else None,
        "normal_min": meta.get("normal_min"),
        "normal_max": meta.get("normal_max"),
    }


def _correlated_summary(equipment_id: str, sensors_payload: list[dict]) -> dict:
    """The correlated anomalySummary wire shape (contract-tested)."""
    return {
        "equipment_id": equipment_id,
        "detected_at": utcnow_iso(),
        "sensor_count": len(sensors_payload),
        "sensors": sensors_payload,
    }


def _incident_name(summary: dict) -> str:
    """Human-readable incident title from the correlated sensor set."""
    types = sorted({s["sensor_type"] for s in summary["sensors"]})
    return f"Multi-sensor anomaly ({', '.join(types)})"
