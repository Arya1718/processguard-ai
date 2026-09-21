"""Sensor simulator -- realistic industrial telemetry without real IoT.

Emits one reading per sensor every 2-5 seconds into the Redis Stream
"sensor-readings" (the stand-in for Azure Event Hub). Two modes:

  normal    -- slightly noisy readings within each sensor's normal band
  scenario  -- ramps Cooling Water Pump CP-04's sensors toward the reference
               cooling-tower incident values over ~30-60 s, then holds

Control is via Redis keys so a separate simulator container can be driven
from the agent-service API without sharing a process:

  SET pgai:simulator:control  trigger|reset

Runs as its own container in compose (simulator service). It can also be
embedded in the agent-service process for tests via PGAI_SIMULATOR__EMBEDDED.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone

from app.core.logging_config import get_logger

logger = get_logger(__name__)

CONTROL_KEY = "pgai:simulator:control"
STATE_KEY = "pgai:simulator:state"
CONTROL_TTL = 60  # control keys expire so a stale trigger cannot linger

# Reference cooling-tower incident: sensor_type -> (target, hold_noise)
SCENARIO_TARGETS = {
    "temperature": 38.0,
    "ph": 6.8,
    "flow_rate": 106.0,
    "vibration": 4.6,
    "conductivity": 1280.0,
}

SCENARIO_NAME = "cooling-tower-incident"


class SensorSimulator:
    """Produces readings into the sensor-readings stream."""

    def __init__(self, redis_client, settings) -> None:
        self._redis = redis_client
        self._interval_min = settings.simulator_interval_min_seconds
        self._interval_max = settings.simulator_interval_max_seconds
        self._ramp_seconds = settings.simulator_ramp_seconds
        self._stream = settings.simulator_stream
        self._maxlen = settings.simulator_stream_maxlen
        self._poll_seconds = settings.simulator_control_poll_seconds
        self._mode = "normal"
        self._scenario_started_at: float | None = None
        self._running = False
        self._sensors: list[dict] = []  # {id, equipment_id, type, unit, min, max}

    # -- lifecycle ----------------------------------------------------------

    async def load_sensors(self, pool) -> None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT s."Id", s."EquipmentId", s."SensorType", s."Unit", '
                's."NormalMin", s."NormalMax", e."SiteId" '
                'FROM "Sensors" s JOIN "Equipment" e ON e."Id" = s."EquipmentId"'
            )
        self._sensors = [
            {
                "id": str(r["Id"]),
                "equipment_id": str(r["EquipmentId"]),
                "sensor_type": r["SensorType"],
                "unit": r["Unit"],
                "min": float(r["NormalMin"]),
                "max": float(r["NormalMax"]),
            }
            for r in rows
        ]
        logger.info("Simulator loaded %d sensors", len(self._sensors))

    async def run(self) -> None:
        self._running = True
        logger.info(
            "Simulator running (interval %.1f-%.1fs, stream=%s, mode=normal)",
            self._interval_min, self._interval_max, self._stream,
        )
        await self._publish_state()
        while self._running:
            loop_start = time.monotonic()
            await self._check_control()
            await self._emit_all_sensors()
            await self._publish_state()
            elapsed = time.monotonic() - loop_start
            sleep_for = random.uniform(self._interval_min, self._interval_max) - elapsed
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def stop(self) -> None:
        self._running = False

    # -- control ------------------------------------------------------------

    async def _check_control(self) -> None:
        command = await self._redis.get(CONTROL_KEY)
        if command == "trigger":
            await self._redis.delete(CONTROL_KEY)
            self._mode = SCENARIO_NAME
            self._scenario_started_at = time.monotonic()
            logger.info("Simulator: scenario '%s' triggered", SCENARIO_NAME)
            await self._publish_state()
        elif command == "reset":
            await self._redis.delete(CONTROL_KEY)
            self._mode = "normal"
            self._scenario_started_at = None
            logger.info("Simulator: reset to normal mode")
            await self._publish_state()

    async def _publish_state(self) -> None:
        """Best-effort state publish for GET /api/v1/simulator/status."""
        try:
            await self._redis.set(STATE_KEY, json.dumps(self.state_dict()))
        except Exception as exc:
            logger.warning("State publish failed: %s", exc)

    async def trigger_cooling_tower_incident(self) -> dict:
        await self._redis.set(CONTROL_KEY, "trigger", ex=CONTROL_TTL)
        return self.state_dict()

    async def reset(self) -> dict:
        await self._redis.set(CONTROL_KEY, "reset", ex=CONTROL_TTL)
        return self.state_dict()

    def state_dict(self) -> dict:
        return {
            "mode": self._mode,
            "scenario": SCENARIO_NAME if self._mode != "normal" else None,
            "scenario_started": self._scenario_started_at is not None,
            "sensors_emitted": len(self._sensors),
        }

    # -- emission -----------------------------------------------------------

    def _normal_value(self, sensor: dict) -> float:
        midpoint = (sensor["min"] + sensor["max"]) / 2.0
        half_band = (sensor["max"] - sensor["min"]) / 2.0
        noise = half_band * 0.15
        return random.gauss(midpoint, noise / 1.5) if half_band > 0 else midpoint

    def _scenario_value(self, sensor: dict, elapsed: float) -> float:
        target = SCENARIO_TARGETS.get(sensor["sensor_type"])
        if target is None:
            return self._normal_value(sensor)
        start = sensor["min"] if target >= sensor["min"] else sensor["max"]
        ramp = min(elapsed / self._ramp_seconds, 1.0)
        eased = ramp * ramp * (3 - 2 * ramp)  # smoothstep
        base = start + (target - start) * eased
        noise = abs(target - start) * 0.01
        return random.gauss(base, noise) if noise > 0 else base

    async def _emit_all_sensors(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self._redis.pipeline(transaction=False) as pipe:
            for sensor in self._sensors:
                if self._mode == "normal":
                    value = self._normal_value(sensor)
                else:
                    value = self._scenario_value(
                        sensor, time.monotonic() - (self._scenario_started_at or time.monotonic())
                    )
                event = {
                    "sensor_id": sensor["id"],
                    "equipment_id": sensor["equipment_id"],
                    "sensor_type": sensor["sensor_type"],
                    "value": f"{value:.4f}",
                    "unit": sensor["unit"],
                    "timestamp": now_iso,
                }
                pipe.xadd(self._stream, event, maxlen=self._maxlen, approximate=True)
            await pipe.execute()
