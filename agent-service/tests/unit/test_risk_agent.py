"""Unit tests for RiskAgent severity scoring and the full handle() method.

Fills the coverage gap for _severity_from and the async handle() method
which weren't fully covered by test_risk_hitl.py (which only tests the
pure helper functions).
"""
from __future__ import annotations

import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.risk.agent import (
    RiskAgent,
    load_hitl_thresholds,
    hitl_level_for,
)
from app.core.db import get_incident_diagnosis_state, save_risk_assessment


@pytest.fixture(scope="module")
def config() -> dict:
    return load_hitl_thresholds()


class TestSeverityScoring:
    def test_no_sensors_is_low(self, config):
        assert RiskAgent._severity_from({}, config) == "low"

    def test_high_static_margin_is_high(self, config):
        summary = {"sensors": [{"static_margin": 1.5}]}  # >= 1.0
        assert RiskAgent._severity_from(summary, config) == "high"

    def test_high_drift_z_is_high(self, config):
        summary = {"sensors": [{"drift_z": 15.0}]}  # well above thresholds["drift_z_high"]=10
        assert RiskAgent._severity_from(summary, config) == "high"

    def test_medium_static_margin_is_medium(self, config):
        thresholds = config["severity_thresholds"]
        margin = thresholds["static_margin_high"] * thresholds["medium_fraction"]
        summary = {"sensors": [{"static_margin": margin}]}
        assert RiskAgent._severity_from(summary, config) == "medium"

    def test_low_static_margin_is_low(self, config):
        summary = {"sensors": [{"static_margin": 0.1}]}
        assert RiskAgent._severity_from(summary, config) == "low"

    def test_worst_of_multiple_sensors(self, config):
        """If one sensor is high and another is low, severity is high."""
        summary = {"sensors": [
            {"static_margin": 0.1},   # low
            {"drift_z": 10.0},        # high
        ]}
        assert RiskAgent._severity_from(summary, config) == "high"


class TestRiskAgentHandle:
    """Tests the async handle() method with a mocked DB layer."""

    @pytest.mark.asyncio
    async def test_handle_scores_and_persists(self, config, monkeypatch):
        """handle() reads diagnosis state, scores, persists, and returns result."""
        from unittest.mock import AsyncMock

        async def mock_get_state(incident_id):
            return {
                "anomaly_summary": {
                    "sensors": [
                        {"sensor_type": "flow_rate", "static_margin": 1.2,
                         "reason": "outside normal band by 88%"},
                    ],
                },
                "root_cause_hypothesis": "cooling-water pump degradation with a blocked suction strainer",
                "confidence": 87,
            }

        async def mock_save_risk(incident_id, severity, consequences, hitl_level, required_approval_level):
            return True

        monkeypatch.setattr("app.agents.risk.agent.get_incident_diagnosis_state", mock_get_state)
        monkeypatch.setattr("app.agents.risk.agent.save_risk_assessment", mock_save_risk)

        agent = RiskAgent(config=config)
        result = await agent.handle({"incident_id": "inc-1"})

        assert result["incident_id"] == "inc-1"
        assert result["severity"] == "high"
        assert result["hitl_level"] >= 2
        assert len(result["consequences"]) > 0
        # The consequences should include equipment damage (vibration/pump keyword)
        consequence_names = {c["consequence"] for c in result["consequences"]}
        assert "equipment damage" in consequence_names

    @pytest.mark.asyncio
    async def test_handle_raises_when_incident_missing(self, config, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("app.agents.risk.agent.get_incident_diagnosis_state", AsyncMock(return_value=None))

        agent = RiskAgent(config=config)
        with pytest.raises(RuntimeError, match="not found for risk scoring"):
            await agent.handle({"incident_id": "inc-missing"})

    @pytest.mark.asyncio
    async def test_handle_uses_payload_summary_fallback(self, config, monkeypatch):
        """If the DB state has no anomaly_summary, the payload summary is used."""
        from unittest.mock import AsyncMock

        async def mock_get_state(incident_id):
            return {"anomaly_summary": None, "root_cause_hypothesis": None, "confidence": None}

        async def mock_save_risk(*a, **kw):
            return True

        monkeypatch.setattr("app.agents.risk.agent.get_incident_diagnosis_state", mock_get_state)
        monkeypatch.setattr("app.agents.risk.agent.save_risk_assessment", mock_save_risk)

        agent = RiskAgent(config=config)
        summary = {"sensors": [{"static_margin": 0.5, "reason": "flow drop"}]}
        result = await agent.handle({"incident_id": "inc-2", "summary": summary})

        assert result["incident_id"] == "inc-2"
        # With low confidence (None -> defaults to strictest) and low severity,
        # the level should still be >= 2 due to confidence floor.
        assert result["hitl_level"] >= 2


class TestSeverityBoundaryConditions:
    def test_boundary_at_high_threshold(self, config):
        """Exactly at the static_margin_high threshold should be 'high'."""
        threshold = config["severity_thresholds"]["static_margin_high"]
        summary = {"sensors": [{"static_margin": threshold}]}
        assert RiskAgent._severity_from(summary, config) == "high"

    def test_boundary_just_below_high(self, config):
        """Just below the high threshold but above medium fraction is 'medium'."""
        high = config["severity_thresholds"]["static_margin_high"]
        medium = high * config["severity_thresholds"]["medium_fraction"]
        summary = {"sensors": [{"static_margin": medium + 0.01}]}
        result = RiskAgent._severity_from(summary, config)
        assert result == "medium"
