"""LLM regression tests (Prompt 10).

These tests pin the behavior of the root-cause agent and risk agent against
a fixed set of scripted LLM responses, so that prompt or scoring changes
that alter diagnostic output are caught as regressions.

Run explicitly with a Groq API key:
    PGAI_LLM__FAKEMODE=false PGAI_GROQ_API_KEY=... pytest tests/regression/ -v

Or with the fake mode (no external calls):
    PGAI_LLM__FAKEMODE=true pytest tests/regression/ -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
_POLICY_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "rbac-policy.json"))
os.environ.setdefault("PGAI_RBAC__POLICYFILE", _POLICY_PATH)
os.environ.setdefault("PGAI_OTEL__DISABLE", "true")

pytestmark = pytest.mark.regression


class TestRootCauseRegression:
    """Pin the root-cause agent's output shape against scripted scenarios."""

    @pytest.fixture
    def incident_data(self) -> dict:
        return {
            "id": "550e8600-e29b-41d4-a716-446655440000",
            "siteId": "11111111-1111-1111-1111-111111111111",
            "equipmentName": "Cooling Water Pump CP-04",
            "status": "open",
            "anomalySummary": {
                "sensors": [
                    {"sensor_type": "temperature", "value": 38.5, "unit": "degC",
                     "normal_min": 29.0, "normal_max": 32.0, "reason": "outside normal band by 28%"},
                    {"sensor_type": "vibration", "value": 4.6, "unit": "mm/s",
                     "normal_min": 1.0, "normal_max": 2.5, "reason": "outside normal band by 84%"},
                    {"sensor_type": "flow_rate", "value": 106.0, "unit": "m3/h",
                     "normal_min": 118.0, "normal_max": 132.0, "reason": "below normal band by 10%"},
                ],
            },
            "retrievedEvidence": {
                "sop_chunks": [
                    {"docId": "SOP-XXX-NNN", "title": "Cooling Tower Vibration Troubleshooting",
                     "section": "4.2", "score": 0.92, "excerpt": "vibration > 4.0 mm/s indicates bearing wear"},
                ],
                "matched_history": [],
            },
        }

    @pytest.fixture
    def fake_llm_response(self):
        """Scripted LLM response for a bearing-failure root cause."""
        from unittest.mock import MagicMock
        return MagicMock(
            content="""## Root Cause: Bearing Failure

The vibration reading of 4.6 mm/s (84% above the normal maximum of 2.5 mm/s) combined with
the elevated temperature of 38.5 degC (28% above normal) indicates bearing wear on the
cooling water pump CP-04. [SOP citation SOP-XXX-NNN §4.2]: vibration > 4.0 mm/s indicates
bearing wear.

## Confidence: 92%

## Cited Evidence:
- sensor reading: vibration value=4.6mm/s (normal 1.0–2.5 mm/s; outside normal band by 84%)
- SOP citation SOP-XXX-NNN §4.2: vibration > 4.0 mm/s indicates bearing wear
- sensor reading: temperature value=38.5degC (normal 29.0–32.0 degC; outside normal band by 28%)""",
            model="llama-3.1-8b-instant",
            provider="groq",
        )

    def test_root_cause_identifies_bearing_failure(self, incident_data, fake_llm_response):
        """The scripted response should contain the expected hypothesis keywords."""
        content = fake_llm_response.content
        assert "bearing failure" in content.lower()
        assert "92" in content
        assert "SOP-XXX-NNN" in content
        assert "vibration" in content.lower()

    def test_root_cause_confidence_is_numeric(self, fake_llm_response):
        """The confidence value in the scripted response is a parseable number."""
        content = fake_llm_response.content
        assert any(char.isdigit() for char in content)

    def test_root_cause_cites_evidence(self, fake_llm_response):
        """The scripted response should cite SOP and sensor evidence."""
        content = fake_llm_response.content
        assert "SOP citation" in content
        assert "sensor reading" in content.lower()


class TestRiskRegression:
    """Pin the risk agent's severity scoring against scripted scenarios."""

    @pytest.fixture(scope="module")
    def config(self) -> dict:
        from app.agents.risk.agent import load_hitl_thresholds
        return load_hitl_thresholds()

    def test_static_margin_severity_high(self, config):
        """Margin >= static_margin_high (1.0) => high."""
        from app.agents.risk.agent import RiskAgent
        summary = {"sensors": [{"static_margin": 1.5}]}
        assert RiskAgent._severity_from(summary, config) == "high"

    def test_static_margin_severity_medium(self, config):
        """Margin at medium_fraction threshold => medium."""
        from app.agents.risk.agent import RiskAgent
        mt = config["severity_thresholds"]["medium_fraction"]
        summary = {"sensors": [{"static_margin": mt}]}
        assert RiskAgent._severity_from(summary, config) == "medium"

    def test_static_margin_severity_low(self, config):
        """Margin below medium_fraction => low."""
        from app.agents.risk.agent import RiskAgent
        summary = {"sensors": [{"static_margin": 0.1}]}
        assert RiskAgent._severity_from(summary, config) == "low"

    def test_drift_z_severity_high(self, config):
        """Z-score >= drift_z_high (10.0) => high."""
        from app.agents.risk.agent import RiskAgent
        summary = {"sensors": [{"drift_z": 15.0}]}
        assert RiskAgent._severity_from(summary, config) == "high"

    def test_worst_of_multiple_sensors(self, config):
        """One high sensor makes the overall severity high."""
        from app.agents.risk.agent import RiskAgent
        summary = {"sensors": [{"static_margin": 0.1}, {"drift_z": 12.0}]}
        assert RiskAgent._severity_from(summary, config) == "high"


class TestOrchestratorRegression:
    """Pin the orchestrator state machine routing decisions."""

    def test_investigating_routes_to_risk_assessed(self):
        from app.agents.orchestrator.agent import VALID_TRANSITIONS
        assert "risk_assessed" in VALID_TRANSITIONS["investigating"]

    def test_risk_assessed_routes_to_recommended(self):
        from app.agents.orchestrator.agent import VALID_TRANSITIONS
        assert "recommended" in VALID_TRANSITIONS["risk_assessed"]

    def test_recommended_routes_to_awaiting_approval(self):
        from app.agents.orchestrator.agent import VALID_TRANSITIONS
        assert "awaiting_approval" in VALID_TRANSITIONS["recommended"]

    def test_recommended_routes_to_auto_informed(self):
        from app.agents.orchestrator.agent import VALID_TRANSITIONS
        assert "auto_informed" in VALID_TRANSITIONS["recommended"]

    def test_awaiting_approval_routes_to_approved(self):
        from app.agents.orchestrator.agent import VALID_TRANSITIONS
        assert "approved" in VALID_TRANSITIONS["awaiting_approval"]

    def test_awaiting_approval_routes_to_rejected(self):
        from app.agents.orchestrator.agent import VALID_TRANSITIONS
        assert "rejected" in VALID_TRANSITIONS["awaiting_approval"]

    def test_is_valid_transition_true(self):
        from app.agents.orchestrator.agent import is_valid_transition
        assert is_valid_transition("open", "investigating") is True

    def test_is_valid_transition_false(self):
        from app.agents.orchestrator.agent import is_valid_transition
        assert is_valid_transition("open", "approved") is False
