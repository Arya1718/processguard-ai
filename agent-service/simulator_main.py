"""Standalone sensor-data process (Prompt 8: synthetic OR real TEP replay).

Chooses the stream producer at startup from SENSOR_DATA_SOURCE:

  synthetic (default) -- the Prompt 2 random-walk simulator (demo fallback)
  tep_replay          -- real Tennessee Eastman Process data (Rieth et al.,
                         DOI 10.7910/DVN/6C3JR1) replayed into the SAME
                         "sensor-readings" stream

Both producers implement the same control contract (pgai:simulator:control
trigger|reset) and the same state key (pgai:simulator:state), so the
agent-service simulator router drives either one identically. Everything
downstream of the stream is unchanged in both modes.

Fail-fast: requires the same PGAI_* environment variables as the agent
service; refuses to start if the database or Redis is unreachable.
"""
from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.db import init_db_pool, seed_demo_data
from app.core.logging_config import configure_logging, get_logger
from app.simulator.service import SensorSimulator
from app.simulator.tep_replay import TepReplayEngine

logger = get_logger(__name__)


async def main() -> None:
    settings = get_settings()

    if settings.sensor_data_source == "tep_replay":
        producer: TepReplayEngine | SensorSimulator = TepReplayEngine(
            None, settings
        )  # engine loads CSVs; redis/db wired below
    else:
        producer = None  # type: ignore[assignment]

    configure_logging(settings.log_level)

    pool = await init_db_pool(settings.db_dsn())
    await seed_demo_data()  # ensure sensors exist before producing readings

    redis_client = aioredis.from_url(settings.redis_url(), decode_responses=True)
    await redis_client.ping()

    if isinstance(producer, TepReplayEngine):
        producer._redis = redis_client
        await producer.load_sensors(pool)
        producer.load_data()
        logger.info(
            "Sensor-data container starting (source=tep_replay, stream=%s)",
            settings.simulator_stream,
        )
        try:
            await producer.run()
        finally:
            await producer.stop()
        return

    simulator = SensorSimulator(redis_client, settings)
    await simulator.load_sensors(pool)

    logger.info(
        "Sensor-data container starting (source=synthetic, stream=%s)",
        settings.simulator_stream,
    )
    try:
        await simulator.run()
    finally:
        await simulator.stop()


if __name__ == "__main__":
    asyncio.run(main())
