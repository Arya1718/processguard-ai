"""Unit tests for app/api/incidents.py.

Uses httpx.AsyncClient + ASGITransport (same pattern as test_health_and_bus.py)
to avoid the Starlette 0.27 / httpx 0.28 TestClient incompatibility.

DB functions are monkeypatched at the app.api.incidents namespace (where the
route module imported them directly), so no real Postgres is needed.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
_POLICY_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "rbac-policy.json"))
os.environ.setdefault("PGAI_RBAC__POLICYFILE", _POLICY_PATH)
os.environ.setdefault("PGAI_OTEL__DISABLE", "true")

from app.main import app
from app.core.security import Principal, require_principal

TEST_SITE = "site-A"
TEST_USERNAME = "test_operator"
TEST_TOKEN = "test-token"
TEST_MANAGER = "test_manager"


def _make_principal() -> Principal:
    return Principal(
        token=TEST_TOKEN,
        payload={
            "site_id": TEST_SITE,
            "preferred_username": TEST_USERNAME,
            "roles": ["MaintenanceEngineer"],
        },
    )


def _make_manager() -> Principal:
    return Principal(
        token=TEST_TOKEN,
        payload={
            "site_id": TEST_SITE,
            "preferred_username": TEST_MANAGER,
            "roles": ["PlantManager"],
        },
    )


@pytest.fixture
@pytest.mark.asyncio
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _patch_auth():
    def _fake_require_principal():
        return _make_principal()
    app.dependency_overrides[require_principal] = _fake_require_principal
    yield
    app.dependency_overrides.pop(require_principal, None)


# ---------------------------------------------------------------------------
# /incidents (list)
# ---------------------------------------------------------------------------

class TestIncidentsList:
    @pytest.fixture(autouse=True)
    def _patch_list(self, monkeypatch):
        async def _fake_list(status, site_id, limit, offset):
            return [{
                "id": "inc-1",
                "siteId": site_id,
                "equipmentId": "eq-1",
                "equipmentName": "Pump A",
                "status": "open",
                "severity": "high",
                "detectedAt": "2026-01-01T00:00:00Z",
            }], 1
        monkeypatch.setattr("app.api.incidents.list_incidents", _fake_list)

    async def test_list_incidents(self, client):
        resp = await client.get("/api/v1/incidents")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == "inc-1"
        assert data["total"] == 1
        assert data["limit"] == 50
        assert data["offset"] == 0

    async def test_list_incidents_with_filters(self, client):
        resp = await client.get("/api/v1/incidents?status=open&limit=10&offset=5")
        assert resp.status_code == 200
        assert resp.json()["items"][0]["status"] == "open"

    async def test_list_incidents_invalid_status(self, client):
        resp = await client.get("/api/v1/incidents?status=invalid_status")
        assert resp.status_code == 422

    async def test_list_incidents_cross_site(self, client):
        resp = await client.get("/api/v1/incidents?site_id=other-site")
        assert resp.status_code == 403
        assert "outside your assigned site" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /incidents/{id} (detail)
# ---------------------------------------------------------------------------

class TestIncidentDetail:
    @pytest.fixture(autouse=True)
    def _patch_get(self, monkeypatch):
        async def _fake_get(_incident_id):
            return {
                "id": "inc-1", "siteId": TEST_SITE, "equipmentId": "eq-1",
                "equipmentName": "Pump A", "status": "open", "severity": "high",
                "detectedAt": "2026-01-01T00:00:00Z",
                "anomalySummary": {"sensors": [{"sensor_type": "vibration", "value": 8.5}]},
                "rootCause": {"hypothesis": "bearing failure", "confidence": 90, "citedEvidence": []},
                "risk": {"severity": "high", "hitlLevel": 2, "consequences": []},
                "recommendation": {"actions": [{"action": "inspect"}], "rationale": "bearing noise"},
            }
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)

    async def test_get_incident_detail(self, client):
        resp = await client.get("/api/v1/incidents/inc-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "inc-1"
        assert data["siteId"] == TEST_SITE
        assert data["rootCause"]["hypothesis"] == "bearing failure"
        assert data["risk"]["hitlLevel"] == 2

    async def test_get_incident_not_found(self, client, monkeypatch):
        monkeypatch.setattr("app.api.incidents.get_incident", AsyncMock(return_value=None))
        resp = await client.get("/api/v1/incidents/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "incident not found"

    async def test_get_incident_cross_site(self, client, monkeypatch):
        async def _fake_get(_ii):
            return {"id": "inc-1", "siteId": "other-site", "status": "open"}
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)
        resp = await client.get("/api/v1/incidents/inc-1")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /incidents/{id}/evidence
# ---------------------------------------------------------------------------

class TestIncidentEvidence:
    @pytest.fixture(autouse=True)
    def _patch_get(self, monkeypatch):
        async def _fake_get(_ii):
            return {
                "id": "inc-1", "siteId": TEST_SITE, "status": "open",
                "retrievedEvidence": {
                    "collected_at": "2026-01-01T00:00:00Z",
                    "sop_chunks": [{"title": "SOP-1", "content": "check temperature"}],
                },
            }
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)

    async def test_get_evidence(self, client):
        resp = await client.get("/api/v1/incidents/inc-1/evidence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["incidentId"] == "inc-1"
        assert data["collectedAt"] == "2026-01-01T00:00:00Z"
        assert "sop_chunks" in data["evidence"]


# ---------------------------------------------------------------------------
# /incidents/{id}/root-cause
# ---------------------------------------------------------------------------

class TestIncidentRootCause:
    @pytest.fixture(autouse=True)
    def _patch_get(self, monkeypatch):
        async def _fake_get(_ii):
            return {
                "id": "inc-1", "siteId": TEST_SITE, "status": "risk_assessed",
                "rootCause": {
                    "hypothesis": "bearing failure", "confidence": 92,
                    "citedEvidence": [{"chunk_id": "SOP-1", "excerpt": "noise"}],
                },
            }
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)

    async def test_get_root_cause(self, client):
        resp = await client.get("/api/v1/incidents/inc-1/root-cause")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hypothesis"] == "bearing failure"
        assert data["confidence"] == 92
        assert len(data["citedEvidence"]) == 1

    async def test_get_root_cause_not_yet_assessed(self, client, monkeypatch):
        async def _fake_get(_ii):
            return {"id": "inc-1", "siteId": TEST_SITE, "status": "open", "rootCause": None}
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)
        resp = await client.get("/api/v1/incidents/inc-1/root-cause")
        assert resp.status_code == 200
        assert resp.json()["hypothesis"] is None


# ---------------------------------------------------------------------------
# /incidents/{id}/risk
# ---------------------------------------------------------------------------

class TestIncidentRisk:
    @pytest.fixture(autouse=True)
    def _patch_get(self, monkeypatch):
        async def _fake_get(_ii):
            return {
                "id": "inc-1", "siteId": TEST_SITE, "status": "risk_assessed",
                "risk": {"severity": "high", "consequences": ["equipment damage"], "hitlLevel": 3, "assessedAt": "2026-01-01T00:00:00Z"},
                "requiredApprovalLevel": 3,
            }
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)

    async def test_get_risk(self, client):
        resp = await client.get("/api/v1/incidents/inc-1/risk")
        assert resp.status_code == 200
        data = resp.json()
        assert data["severity"] == "high"
        assert data["hitlLevel"] == 3
        assert data["requiredApprovalLevel"] == 3


# ---------------------------------------------------------------------------
# /incidents/{id}/recommendation
# ---------------------------------------------------------------------------

class TestIncidentRecommendation:
    @pytest.fixture(autouse=True)
    def _patch_get(self, monkeypatch):
        async def _fake_get(_ii):
            return {
                "id": "inc-1", "siteId": TEST_SITE, "status": "recommended",
                "recommendation": {"actions": [{"action": "inspect_bearing"}], "rationale": "bearing noise", "toolCallLog": [{"tool": "sensor_query"}]},
                "risk": {"hitlLevel": 2, "severity": "high"},
            }
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)

    async def test_get_recommendation(self, client):
        resp = await client.get("/api/v1/incidents/inc-1/recommendation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "recommended"
        assert len(data["actions"]) == 1
        assert data["rationale"] == "bearing noise"


# ---------------------------------------------------------------------------
# /incidents/{id}/state-history
# ---------------------------------------------------------------------------

class TestStateHistory:
    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch):
        async def _fake_get(_ii):
            return {"id": "inc-1", "siteId": TEST_SITE, "status": "approved"}
        async def _fake_history(_ii):
            return [
                {"from": "open", "to": "investigating", "ts": "2026-01-01T00:00:00Z"},
                {"from": "investigating", "to": "risk_assessed", "ts": "2026-01-01T00:01:00Z"},
            ]
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)
        monkeypatch.setattr("app.api.incidents.get_incident_state_history", _fake_history)

    async def test_get_state_history(self, client):
        resp = await client.get("/api/v1/incidents/inc-1/state-history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["incidentId"] == "inc-1"
        assert data["currentStatus"] == "approved"
        assert len(data["transitions"]) == 2


# ---------------------------------------------------------------------------
# /incidents/{id}/approve + /reject (HITL)
# ---------------------------------------------------------------------------

class TestHitlDecisions:
    @pytest.fixture
    def _patch_decision(self, monkeypatch):
        """Patch decision-related DB calls for all HITL tests."""
        import app.api.incidents as _inc

        async def _fake_record(_ii, _decision, _by, _level, _justification):
            return datetime.now(timezone.utc)

        # Store call count to switch return values
        state = {"call_count": 0, "level": 2}

        async def _smart_view(_ii):
            state["call_count"] += 1
            if state["call_count"] == 1:
                return {
                    "site_id": TEST_SITE, "status": "awaiting_approval",
                    "hitl_level": state["level"], "required_approval_level": state["level"],
                    "recommended_actions": [], "rationale": "test", "tool_call_log": None,
                    "risk_severity": "high", "root_cause_hypothesis": "bearing",
                    "confidence": 90, "anomaly_summary": None,
                }
            return {
                "site_id": TEST_SITE, "status": "approved",
                "hitl_level": state["level"], "required_approval_level": state["level"],
                "recommended_actions": [], "rationale": "test", "tool_call_log": None,
                "risk_severity": "high", "root_cause_hypothesis": "bearing",
                "confidence": 90, "anomaly_summary": None,
            }

        monkeypatch.setattr(_inc, "get_incident_for_decision", _smart_view)
        monkeypatch.setattr(_inc, "record_approval", _fake_record)
        monkeypatch.setattr("app.api.incidents.get_event_bus", lambda: None)
        return state

    async def test_approve_incident(self, client, _patch_decision):
        resp = await client.post("/api/v1/incidents/inc-1/approve", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "approved"
        assert data["decidedBy"] == TEST_USERNAME
        assert data["status"] == "approved"

    async def test_reject_incident_with_justification(self, client, _patch_decision):
        resp = await client.post(
            "/api/v1/incidents/inc-1/reject",
            json={"justification": "inspection showed no fault"},
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "rejected"

    async def test_reject_at_level3_without_justification(self, client, _patch_decision):
        _patch_decision["level"] = 3
        # Override principal to PlantManager (max_approval_level=3)
        app.dependency_overrides[require_principal] = lambda: _make_manager()
        resp = await client.post("/api/v1/incidents/inc-1/reject", json={})
        app.dependency_overrides[require_principal] = lambda: _make_principal()
        assert resp.status_code == 422
        assert "justification is required" in resp.json()["detail"]

    async def test_decision_wrong_status(self, client, monkeypatch):
        import app.api.incidents as _inc

        async def _fake_view_open(_ii):
            return {
                "site_id": TEST_SITE, "status": "open",
                "hitl_level": 2, "required_approval_level": 2,
                "recommended_actions": [], "rationale": "test", "tool_call_log": None,
                "risk_severity": "high", "root_cause_hypothesis": "bearing",
                "confidence": 90, "anomaly_summary": None,
            }
        monkeypatch.setattr(_inc, "get_incident_for_decision", _fake_view_open)
        resp = await client.post("/api/v1/incidents/inc-1/approve", json={})
        assert resp.status_code == 409
        assert "awaiting_approval" in resp.json()["detail"]

    async def test_decision_not_found(self, client, monkeypatch):
        import app.api.incidents as _inc
        monkeypatch.setattr(_inc, "get_incident_for_decision", AsyncMock(return_value=None))
        resp = await client.post("/api/v1/incidents/inc-1/approve", json={})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /incidents/{id}/action
# ---------------------------------------------------------------------------

class TestIncidentAction:
    @pytest.fixture(autouse=True)
    def _patch_action(self, monkeypatch):
        async def _fake_record(_ii):
            return {
                "incidentId": "inc-1", "siteId": TEST_SITE,
                "actions": [{"action": "work_order", "result": "success"}],
                "status": "action_taken",
            }
        monkeypatch.setattr("app.api.incidents.get_incident_action_record", _fake_record)

    async def test_get_action(self, client):
        resp = await client.get("/api/v1/incidents/inc-1/action")
        assert resp.status_code == 200
        data = resp.json()
        assert data["incidentId"] == "inc-1"
        assert data["status"] == "action_taken"

    async def test_get_action_not_found(self, client, monkeypatch):
        monkeypatch.setattr("app.api.incidents.get_incident_action_record", AsyncMock(return_value=None))
        resp = await client.get("/api/v1/incidents/inc-1/action")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /incidents/{id}/resolve
# ---------------------------------------------------------------------------

class TestResolveIncident:
    @pytest.fixture(autouse=True)
    def _patch_resolve(self, monkeypatch):
        async def _fake_get(_ii):
            return {"id": "inc-1", "siteId": TEST_SITE, "status": "approved"}
        async def _fake_resolve(_ii, _by, _note):
            return {"status": "resolved", "resolvedAt": "2026-01-01T00:00:00Z"}
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)
        monkeypatch.setattr("app.api.incidents.resolve_incident_manually", _fake_resolve)

    async def test_resolve_incident(self, client):
        resp = await client.post("/api/v1/incidents/inc-1/resolve", json={"note": "fixed"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["incidentId"] == "inc-1"
        assert data["status"] == "resolved"

    async def test_resolve_already_resolved(self, client, monkeypatch):
        async def _fake_get(_ii):
            return {"id": "inc-1", "siteId": TEST_SITE, "status": "resolved"}
        monkeypatch.setattr("app.api.incidents.get_incident", _fake_get)
        resp = await client.post("/api/v1/incidents/inc-1/resolve", json={})
        assert resp.status_code == 409
        assert "already resolved" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /sites/{site_id}/sensors/latest
# ---------------------------------------------------------------------------

class TestSensorReadings:
    @pytest.fixture(autouse=True)
    def _patch_readings(self, monkeypatch):
        async def _fake_readings(_site):
            return [{"sensorId": "s1", "sensorType": "temperature", "value": 72.5}]
        monkeypatch.setattr("app.api.incidents.latest_readings_for_site", _fake_readings)

    async def test_get_sensors(self, client):
        resp = await client.get("/api/v1/sites/site-A/sensors/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["site_id"] == "site-A"
        assert len(data["sensors"]) == 1

    async def test_get_sensors_cross_site(self, client):
        resp = await client.get("/api/v1/sites/other-site/sensors/latest")
        assert resp.status_code == 403
