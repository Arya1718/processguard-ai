"""Unit tests for the Risk Agent's HITL gating (Prompt 4).

The mapping must be DETERMINISTIC and loaded from config, and the safety
design must hold: ambiguity (low confidence) can only RAISE the gating
level, never lower it -- "high severity + low confidence still requires
human review" is an explicit, tested requirement.
"""
from __future__ import annotations

import pytest

from app.agents.risk.agent import hitl_level_for, load_hitl_thresholds, select_consequences


@pytest.fixture(scope="module")
def config() -> dict:
    return load_hitl_thresholds()


# ---------------------------------------------------------------------------
# Base table: severity -> level (high confidence path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("severity", "confidence", "expected"),
    [
        ("low", 95, 1),      # low severity, confident -> inform only
        ("medium", 90, 2),   # medium severity, confident -> recommend
        ("high", 95, 2),     # high severity, confident -> recommend
    ],
)
def test_base_mapping(config, severity, confidence, expected):
    assert hitl_level_for(severity, confidence, config) == expected


# ---------------------------------------------------------------------------
# Confidence floors: low confidence RAISES the level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("severity", "confidence", "expected"),
    [
        ("low", 45, 3),      # <50 confidence -> at least level 3, even for low severity
        ("medium", 45, 3),   # <50 confidence overrides the medium base
        ("medium", 60, 2),   # <70 confidence -> at least level 2
        ("high", 60, 2),     # high severity, moderate confidence -> still reviewed
        ("high", 80, 2),     # safety floor: high severity below 85 -> >= 2
    ],
)
def test_confidence_floors(config, severity, confidence, expected):
    assert hitl_level_for(severity, confidence, config) == expected


def test_high_severity_low_confidence_still_requires_review(config):
    """THE safety case: severity alone might suggest a lower tier, but low
    confidence must never let a serious anomaly bypass human review."""
    assert hitl_level_for("high", 40, config) == 3
    assert hitl_level_for("high", 30, config) == 3
    # And a high-severity anomaly never lands at level 1, whatever the
    # confidence: the base level plus the safety floor both push it up.
    for confidence in range(0, 101, 5):
        assert hitl_level_for("high", confidence, config) >= 2


def test_missing_confidence_defaults_to_most_cautious(config):
    """No confidence value = maximum ambiguity = the strictest gate."""
    assert hitl_level_for("low", None, config) == 3
    assert hitl_level_for("high", None, config) == 3


def test_mapping_is_deterministic(config):
    """Same inputs -> same level, every time (auditable gating)."""
    results = {hitl_level_for("high", 55, config) for _ in range(20)}
    assert results == {2}
    results = {hitl_level_for("medium", 45, config) for _ in range(20)}
    assert results == {3}


def test_config_rejects_missing_sections(tmp_path):
    """A corrupt/incomplete config must fail loudly, not fall back to code."""
    bad = tmp_path / "hitl.json"
    bad.write_text('{"severity_to_base_level": {"low": 1, "medium": 2, "high": 2}}')
    with pytest.raises(ValueError):
        load_hitl_thresholds(bad)


# ---------------------------------------------------------------------------
# Consequence selection: only the consequences that actually apply
# ---------------------------------------------------------------------------

def test_consequences_pump_degradation(config):
    summary = {"sensors": [{"sensor_type": "vibration", "reason": "outside normal band"}]}
    out = select_consequences("cooling-water pump degradation", summary, config)
    names = {c["consequence"] for c in out}
    assert "equipment damage" in names
    assert "chemical imbalance" not in names  # no chemistry signature


def test_consequences_chemistry_excursion(config):
    summary = {"sensors": [{"sensor_type": "ph", "reason": "outside normal band"},
                            {"sensor_type": "conductivity", "reason": "drifting"}]}
    out = select_consequences("basin chemistry excursion", summary, config)
    names = {c["consequence"] for c in out}
    assert "chemical imbalance" in names
    assert "equipment damage" not in names


def test_consequences_never_all_four_by_default(config):
    """An unrelated hypothesis with no keyword hits yields no fabricated
    consequences -- the list is evidence-driven, not a constant."""
    out = select_consequences("unknown transient", {"sensors": []}, config)
    assert out == []
