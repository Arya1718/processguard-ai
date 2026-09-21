"""Unit tests for the Detection Agent's pure scoring logic (no network/IO)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
from app.simulator.service import SCENARIO_TARGETS, SensorSimulator

T0 = datetime(2026, 9, 18, 14, 0, 0, tzinfo=timezone.utc)
TEMP = SensorThresholds(normal_min=29.0, normal_max=32.0)


def _reading(value: float, at: datetime = T0, sensor_id: str = "s-temp") -> Reading:
    return Reading(
        sensor_id=sensor_id, equipment_id="equip-1", sensor_type="temperature",
        value=value, unit="degC", occurred_at=at,
    )


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
def test_baseline_flat_sensor_has_sigma_floor() -> None:
    stats = compute_baseline([31.0, 31.0, 31.0, 31.0])
    assert stats.mean == 31.0
    assert stats.sigma > 0  # floor prevents divide-by-zero


def test_baseline_noisy_sensor() -> None:
    stats = compute_baseline([30.9, 31.1, 31.0, 30.8, 31.2])
    assert 30.9 <= stats.mean <= 31.1
    assert 0.01 < stats.sigma < 0.5


# ---------------------------------------------------------------------------
# Static band check
# ---------------------------------------------------------------------------
def test_normal_reading_is_not_anomalous() -> None:
    baseline = compute_baseline([30.9, 31.0, 31.1, 31.0])
    scored = score_reading(_reading(31.0), TEMP, baseline, static_z=0.05, drift_z=3.0)
    assert not scored.anomalous
    assert not scored.static_anomaly
    assert not scored.drift_anomaly


def test_value_outside_band_is_anomalous() -> None:
    baseline = compute_baseline([31.0, 31.0, 31.0])
    scored = score_reading(_reading(38.0), TEMP, baseline, static_z=0.05, drift_z=3.0)
    assert scored.anomalous and scored.static_anomaly
    assert scored.static_margin is not None and scored.static_margin > 0.05


def test_slight_overshoot_within_tolerance_is_not_anomalous() -> None:
    baseline = compute_baseline([31.0, 31.0, 31.0])
    # band=3.0; 5% tolerance = 0.15 -> 32.1 is inside tolerance
    scored = score_reading(_reading(32.1), TEMP, baseline, static_z=0.05, drift_z=50.0)
    assert not scored.static_anomaly
    # 32.5 is beyond tolerance
    scored2 = score_reading(_reading(32.5), TEMP, baseline, static_z=0.05, drift_z=50.0)
    assert scored2.static_anomaly


# ---------------------------------------------------------------------------
# Baseline drift check
# ---------------------------------------------------------------------------
def test_fast_drift_within_band_is_anomalous() -> None:
    # Sensor has been hovering at 29.2 (low end); a jump to 31.4 stays inside
    # the band but is far off its own baseline -> drift anomaly.
    baseline = compute_baseline([29.2, 29.2, 29.15, 29.25, 29.2])
    scored = score_reading(_reading(31.4), TEMP, baseline, static_z=0.05, drift_z=3.0)
    assert scored.anomalous and scored.drift_anomaly


def test_gradual_drift_is_not_anomalous() -> None:
    # Baseline tracks a slow climb; latest value is close to that baseline.
    baseline = compute_baseline([30.8, 30.9, 31.0, 31.0, 31.1])
    scored = score_reading(_reading(31.05), TEMP, baseline, static_z=0.05, drift_z=3.0)
    assert not scored.anomalous


# ---------------------------------------------------------------------------
# Correlation window
# ---------------------------------------------------------------------------
def _obs(sensor: str, at: datetime, equipment: str = "equip-1") -> AnomalyObservation:
    return AnomalyObservation(
        equipment_id=equipment, sensor_id=sensor, sensor_type=sensor,
        observed_at=at, value=99.0, unit="x", reason="test",
    )


def test_five_sensors_within_window_correlate_to_one_incident() -> None:
    obs = [
        _obs("temp", T0),
        _obs("ph", T0 + timedelta(seconds=3)),
        _obs("flow", T0 + timedelta(seconds=6)),
        _obs("vib", T0 + timedelta(seconds=9)),
        _obs("cond", T0 + timedelta(seconds=12)),
    ]
    incidents = correlate_anomalies(obs, correlation_window_seconds=30, max_gap_seconds=90, now=T0 + timedelta(seconds=13))
    assert len(incidents) == 1
    assert len(incidents[0].sensors) == 5


def test_chain_linking_groups_indirect_neighbors() -> None:
    # A~B 25s apart, B~C 25s apart (A~C 50s > window) -> still ONE cluster.
    obs = [_obs("a", T0), _obs("b", T0 + timedelta(seconds=25)), _obs("c", T0 + timedelta(seconds=50))]
    incidents = correlate_anomalies(obs, correlation_window_seconds=30, max_gap_seconds=90, now=T0 + timedelta(seconds=51))
    assert len(incidents) == 1
    assert len(incidents[0].sensors) == 3


def test_stale_cluster_is_dropped() -> None:
    obs = [_obs("temp", T0), _obs("ph", T0 + timedelta(seconds=2))]
    incidents = correlate_anomalies(
        obs, correlation_window_seconds=30, max_gap_seconds=90, now=T0 + timedelta(seconds=300)
    )
    assert incidents == []


def test_different_equipment_never_correlate() -> None:
    obs = [_obs("temp", T0, equipment="e1"), _obs("ph", T0 + timedelta(seconds=1), equipment="e2")]
    incidents = correlate_anomalies(obs, correlation_window_seconds=30, max_gap_seconds=90, now=T0 + timedelta(seconds=2))
    assert len(incidents) == 2


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------
def test_severity_levels() -> None:
    assert severity_from_score([0.02], [None]) == "low"
    assert severity_from_score([0.2], [None]) == "medium"
    assert severity_from_score([0.6], [None]) == "high"
    assert severity_from_score([None], [12.0]) == "high"


# ---------------------------------------------------------------------------
# Simulator value generation (pure helpers)
# ---------------------------------------------------------------------------
def _make_simulator() -> SensorSimulator:
    class _FakeRedis:
        pass

    class _FakeSettings:
        simulator_interval_min_seconds = 2
        simulator_interval_max_seconds = 5
        simulator_ramp_seconds = 30
        simulator_stream = "sensor-readings"
        simulator_stream_maxlen = 1000
        simulator_control_poll_seconds = 1

    return SensorSimulator(_FakeRedis(), _FakeSettings())


def test_simulator_normal_values_stay_in_band() -> None:
    sim = _make_simulator()
    sensor = {"id": "s", "equipment_id": "e", "sensor_type": "temperature", "unit": "degC", "min": 29.0, "max": 32.0}
    for _ in range(200):
        value = sim._normal_value(sensor)
        assert 28.0 <= value <= 33.0  # gentle noise, never far outside


def test_simulator_scenario_ramps_toward_targets_and_holds() -> None:
    sim = _make_simulator()
    sensor = {"id": "s", "equipment_id": "e", "sensor_type": "temperature", "unit": "degC", "min": 29.0, "max": 32.0}
    early = [sim._scenario_value(sensor, t) for t in (0.0, 3.0)]
    assert all(v < 31.0 for v in early)  # ramp start still near normal
    held = [sim._scenario_value(sensor, 120.0) for _ in range(50)]
    target = SCENARIO_TARGETS["temperature"]
    assert all(abs(v - target) < 0.5 for v in held)  # held at 38 degC
