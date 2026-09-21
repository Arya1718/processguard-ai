"""Anomaly scoring for the Detection Agent (pure logic, no I/O).

Two independent checks per reading (see docs/ingestion-pipeline.md):

1. static check   -- outside the sensor's configured [normal_min, normal_max]
                     band, beyond an allowable fraction of the band (static_z).
2. baseline check -- the sensor's own recent rolling average (computed over a
                     short window in Redis) vs this reading: a fast deviation
                     from its own baseline (drift_z in robust sigma units).

A reading is ANOMALOUS when either check fires. Correlation across sensors is
handled separately by `correlate_anomalies` so that 5 drifting sensors produce
ONE incident, not five.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class Reading:
    sensor_id: str
    equipment_id: str
    sensor_type: str
    value: float
    unit: str
    occurred_at: datetime


@dataclass(frozen=True)
class BaselineStats:
    mean: float
    sigma: float  # robust standard deviation (clamped to a floor)


@dataclass(frozen=True)
class SensorThresholds:
    normal_min: float
    normal_max: float


@dataclass(frozen=True)
class ScoredReading:
    reading: Reading
    anomalous: bool
    static_anomaly: bool
    drift_anomaly: bool
    static_margin: float | None  # how far outside the band, as fraction of band
    drift_z: float | None        # how many sigmas from the recent baseline
    reason: str


def compute_baseline(values: list[float]) -> BaselineStats:
    """Rolling baseline over the last N readings for one sensor.

    Uses mean + (robust) standard deviation. Sigma is clamped to at least
    1e-3 * max(1, |mean|) so a perfectly flat sensor can never produce an
    infinite z-score from a single jittery reading.
    """
    if not values:
        return BaselineStats(mean=0.0, sigma=0.0)
    mean = statistics.fmean(values)
    if len(values) >= 2:
        sigma = statistics.pstdev(values)
    else:
        sigma = 0.0
    floor = 1e-3 * max(1.0, abs(mean))
    return BaselineStats(mean=mean, sigma=max(sigma, floor))


def score_reading(
    reading: Reading,
    thresholds: SensorThresholds,
    baseline: BaselineStats,
    static_z: float,
    drift_z: float,
) -> ScoredReading:
    """Classify one reading against static band + rolling baseline.

    static_z: allowed overshoot past the band, as a fraction of band width.
              0.0 means any excursion is anomalous; 0.1 tolerates 10% beyond.
    drift_z:  z-score threshold vs the sensor's own recent baseline.
    """
    band = thresholds.normal_max - thresholds.normal_min
    if band <= 0:
        band = max(abs(thresholds.normal_min), abs(thresholds.normal_max), 1.0)

    static_margin: float | None = None
    static_anomaly = False
    overshoot = 0.0
    if reading.value < thresholds.normal_min:
        overshoot = thresholds.normal_min - reading.value
    elif reading.value > thresholds.normal_max:
        overshoot = reading.value - thresholds.normal_max
    if overshoot > 0:
        static_margin = overshoot / band
        static_anomaly = static_margin > static_z

    drift_z_score: float | None = None
    drift_anomaly = False
    if baseline.sigma > 0:
        drift_z_score = abs(reading.value - baseline.mean) / baseline.sigma
        drift_anomaly = drift_z_score > drift_z

    anomalous = static_anomaly or drift_anomaly
    reasons = []
    if static_anomaly:
        reasons.append(f"outside normal band by {static_margin:.1%}")
    if drift_anomaly:
        reasons.append(f"drifting from own baseline ({drift_z_score:.1f} sigma)")

    return ScoredReading(
        reading=reading,
        anomalous=anomalous,
        static_anomaly=static_anomaly,
        drift_anomaly=drift_anomaly,
        static_margin=static_margin,
        drift_z=drift_z_score,
        reason="; ".join(reasons),
    )


# ---------------------------------------------------------------------------
# Correlated incident aggregation
# ---------------------------------------------------------------------------

@dataclass
class AnomalyObservation:
    """One anomalous reading, kept in a short-term correlation buffer."""
    equipment_id: str
    sensor_id: str
    sensor_type: str
    observed_at: datetime
    value: float
    unit: str
    reason: str
    # Numeric scores carried alongside the human-readable reason so severity
    # computation never has to parse text back.
    static_margin: float | None = None  # overshoot as fraction of band width
    drift_z: float | None = None        # z-score vs the sensor's own baseline


@dataclass
class CorrelatedAnomaly:
    equipment_id: str
    sensors: list[AnomalyObservation] = field(default_factory=list)

    @property
    def sensor_ids(self) -> list[str]:
        return [o.sensor_id for o in self.sensors]

    @property
    def last_observed_at(self) -> datetime:
        return max(o.observed_at for o in self.sensors)


def correlate_anomalies(
    observations: list[AnomalyObservation],
    correlation_window_seconds: float,
    max_gap_seconds: float,
    now: datetime | None = None,
) -> list[CorrelatedAnomaly]:
    """Group recent anomalous observations into correlated incidents.

    Two anomalous readings belong to the same incident when they hit the same
    equipment and their timestamps are within `correlation_window_seconds` of
    each other (chain-linked: A~B and B~C group even if A~C is wider), AND the
    newest observation is no older than `max_gap_seconds` (staleness cutoff so
    an old half of a chain cannot persist forever).

    Returns one CorrelatedAnomaly per equipment that still has fresh anomalies.
    """
    current = now or datetime.now(timezone.utc)
    by_equipment: dict[str, list[AnomalyObservation]] = {}
    for obs in observations:
        by_equipment.setdefault(obs.equipment_id, []).append(obs)

    incidents: list[CorrelatedAnomaly] = []
    window_seconds = correlation_window_seconds
    for equipment_id, obs_list in by_equipment.items():
        obs_list = sorted(obs_list, key=lambda o: o.observed_at)
        clusters: list[list[AnomalyObservation]] = []
        for obs in obs_list:
            if clusters and (obs.observed_at - clusters[-1][-1].observed_at).total_seconds() <= window_seconds:
                clusters[-1].append(obs)
            else:
                clusters.append([obs])
        # Only the freshest cluster can still be "active"; older clusters are
        # either already turned into incidents or expired.
        freshest = clusters[-1]
        age = (current - freshest[-1].observed_at).total_seconds()
        if age <= max_gap_seconds:
            incidents.append(CorrelatedAnomaly(equipment_id=equipment_id, sensors=freshest))
    return incidents


def severity_from_score(margins: list[float], drift_scores: list[float]) -> str:
    """Map how far/fast sensors deviated to a severity label.

    severity = max over sensors of (static overshoot fraction / 0.5) and
    (drift z / 10), i.e. a sensor 50% outside its band or ~10 sigmas off its
    baseline is "high"; 15%+ or ~3 sigmas is at least "medium".
    """
    worst = 0.0
    for m in margins:
        if m is not None:
            worst = max(worst, m / 0.5)
    for z in drift_scores:
        if z is not None:
            worst = max(worst, z / 10.0)
    if worst >= 1.0:
        return "high"
    if worst >= 0.3:
        return "medium"
    return "low"
