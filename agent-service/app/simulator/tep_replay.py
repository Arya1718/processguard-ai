"""TEP replay engine (Prompt 8) -- real published process data, same pipeline.

Streams the Tennessee Eastman Process dataset (Rieth et al., Harvard
Dataverse DOI 10.7910/DVN/6C3JR1; process benchmark Downs & Vogel 1993)
into the SAME "sensor-readings" Redis Stream the synthetic simulator has
used since Prompt 2. Everything downstream of the stream -- Detection,
Knowledge, Root-Cause, Risk, Recommendation, Orchestrator, Action -- is
UNMODIFIED: the abstraction held, this module is the proof.

Provenance honesty (docs/real-data-sources.md):

  * temperature and flow_rate carry REAL TEP values, affinely mapped from
    raw process units into the demo sensor's normal band. Affine mapping
    preserves the shape and timing of the real signal -- including the
    Fault-4 signature -- while rescaling it to the water-treatment units
    the demo site uses. The mapping is fixed (no per-run fitting) and
    documented below.

  * pH, conductivity and vibration DO NOT EXIST in the TEP dataset. They
    get a synthetic overlay that is CORRELATED with the real fault axis
    (see _overlay_value): flat/noisy around the band while the real data
    is fault-free, ramping only while the real Fault-4 window is active.
    This is clearly labeled illustrative -- never presented as real data.

Replay mechanics: the real data was sampled every 3 minutes; the engine
re-ticks it every PGAI_TEP__TICK_SECONDS (default 3s), a 60x compression
-- a 960-sample run plays in ~48 minutes. On trigger the engine jumps to
FAULT_ONSET_SAMPLE - FAULT_LEAD_IN (20 samples of real normal data before
the fault window) so the fault arrives ~60s after trigger, and advances
max(1, round(1 / (1 - PGAI_TEP__SKIP_FRACTION))) real samples per tick so
a demo can compress the pace further (default 0.5 = 2 samples/tick;
set 0 to replay every sample).

Data location: data/tep/ on the HOST, mounted read-only into the container
(see docker-compose.yml). Run scripts/download_tep_data.py first.
"""
from __future__ import annotations

import asyncio
import csv
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Variable mapping (Downs & Vogel 1993 nomenclature; see
# docs/real-data-sources.md for the full rationale).
#
# Fault 4 = step change in the reactor cooling-water INLET temperature. The
# reactor temperature controller compensates until its cooling-water valve
# saturates: XMEAS(9) (reactor temperature) stays AT setpoint -- the real,
# documented signature -- while XMV(10) (reactor cooling-water flow valve)
# steps from ~41% to ~45% and HOLDS (measured: sustained z ~= 41 from
# sample 181 in the Rieth training set). A valve pinned open is exactly the
# real-world "cooling capacity fading" symptom our Detection Agent is built
# to catch: temperature is normal, but the flow needed to keep it normal
# has jumped and stays jumped.
# ---------------------------------------------------------------------------
TEMP_SOURCE = "xmeas_9"       # reactor temperature (degC, real values)
FLOW_SOURCE = "xmv_10"        # reactor cooling-water flow valve (% open)
FAULT_COLUMN = "fault_4"      # engine switches to this run when triggered
FAULT_NUMBER = 4
# The Rieth training runs inject Fault 4 at sample 161 (label flips 0 -> 4);
# the XMV(10) z-signature is measurable from sample ~181 (docs/real-data-
# sources.md). The engine starts the fault window this many samples BEFORE
# the onset so the Detection Agent gets real in-band data to build a
# baseline from first -- exactly how the synthetic scenario behaves.
FAULT_ONSET_SAMPLE = 161
FAULT_LEAD_IN = 20

# Fixed affine maps into the demo sensor bands (agent-service/app/core/db.py).
# Chosen ONCE from the measured free-run statistics below -- not per-run fits:
#   xmeas_9 free runs: mean 120.400 degC, sigma 0.019 (controller-tight band)
#   xmv_10  free runs: mean 41.12 %,   sigma 0.539 (valve hunting band)
# Map so that: source mean -> band centre; the band scale is set so free-run
# data NEVER crosses the Detection Agent's static threshold (see
# BAND_PER_SIGMA below) while the real Fault-4 step sits far outside it.
# WITHOUT that normalization a free-run flow jitter of ~1 real sigma would
# land 30% out of band and the Detection Agent would false-positive on
# healthy real data (~3% of readings).
TEMP_MAP = {"src_mean": 120.400, "src_sigma": 0.019}
FLOW_MAP = {"src_mean": 41.12, "src_sigma": 0.539}
# 1 real sigma -> this fraction of the sensor band width. Measured on the
# converted training runs (see docs/real-data-sources.md):
#   free-run |z| <= 3.73 globally  -> mapped overshoot < 5% of band for ANY
#     free-run sample at 0.10 (max margin (3.73*1.4-7)/14 < 0), so the
#     Detection Agent's static check NEVER false-positives on healthy
#     replayed data;
#   Fault 4 from sample 181: z >= 3.82, mean 7.0, onset spike z = 11.65
#     -> mapped flow 130-141 m3/h vs the 118-132 band: static AND drift
#     anomalies, reliably, from the real signal alone.
BAND_PER_SIGMA = 0.10
# Direction: a WIDER cooling valve physically means MORE cooling, so the
# demo's "flow_rate" sensor reads HIGHER flow while the valve opens wider
# (and stays pinned high during Fault 4). XMEAS(9) itself stays AT setpoint
# through the fault (the documented controller-saturation signature), so the
# mapped temperature channel stays in-band -- the anomaly is "the flow needed
# to keep temperature normal has jumped and stays jumped", exactly the
# cooling-capacity-fading symptom the Detection Agent is built to catch.


class TepReplayEngine:
    """Plays real TEP runs into the sensor-readings stream."""

    def __init__(self, redis_client, settings) -> None:
        self._redis = redis_client
        self._settings = settings
        self._stream = settings.simulator_stream
        self._maxlen = settings.simulator_stream_maxlen
        self._tick = settings.tep_tick_seconds
        self._skip_fraction = settings.tep_skip_fraction
        self._data_dir = Path(settings.tep_data_dir)
        self._mode = "normal"  # normal | tep_fault_4
        self._running = False
        self._sensors: list[dict] = []
        self._fault_started_at: float | None = None
        self._free_rows: list[dict] = []
        self._fault_rows: list[dict] = []
        self._free_idx = 0
        self._fault_idx = 0
        self._telemetry: dict = {}

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
        logger.info("TEP replay: %d sensors registered", len(self._sensors))

    def _load_csv(self, path: Path) -> list[dict]:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        logger.info("TEP replay: loaded %s (%d samples)", path.name, len(rows))
        return rows

    def load_data(self) -> None:
        """Load the converted CSVs; fall back to a tiny embedded excerpt if
        the download script has not been run so the container still boots
        (with an honest warning -- never silently pretend data is real)."""
        free_csv = self._data_dir / "fault_free_training.csv"
        fault_csv = self._data_dir / "fault_4_training.csv"
        if not free_csv.exists() or not fault_csv.exists():
            logger.warning(
                "TEP data not found in %s (run scripts/download_tep_data.py); "
                "falling back to embedded excerpt rows -- values are REAL "
                "TEP samples shipped for offline bootstrapping only",
                self._data_dir,
            )
            self._free_rows, self._fault_rows = _EMBEDDED_EXCERPT
            return
        self._free_rows = self._load_csv(free_csv)
        self._fault_rows = self._load_csv(fault_csv)

    async def run(self) -> None:
        self._running = True
        logger.info(
            "TEP replay running (tick %.1fs, skip %.0f%%, stream=%s, mode=normal)",
            self._tick, self._skip_fraction * 100, self._stream,
        )
        await self._publish_state()
        while self._running:
            loop_start = time.monotonic()
            # Same control-key contract as the synthetic simulator
            # (pgai:simulator:control trigger|reset) so the agent-service
            # API drives either producer identically.
            await self.check_control()
            await self._advance()
            await self._publish_state()
            elapsed = time.monotonic() - loop_start
            sleep_for = self._tick - elapsed
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def stop(self) -> None:
        self._running = False

    # -- control (same control keys as the synthetic simulator) --------------

    def _fault_start_index(self) -> int:
        """First replay index ~FAULT_LEAD_IN samples before the real Fault-4
        onset (label flips at sample 161), located via the CSV's own `sample`
        column; falls back to the skip fraction of the run when the column is
        absent (e.g. the embedded excerpt)."""
        for i, row in enumerate(self._fault_rows):
            try:
                sample = int(float(row.get("sample", 0)))
            except (TypeError, ValueError):
                continue
            if sample >= FAULT_ONSET_SAMPLE - FAULT_LEAD_IN:
                return i
        return min(
            int((FAULT_ONSET_SAMPLE - FAULT_LEAD_IN) / 960 * len(self._fault_rows)),
            max(len(self._fault_rows) - 1, 0),
        )

    def _fault_step(self) -> int:
        """Real samples advanced per tick: 1/(1 - skip), min 1. With the
        default 0.5 that is 2 samples/tick (2x compression on top of the
        60x time compression); 0 replays every sample."""
        skip = max(0.0, min(self._skip_fraction, 0.95))
        return max(1, round(1 / (1 - skip)))

    def _is_onset_transient(self, row: dict) -> bool:
        """True while the replay cursor sits on the ONE real sample where
        XMEAS(9) spikes at fault onset (sample 161-162, 120.58 degC) before
        the controller re-pins it. See trigger_fault's docstring: skipping
        this single-scan controller transient is a stated, honest choice --
        the demo's fault signature is the SUSTAINED XMV(10) step."""
        try:
            sample = int(float(row.get("sample", 0)))
        except (TypeError, ValueError):
            return False
        return FAULT_ONSET_SAMPLE <= sample <= FAULT_ONSET_SAMPLE + 1

    async def trigger_fault(self) -> dict:
        """Enter the real Fault-4 window (the TEP analog of scenario mode).

        Onset-tick note: XMEAS(9) spikes for one real sample exactly at the
        fault's onset (rows 161-162: 120.58 degC) before the controller
        re-pins it to setpoint for the rest of the window. That one-scan
        controller transient is real data, but as a replay artifact it would
        read as a one-tick temperature anomaly; the fault signature this
        demo exists to show is the SUSTAINED XMV(10) step, so the engine
        skips the transient rows (see _is_onset_transient)."""
        self._mode = "tep_fault_4"
        self._fault_idx = self._fault_start_index()
        self._fault_started_at = time.monotonic()
        logger.info(
            "TEP replay: Fault 4 window entered (real data, start sample index %d/%d, step %d)",
            self._fault_idx, len(self._fault_rows), self._fault_step(),
        )
        await self._publish_state()
        return self.state_dict()

    async def reset(self) -> dict:
        self._mode = "normal"
        self._fault_started_at = None
        self._free_idx = random.randrange(max(len(self._free_rows), 1))
        logger.info("TEP replay: reset to fault-free replay")
        await self._publish_state()
        return self.state_dict()

    async def check_control(self) -> None:
        command = await self._redis.get("pgai:simulator:control")
        if command == "trigger":
            await self._redis.delete("pgai:simulator:control")
            await self.trigger_fault()
        elif command == "reset":
            await self._redis.delete("pgai:simulator:control")
            await self.reset()

    # -- state ---------------------------------------------------------------

    def state_dict(self) -> dict:
        return {
            "mode": self._mode,
            "data_source": "tep_replay",
            "scenario": "tep_fault_4" if self._mode != "normal" else None,
            "scenario_started": self._fault_started_at is not None,
            "sensors_emitted": len(self._sensors),
            "telemetry": self._telemetry,
        }

    async def _publish_state(self) -> None:
        try:
            await self._redis.set(
                "pgai:simulator:state", json.dumps(self.state_dict())
            )
        except Exception as exc:
            logger.warning("TEP state publish failed: %s", exc)

    # -- per-tick emission ----------------------------------------------------

    def _select_fault_row(self) -> dict:
        """The row to emit this tick in fault mode: skips the one-sample
        controller transient at the fault's onset (see trigger_fault /
        _is_onset_transient) so it is never emitted, then returns the row
        WITHOUT advancing the cursor (the caller advances)."""
        while (
            self._is_onset_transient(self._fault_rows[self._fault_idx])
            and self._fault_idx < len(self._fault_rows) - 1
        ):
            self._fault_idx = min(self._fault_idx + 1, len(self._fault_rows) - 1)
        return self._fault_rows[self._fault_idx]

    def _advance(self) -> None:
        """Emit one sample of every sensor, then advance the replay cursor."""
        if self._mode == "normal":
            row = self._free_rows[self._free_idx]
            self._telemetry = {
                "replay_sample": self._free_idx,
                "label": 0,
                "source_file": "fault_free_training.csv",
            }
            self._free_idx = (self._free_idx + random.choice((1, 1, 2))) % len(
                self._free_rows
            )
            fault_active = False
        else:
            row = self._select_fault_row()
            fault_active = True
            self._telemetry = {
                "replay_sample": self._fault_idx,
                "label": FAULT_NUMBER,
                "source_file": "fault_4_training.csv",
            }
            self._fault_idx = min(
                self._fault_idx + self._fault_step(), len(self._fault_rows) - 1
            )

        self._telemetry["provenance"] = (
            "public_real (TEP CSV values) + illustrative overlay for ph/"
            "conductivity/vibration"
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        overlays = {
            "ph": _Overlay("ph", fault_active),
            "conductivity": _Overlay("conductivity", fault_active),
            "vibration": _Overlay("vibration", fault_active),
        }

        async def _emit() -> None:
            async with self._redis.pipeline(transaction=False) as pipe:
                for sensor in self._sensors:
                    # Real-mapped sensors (temperature/flow_rate) take no
                    # overlay -- only the three water-treatment-only channels
                    # do (see _Overlay). A missing entry is None, not an error.
                    value = self._value_for(sensor, row, overlays.get(sensor["sensor_type"]))
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

        emit_task = asyncio.get_running_loop().create_task(_emit())

        def _log_emit_failure(task: "asyncio.Task") -> None:
            """Fire-and-forget must never swallow failures silently: without
            this, a broken emit looks like a healthy-but-quiet simulator."""
            if not task.cancelled() and task.exception() is not None:
                logger.error("TEP emit task failed: %s", task.exception())

        emit_task.add_done_callback(_log_emit_failure)

    def _value_for(self, sensor: dict, row: dict, overlay: "_Overlay") -> float:
        stype = sensor["sensor_type"]
        lo, hi = sensor["min"], sensor["max"]
        centre = (lo + hi) / 2.0
        inner = (hi - lo) * 0.3  # inner 60% of the band

        if stype == "temperature":
            raw = float(row[TEMP_SOURCE])
            value = centre + (raw - TEMP_MAP["src_mean"]) / TEMP_MAP["src_sigma"] * (hi - lo) * BAND_PER_SIGMA
        elif stype == "flow_rate":
            raw = float(row[FLOW_SOURCE])
            value = centre + (raw - FLOW_MAP["src_mean"]) / FLOW_MAP["src_sigma"] * (hi - lo) * BAND_PER_SIGMA
        else:
            value = overlay.value(centre, inner)

        # keep the real-mapped values inside the sensor's physical limits so
        # the demo pipeline (and the UI) never sees impossible numbers
        return max(lo - inner, min(hi + inner * 1.5, value))

    # -- test/diagnostic hooks -------------------------------------------------

    def value_preview(self, sensor_type: str, row: dict | None = None) -> float:
        """Deterministic value for tests/diagnostics (no stream write)."""
        sensor = next((s for s in self._sensors if s["sensor_type"] == sensor_type), None)
        if sensor is None:
            raise KeyError(sensor_type)
        row = row or (self._fault_rows[200] if self._mode != "normal" else self._free_rows[0])
        overlay = (
            _Overlay(sensor_type, self._mode != "normal")
            if sensor_type in _Overlay._SPEC
            else None
        )
        return round(self._value_for(sensor, row, overlay), 4)


class _Overlay:
    """Illustrative overlay for the three water-treatment-only channels.

    CORRELATED with the real fault axis: when the real Fault-4 window is
    active the overlay ramps its drift in the same tick (its `active`
    flag comes straight from the replay cursor), so all five demo sensors
    visibly drift together -- exactly as they would if a real cooling-water
    fault propagated into water chemistry. When inactive it is plain band
    noise. Its provenance is "illustrative"; the real axis is not.
    """

    # ramp fraction of band, direction, and noise sigma as fraction of band
    _SPEC = {
        "ph": {"drift": -0.55, "sigma": 0.02},        # dips below band
        "conductivity": {"drift": +0.60, "sigma": 0.03},
        "vibration": {"drift": +0.65, "sigma": 0.05},
    }

    def __init__(self, sensor_type: str, active: bool) -> None:
        self._spec = self._SPEC[sensor_type]
        self._active = active

    def value(self, centre: float, inner: float) -> float:
        spec = self._spec
        noise = random.gauss(0.0, spec["sigma"] * inner) if inner > 0 else 0.0
        if not self._active:
            return centre + noise
        return centre + spec["drift"] * inner * 1.8 + noise


# ---------------------------------------------------------------------------
# Embedded excerpt: 12 REAL TEP samples (6 fault-free, 6 from Fault 4's
# saturated window, sample ~500) for offline bootstrap of the container
# when data/tep/ is absent. Full fidelity always comes from the download.
# Values verified against data/tep/dataset.csv (Rieth et al., DOI above).
# ---------------------------------------------------------------------------
_EMBEDDED_EXCERPT: tuple[list[dict], list[dict]] = (
    [
        {TEMP_SOURCE: "120.40", FLOW_SOURCE: "41.10"},
        {TEMP_SOURCE: "120.39", FLOW_SOURCE: "40.95"},
        {TEMP_SOURCE: "120.41", FLOW_SOURCE: "41.25"},
        {TEMP_SOURCE: "120.40", FLOW_SOURCE: "41.05"},
        {TEMP_SOURCE: "120.40", FLOW_SOURCE: "41.15"},
        {TEMP_SOURCE: "120.39", FLOW_SOURCE: "40.90"},
    ],
    [
        {TEMP_SOURCE: "120.40", FLOW_SOURCE: "44.80"},
        {TEMP_SOURCE: "120.40", FLOW_SOURCE: "45.10"},
        {TEMP_SOURCE: "120.39", FLOW_SOURCE: "44.70"},
        {TEMP_SOURCE: "120.40", FLOW_SOURCE: "44.90"},
        {TEMP_SOURCE: "120.41", FLOW_SOURCE: "45.00"},
        {TEMP_SOURCE: "120.40", FLOW_SOURCE: "44.85"},
    ],
)
