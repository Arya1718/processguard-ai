"""Prompt 8 -- TEP replay mode drives the UNMODIFIED Detection Agent.

The contract under test: the TepReplayEngine streams real Rieth et al.
Tennessee Eastman Process data into the same stream shape the synthetic
simulator has always used, and the Prompt 2 detection logic
(compute_baseline / score_reading / correlate_anomalies / severity_from_score,
imported unchanged) flags the real Fault-4 window as ONE correlated incident
-- with no false static alarms on healthy replayed data.

Offline safety: the engine's embedded real-data excerpt (12 genuine TEP
samples shipped in the source) keeps the core fault test runnable without
the 18.9 MB mirror; tests that need the FULL converted runs skip with an
EXPLICIT message when data/tep/*.csv is absent (run
scripts/download_tep_data.py --from-small-tep data/small_tep.zip). The skip
is a documented CI tradeoff, never a silent pass (docs/real-data-sources.md).
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agents.detection.scoring import (
    AnomalyObservation,
    Reading,
    SensorThresholds,
    compute_baseline,
    correlate_anomalies,
    score_reading,
    severity_from_score,
)
from app.simulator.tep_replay import (
    FAULT_LEAD_IN,
    FAULT_ONSET_SAMPLE,
    TepReplayEngine,
    _Overlay,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_DATA_DIR = REPO_ROOT / "data" / "tep"
REAL_CSVS_PRESENT = (REAL_DATA_DIR / "fault_free_training.csv").exists() and (
    REAL_DATA_DIR / "fault_4_training.csv").exists()
MISSING_DATA_REASON = (
    "converted TEP runs not present -- run "
    "scripts/download_tep_data.py --from-small-tep data/small_tep.zip "
    "on a machine with the mirror (CI has no guaranteed network access; "
    "this tradeoff is documented in docs/real-data-sources.md)"
)

T0 = datetime.now(timezone.utc)  # harness clock starts at real "now" so the
# unmodified correlate_anomalies() staleness check behaves as in production

# The demo sensor bands seeded since Prompt 2 (app/core/db.py::DEMO_SENSORS).
BANDS: dict[str, tuple[float, float]] = {
    "temperature": (29.0, 32.0),
    "ph": (7.5, 8.2),
    "flow_rate": (118.0, 132.0),
    "vibration": (1.0, 2.5),
    "conductivity": (950.0, 1100.0),
}


class FakeSettings:
    simulator_stream = "sensor-readings"
    simulator_stream_maxlen = 100000
    tep_tick_seconds = 3.0
    tep_skip_fraction = 0.5

    def __init__(self, data_dir: str | None = None) -> None:
        self.tep_data_dir = data_dir or "data/tep"


def make_engine(data_dir: str | None = None) -> TepReplayEngine:
    """Engine wired the way simulator_main.py does, minus redis/db (the
    detection tests below drive _value_for directly through the same
    scoring path the agent uses). Defaults to the repo's real data dir."""
    engine = TepReplayEngine(
        None, FakeSettings(data_dir or str(REAL_DATA_DIR))  # type: ignore[arg-type]
    )
    engine.load_data()
    engine._sensors = [
        {
            "id": f"sensor-{stype}",
            "equipment_id": "equip-1",
            "sensor_type": stype,
            "unit": "u",
            "min": lo,
            "max": hi,
        }
        for stype, (lo, hi) in BANDS.items()
    ]
    return engine


class DetectionHarness:
    """Score replayed values through the UNMODIFIED Prompt 2 detection logic,
    mirroring DetectionAgent._process_reading (rolling 40-sample baseline per
    sensor; anomalies collected into a correlation buffer)."""

    def __init__(self, engine: TepReplayEngine) -> None:
        self.engine = engine
        self.baselines: dict[str, list[float]] = {t: [] for t in BANDS}
        self.anomalies: list[tuple[str, int, str, str, float | None, float | None]] = []
        self.clock = T0

    def tick(self, mode: str, tick_no: int) -> None:
        if mode == "normal":
            row = self.engine._free_rows[min(tick_no, len(self.engine._free_rows) - 1)]
        else:
            # The engine's own row selection (onset-transient skip included)
            # so the harness sees exactly what a live tick would emit.
            row = self.engine._select_fault_row()
        active = mode != "normal"
        self.clock += timedelta(seconds=3)
        for sensor in self.engine._sensors:
            stype = sensor["sensor_type"]
            value = self.engine._value_for(sensor, row, _Overlay("ph", active))
            baseline = compute_baseline(self.baselines[stype][-40:])
            scored = score_reading(
                Reading(
                    sensor_id=sensor["id"], equipment_id="equip-1",
                    sensor_type=stype, value=value, unit="u",
                    occurred_at=self.clock,
                ),
                SensorThresholds(sensor["min"], sensor["max"]),
                baseline, static_z=0.05, drift_z=3.0,
            )
            if scored.anomalous:
                self.anomalies.append(
                    (mode, tick_no, stype, scored.reason,
                     scored.static_margin, scored.drift_z)
                )
            self.baselines[stype].append(value)


# ---------------------------------------------------------------------------
# Healthy replay: the real fault-free run must not trip the STATIC check
# ---------------------------------------------------------------------------
def test_free_replay_never_trips_static_check() -> None:
    engine = make_engine()
    harness = DetectionHarness(engine)
    random.seed(1)
    for i in range(400):
        harness.tick("normal", i)
    static = [a for a in harness.anomalies if a[0] == "normal" and a[4] is not None]
    assert static == [], (
        "healthy real TEP data must never cross the static band check; "
        f"got {static} -- the affine map gain (BAND_PER_SIGMA) is too wide"
    )


def test_free_replay_drift_noise_is_rare_and_documented() -> None:
    # Real XMV(10) valve hunting occasionally produces an isolated drift flag
    # (~1% of readings, scale-invariant). Honest expectation, upper-bounded;
    # see docs/real-data-sources.md ("known behavior", not a hidden flaw).
    engine = make_engine()
    harness = DetectionHarness(engine)
    random.seed(1)
    for i in range(400):
        harness.tick("normal", i)
    drift = [a for a in harness.anomalies if a[0] == "normal"]
    assert len(drift) <= 0.02 * 400 * len(BANDS)


# ---------------------------------------------------------------------------
# The real Fault-4 window: ONE correlated incident from the UNMODIFIED agent
# ---------------------------------------------------------------------------
def test_fault4_replay_flags_one_incident_with_unmodified_detection() -> None:
    engine = make_engine()
    harness = DetectionHarness(engine)
    random.seed(1)
    for i in range(40):  # healthy baseline window, exactly like a live demo
        harness.tick("normal", i)

    engine._mode = "tep_fault_4"
    engine._fault_idx = engine._fault_start_index()
    first_flow_tick: int | None = None
    for k in range(60):
        harness.tick("fault", k)
        engine._fault_idx = min(
            engine._fault_idx + engine._fault_step(), len(engine._fault_rows) - 1
        )
        if first_flow_tick is None and any(
            mode == "fault" and stype == "flow_rate"
            for mode, _, stype, _, _, _ in harness.anomalies
        ):
            first_flow_tick = k

    assert first_flow_tick is not None, (
        "the real XMV(10) Fault-4 step must be flagged by the unmodified "
        "Detection Agent scoring"
    )
    assert first_flow_tick <= 20, "fault should be detected within ~1 minute of trigger"

    # The correlated, persisted shape: ONE incident for the equipment.
    observations = [
        AnomalyObservation(
            equipment_id="equip-1", sensor_id=f"sensor-{st}", sensor_type=st,
            observed_at=harness.clock, value=0.0, unit="u", reason=reason,
            static_margin=margin, drift_z=drift,
        )
        for mode, tick_no, st, reason, margin, drift in harness.anomalies
        if mode == "fault"
    ]
    incidents = correlate_anomalies(observations, 30.0, 90.0)
    assert len(incidents) == 1
    assert incidents[0].equipment_id == "equip-1"
    flagged = {o.sensor_type for o in incidents[0].sensors}
    assert "flow_rate" in flagged

    # XMEAS(9) stays pinned at setpoint during Fault 4 (the documented
    # controller-saturation pattern) -> the mapped TEMPERATURE channel must
    # stay inside its band through the fault window.
    temp_static = [
        a for a in harness.anomalies
        if a[0] == "fault" and a[2] == "temperature" and a[4] is not None
    ]
    assert temp_static == [], "temperature must stay in band (pinned XMEAS(9))"

    margins = [a[4] for a in harness.anomalies if a[0] == "fault" and a[4] is not None]
    drifts = [a[5] for a in harness.anomalies if a[0] == "fault" and a[5] is not None]
    severity = severity_from_score(margins, drifts)
    assert severity in ("medium", "high")


def test_fault4_embedded_excerpt_still_flags_incident() -> None:
    """Same detection contract from the 12-sample embedded excerpt -- runs in
    CI with no data/tep download (excerpt rows are real TEP samples)."""
    engine = make_engine(data_dir=str(REPO_ROOT / "does" / "not" / "exist"))
    harness = DetectionHarness(engine)
    random.seed(1)
    for i in range(40):
        harness.tick("normal", i)
    engine._mode = "tep_fault_4"
    engine._fault_idx = 0
    for k in range(10):
        harness.tick("fault", k)
    flow = [a for a in harness.anomalies if a[0] == "fault" and a[2] == "flow_rate"]
    assert flow and any(a[4] is not None and a[4] > 0.05 for a in flow)


@pytest.mark.skipif(not REAL_CSVS_PRESENT, reason=MISSING_DATA_REASON)
def test_fault_window_starts_lead_in_before_real_onset() -> None:
    engine = make_engine()
    start = engine._fault_start_index()
    sample = int(float(engine._fault_rows[start]["sample"]))
    assert sample == FAULT_ONSET_SAMPLE - FAULT_LEAD_IN


# ---------------------------------------------------------------------------
# The prep script's conversion of the small_tep mirror (network-free)
# ---------------------------------------------------------------------------
def _write_tiny_mirror(directory: Path) -> Path:
    """A 2-run mirror with the same shape as small_tep: dataset.csv (columns
    trimmed to the ones the engine reads) + labels.csv with Fault 4 injected
    at sample 5 of the second run."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "dataset.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["run_id", "sample", "xmeas_9", "xmv_10"])
        for run, samples in (("r-free", range(1, 9)), ("r-f4", range(1, 13))):
            for s in samples:
                faulted = run == "r-f4" and s >= 5
                writer.writerow([run, s, "120.40", "45.00" if faulted else "41.12"])
    with (directory / "labels.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["run_id", "sample", "labels"])
        for run, samples in (("r-free", range(1, 9)), ("r-f4", range(1, 13))):
            for s in samples:
                writer.writerow([run, s, 4 if (run == "r-f4" and s >= 5) else 0])
    return directory


def test_prep_script_converts_small_tep_mirror(tmp_path, monkeypatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "download_tep_data",
        Path(__file__).resolve().parents[2] / "scripts" / "download_tep_data.py",
    )
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)  # type: ignore[union-attr]

    mirror = _write_tiny_mirror(tmp_path / "mirror")
    out_dir = tmp_path / "tep"
    monkeypatch.setattr(prep, "DATA_DIR", out_dir)
    assert prep.convert_small_tep(mirror) == 0
    free = list(csv.DictReader((out_dir / "fault_free_training.csv").open()))
    fault = list(csv.DictReader((out_dir / "fault_4_training.csv").open()))
    assert free and {r["run_id"] for r in free} == {"r-free"}
    assert fault and {r["run_id"] for r in fault} == {"r-f4"}
    # The window retains the onset (sample 5) and lead-in, with the `sample`
    # column intact so the engine can re-locate the onset.
    assert min(int(r["sample"]) for r in fault) == 1
    assert max(int(r["sample"]) for r in fault) == 12
    assert any(r["xmv_10"] == "45.00" for r in fault)


