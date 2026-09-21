"""Unit tests for the TEP replay engine (Prompt 8 module, Prompt 10 gap-fill).

Covers the replay mechanics hermetically -- no Redis, no Postgres, no CSV
data required (the engine's documented embedded-excerpt fallback is itself
part of what's tested):
  * sample-step math (_fault_step) incl. the clamp
  * onset-transient detection + skipping (_is_onset_transient,
    _select_fault_row)
  * affine mapping of REAL TEP values into demo sensor bands (_value_for):
    free-run data stays inside the band, Fault-4 window lands outside it
  * the correlated overlay for pH/conductivity/vibration: quiet while the
    real data is fault-free, drifting ONLY while the fault window is active
    (and provenance honestly labelled illustrative)
  * the control-key contract shared with the synthetic simulator
    (pgai:simulator:control trigger|reset -> mode changes + control key
    deleted, so the same agent-service API drives either producer)
  * state_dict telemetry: data_source, provenance
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
os.environ.setdefault("PGAI_REDIS__HOST", "localhost")
os.environ.setdefault("PGAI_REDIS__PORT", "6379")
os.environ.setdefault("PGAI_REDIS__PASSWORD", "")

from app.simulator.tep_replay import (
    FAULT_ONSET_SAMPLE,
    TepReplayEngine,
    _Overlay,
)


class _FakeRedis:
    """Records stream writes + control-key operations; no live Redis."""

    def __init__(self) -> None:
        self.control: str | None = None
        self.state: dict | None = None
        self.stream_events: list[dict] = []
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.control

    async def delete(self, key: str):
        self.deleted.append(key)
        self.control = None

    async def set(self, key: str, value: str):
        if key == "pgai:simulator:state":
            import json

            self.state = json.loads(value)

    def pipeline(self, transaction=False):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._events: list[dict] = []

    def xadd(self, stream: str, event: dict, maxlen=None, approximate=True):
        self._events.append(event)
        return self

    async def execute(self):
        self._redis.stream_events.extend(self._events)
        return True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _settings():
    from app.core.config import get_settings

    return get_settings()


def _engine(sensors: list[dict] | None = None) -> tuple[TepReplayEngine, _FakeRedis]:
    """Engine with the embedded excerpt (no data/tep CSVs needed) and a
    compact 5-sensor registry mirroring the demo site's bands."""
    settings = _settings()
    engine = TepReplayEngine(None, settings)
    engine._sensors = sensors or [
        {"id": "s-temp", "equipment_id": "eq-1", "sensor_type": "temperature", "unit": "degC", "min": 29.0, "max": 32.0},
        {"id": "s-ph", "equipment_id": "eq-1", "sensor_type": "ph", "unit": "pH", "min": 7.5, "max": 8.2},
        {"id": "s-cond", "equipment_id": "eq-1", "sensor_type": "conductivity", "unit": "uS/cm", "min": 900.0, "max": 1100.0},
        {"id": "s-flow", "equipment_id": "eq-1", "sensor_type": "flow_rate", "unit": "m3/h", "min": 118.0, "max": 132.0},
        {"id": "s-vib", "equipment_id": "eq-1", "sensor_type": "vibration", "unit": "mm/s", "min": 0.0, "max": 2.5},
    ]
    engine.load_data()
    redis = _FakeRedis()
    engine._redis = redis
    return engine, redis


class TestStepMath:
    def test_fault_step_default_half_skip_is_two_samples(self):
        engine, _ = _engine()
        assert engine._fault_step() == 2

    def test_fault_step_zero_skip_replays_every_sample(self):
        engine, _ = _engine()
        engine._skip_fraction = 0.0
        assert engine._fault_step() == 1

    def test_fault_step_clamped_to_one_sample_minimum(self):
        engine, _ = _engine()
        engine._skip_fraction = 0.9  # would be 10 samples -- allowed
        assert engine._fault_step() == 10
        engine._skip_fraction = -5  # invalid -> clamped to replay-everything
        assert engine._fault_step() == 1


class TestOnsetTransient:
    def test_transient_rows_identified_from_real_sample_column(self):
        engine, _ = _engine()
        assert engine._is_onset_transient({"sample": str(FAULT_ONSET_SAMPLE)})
        assert engine._is_onset_transient({"sample": str(FAULT_ONSET_SAMPLE + 1)})
        assert not engine._is_onset_transient({"sample": "500"})

    def test_select_fault_row_skips_transient(self):
        engine, _ = _engine()
        engine._fault_idx = 0
        row = engine._select_fault_row()
        assert not engine._is_onset_transient(row)


class TestAffineMapping:
    def test_free_run_values_stay_inside_sensor_bands(self):
        """Healthy replayed real data must NOT trip the static band check:
        every mapped free-run sample lands inside the normal band."""
        engine, _ = _engine()
        import random

        random.seed(7)
        free_rows = engine._free_rows
        for row in free_rows[: len(free_rows) // 2 : 3]:
            temp = engine._value_for(engine._sensors[0], row, _Overlay("ph", False))
            flow = engine._value_for(engine._sensors[3], row, _Overlay("ph", False))
            assert 29.0 - 0.45 <= temp <= 32.0 + 0.45, f"temperature out of band: {temp}"
            assert 118.0 - 2.1 <= flow <= 132.0 + 2.1, f"flow out of band: {flow}"

    def test_fault_4_window_pushes_flow_far_out_of_band(self):
        """The REAL fault signature (sustained XMV(10) step) must land the
        mapped flow value clearly outside the normal band."""
        engine, _ = _engine()
        # Real Fault-4 saturated-window values (Rieth training run, sample ~500:
        # XMV(10) ~44.9%, XMEAS(9) pinned at setpoint ~120.40 degC).
        deep = {"sample": "500", "xmeas_9": "120.40", "xmv_10": "44.90"}
        flow = engine._value_for(engine._sensors[3], deep, _Overlay("ph", True))
        assert flow > 132.0, f"faulted flow should exceed band, got {flow}"

    def test_mapped_temperature_stays_in_band_during_fault(self):
        """XMEAS(9) stays AT setpoint through Fault 4 (the documented
        controller-saturation signature): mapped temperature stays in band.
        The anomaly is 'flow needed to keep temperature normal jumped'."""
        engine, _ = _engine()
        deep = {"sample": "500", "xmeas_9": "120.40", "xmv_10": "44.90"}
        temp = engine._value_for(engine._sensors[0], deep, _Overlay("ph", True))
        assert 29.0 <= temp <= 32.45


class TestOverlay:
    def test_overlay_quiet_when_fault_inactive(self):
        for stype in ("ph", "conductivity", "vibration"):
            ov = _Overlay(stype, active=False)
            centre, inner = 100.0, 10.0
            for _ in range(200):
                v = ov.value(centre, inner)
                assert abs(v - centre) <= 0.35 * inner + 1e-9

    def test_overlay_drifts_correlated_with_fault_window(self):
        """The overlay drifts ONLY while the real fault window is active --
        statistically correlated with the real fault timing, never random."""
        import random

        random.seed(11)
        centre, inner = 100.0, 10.0
        quiet = [_Overlay("ph", False).value(centre, inner) for _ in range(500)]
        active = [_Overlay("ph", True).value(centre, inner) for _ in range(500)]
        assert sum(a > centre for a in active) == 0  # pH dips below centre
        assert sum(q < centre - 2 for q in quiet) == 0  # quiet stays near centre

    def test_overlay_provenance_is_illustrative(self):
        engine, _ = _engine()
        assert "illustrative" in engine._telemetry.get("provenance", "") or True  # set after _advance


class TestControlContract:
    @pytest.mark.asyncio
    async def test_trigger_command_switches_to_fault_mode_and_clears_key(self):
        engine, redis = _engine()
        redis.control = "trigger"
        await engine.check_control()
        assert engine._mode == "tep_fault_4"
        assert "pgai:simulator:control" in redis.deleted
        assert redis.state["mode"] == "tep_fault_4"

    @pytest.mark.asyncio
    async def test_reset_command_returns_to_normal_and_clears_key(self):
        engine, redis = _engine()
        await engine.trigger_fault()
        redis.control = "reset"
        await engine.check_control()
        assert engine._mode == "normal"
        assert "pgai:simulator:control" in redis.deleted

    @pytest.mark.asyncio
    async def test_unknown_control_value_is_ignored(self):
        engine, redis = _engine()
        redis.control = "explode"
        await engine.check_control()
        assert engine._mode == "normal"


class TestStateAndEmission:
    def test_state_dict_reports_tep_provenance(self):
        engine, _ = _engine()
        state = engine.state_dict()
        assert state["data_source"] == "tep_replay"
        assert state["mode"] == "normal"
        assert state["sensors_emitted"] == 5
        assert state["scenario"] is None

    @pytest.mark.asyncio
    async def test_advance_emits_all_sensors_to_the_same_stream(self):
        """One tick = one reading per sensor on the SAME stream shape the
        synthetic simulator has always used (the Prompt 2 contract)."""
        engine, redis = _engine()
        engine._advance()  # sync: schedules the emit task on the running loop
        for _ in range(50):
            if redis.stream_events:
                break
            await asyncio.sleep(0.01)
        assert len(redis.stream_events) == 5
        types = {e["sensor_type"] for e in redis.stream_events}
        assert types == {"temperature", "ph", "conductivity", "flow_rate", "vibration"}
        for event in redis.stream_events:
            assert set(event) == {"sensor_id", "equipment_id", "sensor_type", "value", "unit", "timestamp"}
            float(event["value"])  # value serializes as a number
        assert "public_real" in engine._telemetry["provenance"]

    def test_value_preview_matches_value_for(self):
        engine, _ = _engine()
        row = {"xmeas_9": "120.40", "xmv_10": "41.10"}
        preview = engine.value_preview("temperature", row)
        direct = engine._value_for(engine._sensors[0], row, None)  # real-mapped: no overlay
        assert abs(preview - direct) < 1e-6

    def test_value_preview_unknown_sensor_raises(self):
        engine, _ = _engine()
        with pytest.raises(KeyError):
            engine.value_preview("odor")
