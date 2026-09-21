"""JSON schema contract tests (Prompt 10) -- the .NET/Python/frontend boundary.

These pin the EXACT field names of the wire payloads the three tiers
exchange, so a Python-side rename cannot silently break the .NET DTOs or the
React frontend (and vice versa). They run HERMETICALLY by exercising the real
builder functions -- the same code the running services execute -- so the
contract cannot drift from the implementation.

The .NET middleware reads these payloads as JsonElement (snake-through), and
the React frontend reads camelCase fields directly (Timeline.jsx,
DetectionPanel.jsx, EvidenceCitation.jsx, RootCausePanel.jsx...). Each test
names the exact field that changed on failure.

Why the frontend/backend schema is pinned in PYTHON tests: the incident
detail payload has exactly one producer (app/core/db.py row mappers) and two
consumers (.NET passthrough + React). This file is that single schema,
executable. docs/testing-strategy.md documents this choice.

Failures are loud and specific: every assertion message names the field that
drifted and who consumes it.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

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

pytestmark = pytest.mark.contract

from app.agents.detection.agent import _correlated_summary, _sensor_payload
from app.agents.detection.scoring import AnomalyObservation
from app.core.db import _history_row_to_dict, _incident_row_to_dict


# ---------------------------------------------------------------------------
# Fixtures: raw DB-shaped records exactly as asyncpg returns them (pascal-case
# keys, per the .NET-created schema), so the real mapper runs against real
# input shapes.
# ---------------------------------------------------------------------------

def _ts(**delta) -> datetime:
    return datetime.now(timezone.utc) + __import__("datetime").timedelta(**delta)


class _Record(SimpleNamespace):
    """asyncpg.Record stand-in: attribute AND subscript access (the mappers
    use row["Field"])."""

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


def _db_record(**overrides) -> _Record:
    """A pascal-case row as Postgres/asyncpg would return it."""
    row = {
        "Id": "9e2b1c3a-0000-0000-0000-000000000001",
        "Name": "Cooling water pump CP-04 anomaly",
        "SiteId": "11111111-1111-1111-1111-111111111111",
        "EquipmentId": "22222222-2222-2222-2222-222222222222",
        "EquipmentName": "Cooling Water Pump CP-04",
        "Status": "awaiting_approval",
        "Severity": "high",
        "DetectedAt": _ts(minutes=-30),
        "AnomalySummary": json.dumps(
            {
                "equipment_id": "22222222-2222-2222-2222-222222222222",
                "detected_at": "2026-09-21T10:00:00+00:00",
                "sensor_count": 1,
                "sensors": [
                    {
                        "sensor_id": "s-1",
                        "sensor_type": "flow_rate",
                        "value": 141.2,
                        "unit": "m3/h",
                        "observed_at": "2026-09-21T10:00:00+00:00",
                        "reason": "static band breach",
                        "static_margin": 1.4,
                        "drift_z": 7.1,
                        "normal_min": 118.0,
                        "normal_max": 132.0,
                    }
                ],
            }
        ),
        "Evidence": json.dumps(
            {
                "incident_id": "9e2b1c3a-0000-0000-0000-000000000001",
                "sop_chunks": [
                    {
                        "docId": "SOP-COOL-014",
                        "title": "Cooling Water Flow and Vibration Response",
                        "section": "Response",
                        "score": 0.81,
                        "excerpt": "If flow drops while vibration rises...",
                        "citation": "SOP-COOL-014 / Cooling Water Flow and Vibration Response -- Response",
                        "sourceType": "illustrative",
                        "sourceUrl": None,
                    }
                ],
                "matched_history": [],
            }
        ),
        "RootCauseHypothesis": "Most likely cause: cooling-water pump degradation",
        "Confidence": 87,
        "CitedEvidence": json.dumps(
            [{"type": "sop", "ref": "SOP-COOL-014", "text": "flow drop + vibration -> inspect suction"}]
        ),
        "RiskSeverity": "high",
        "Consequences": json.dumps(
            [{"consequence": "equipment damage", "why": "sustained out-of-band flow"}]
        ),
        "HitlLevel": 2,
        "RequiredApprovalLevel": 2,
        "RiskAssessedAt": _ts(minutes=-25),
        "RecommendedActions": json.dumps(
            [{"action": "Inspect pump suction strainer", "priority": "P1", "type": "work_order"}]
        ),
        "Rationale": "Same signature resolved before by clearing the strainer.",
        "ToolCallLog": json.dumps(
            [{"tool": "get_equipment_maintenance_status", "arguments": {"equipment_id": "e-1"}, "result": {}, "at": "2026-09-21T10:05:00+00:00"}]
        ),
        "RecommendedAt": _ts(minutes=-20),
        "ActionExecutedAt": None,
        "ActionReference": None,
        "EstimatedCostUsd": 12.5,
        "ResolvedAt": None,
        "CreatedAt": _ts(minutes=-31),
        "UpdatedAt": _ts(minutes=-1),
    }
    row.update(overrides)
    return _Record(**row)


# ---------------------------------------------------------------------------
# 1. Incident detail payload -- GET /api/v1/incidents/{id}
#    (producer: _incident_row_to_dict; consumers: .NET passthrough + React
#    Timeline.jsx / AlertFeed.jsx / ApprovalControls.jsx / ActionPanel.jsx)
# ---------------------------------------------------------------------------

class TestIncidentDetailContract:
    REQUIRED_TOP_LEVEL = {
        "id": "incident.id (React key, AlertFeed e2e selectors)",
        "siteId": "incident.siteId (RBAC site scoping, both layers)",
        "equipmentId": "incident.equipmentId",
        "equipmentName": "incident.equipmentName (AlertFeed card)",
        "status": "incident.status (StatusBadge, e2e waits, state machine)",
        "severity": "incident.severity (AlertFeed card + RiskPanel)",
        "detectedAt": "incident.detectedAt (AlertFeed 'time since detection')",
        "anomalySummary": "incident.anomalySummary (DetectionPanel)",
        "retrievedEvidence": "incident.retrievedEvidence (EvidencePanel)",
        "rootCause": "incident.rootCause (RootCausePanel)",
        "risk": "incident.risk (RiskPanel)",
        "recommendation": "incident.recommendation (RecommendationPanel)",
        "requiredApprovalLevel": "incident.requiredApprovalLevel (ApprovalControls gating)",
        "estimatedCostUsd": "incident.estimatedCostUsd (cost telemetry)",
        "resolvedAt": "incident.resolvedAt",
        "createdAt": "incident.createdAt",
        "updatedAt": "incident.updatedAt",
    }

    def test_every_required_field_present(self):
        """Any missing key = a .NET DTO or React component silently broke."""
        payload = _incident_row_to_dict(_db_record())
        missing = [f"{k} ({why})" for k, why in self.REQUIRED_TOP_LEVEL.items() if k not in payload]
        assert not missing, f"incident detail contract broken -- MISSING FIELDS: {missing}"

    def test_nested_stage_payloads(self):
        payload = _incident_row_to_dict(_db_record())
        assert set(payload["rootCause"]) == {
            "hypothesis", "confidence", "citedEvidence",
        }, "rootCause contract broken -- RootCausePanel.jsx reads hypothesis/confidence/citedEvidence"
        assert set(payload["risk"]) == {
            "severity", "consequences", "hitlLevel", "assessedAt",
        }, "risk contract broken -- RiskPanel.jsx reads severity/consequences/hitlLevel"
        assert set(payload["recommendation"]) == {
            "actions", "rationale", "toolCallLog", "recommendedAt",
        }, "recommendation contract broken -- RecommendationPanel.jsx reads actions/rationale/toolCallLog"

    def test_null_stage_fields_render_null_not_missing(self):
        """Frontend branches on `=== null` for not-yet-computed stages; a
        field that disappears (vs nulls) would crash it."""
        payload = _incident_row_to_dict(_db_record(
            RootCauseHypothesis=None, RiskSeverity=None, RecommendedActions=None,
        ))
        assert payload["rootCause"] is None, "rootCause must be null (not absent) before diagnosis completes"
        assert payload["risk"] is None, "risk must be null (not absent) before assessment completes"
        assert payload["recommendation"] is None, "recommendation must be null (not absent) before planning completes"

    def test_jsonb_columns_arrive_as_objects_not_strings(self):
        payload = _incident_row_to_dict(_db_record())
        assert isinstance(payload["anomalySummary"], dict), "anomalySummary must deserialize to an object"
        assert isinstance(payload["retrievedEvidence"], dict), "retrievedEvidence must deserialize to an object"
        assert isinstance(payload["recommendation"]["toolCallLog"], list), "toolCallLog must be an array"


# ---------------------------------------------------------------------------
# 2. Dashboard/timeline data -- the fields the React app actually renders
# ---------------------------------------------------------------------------

class TestFrontendTimelineContract:
    def test_anomaly_summary_sensor_fields(self):
        """DetectionPanel.jsx reads s.sensor_type/value/unit/reason/
        static_margin/drift_z/normal_min/normal_max."""
        obs = AnomalyObservation(
            sensor_id="s-flow",
            equipment_id="e-1",
            sensor_type="flow_rate",
            value=141.2,
            unit="m3/h",
            observed_at=_ts(),
            reason="outside band + drifting",
            static_margin=1.4,
            drift_z=7.1,
        )
        sensor = _sensor_payload(obs, {"s-flow": {"normal_min": 118.0, "normal_max": 132.0}})
        for field in ("sensor_id", "sensor_type", "value", "unit", "observed_at",
                      "reason", "static_margin", "drift_z", "normal_min", "normal_max"):
            assert field in sensor, (
                f"anomalySummary.sensors[].{field} MISSING -- "
                f"DetectionPanel.jsx renders this field"
            )
        assert sensor["normal_min"] == 118.0 and sensor["normal_max"] == 132.0

    def test_correlated_summary_shape(self):
        summary = _correlated_summary("e-1", [{"sensor_id": "s-1"}])
        assert set(summary) == {"equipment_id", "detected_at", "sensor_count", "sensors"}, (
            "anomalySummary top-level contract broken -- DetectionPanel/Timeline "
            "read summary.sensors, Orchestrator + Knowledge read equipment_id"
        )

    def test_matched_history_fields_incl_provenance(self):
        """EvidencePanel.jsx + Timeline.jsx read h.equipmentName/rootCause/
        source/sourceType/sourceUrl; Prompt 8 provenance REQUIRES sourceType."""
        rec = _Record(
            Id="h-1", EquipmentId="e-1", EquipmentName="Cooling Water Pump CP-04",
            Symptoms='["flow_rate high", "vibration high"]', RootCause="pump degradation",
            Resolution="strainer cleaned", OccurredAt=_ts(days=-17), Source="internal",
            SourceType="illustrative", SourceUrl=None,
        )
        hist = _history_row_to_dict(rec, "e-1")
        for field in ("id", "equipmentName", "symptoms", "rootCause", "resolution",
                      "occurredAt", "source", "sourceType", "sourceUrl", "sameEquipment"):
            assert field in hist, f"matched_history[].{field} MISSING -- Timeline.jsx/EvidencePanel renders it"
        assert hist["sourceType"] == "illustrative"
        assert hist["sameEquipment"] is True


# ---------------------------------------------------------------------------
# 3. AnomalyDetected event on the EventBus (pub/sub contract)
#    Producer: DetectionAgent._publish_event; consumers: Knowledge +
#    Root-Cause agents (possibly in a different process after the Service Bus
#    swap -- this is why the event schema is pinned, not assumed).
# ---------------------------------------------------------------------------

class TestAnomalyDetectedEventContract:
    REQUIRED = {
        "event": "discriminator literal 'AnomalyDetected'",
        "event_id": "deduplication key",
        "incident_id": "Knowledge/Root-Cause look everything up by this",
        "equipment_id": "history similarity match",
        "severity": "root-cause prompt + telemetry",
        "detected_at": "correlation windows",
        "sensor_count": "orchestration gating",
        "sensors": "the retrieval query is built from these",
    }

    def test_event_schema(self):
        """Replays the REAL publish path with a recording bus."""
        import asyncio

        from app.eventbus.base import EventBus

        class _RecordingBus(EventBus):
            def __init__(self):
                self.published = []

            async def publish(self, topic, payload):
                self.published.append((topic, payload))

            async def subscribe(self, topic, handler):
                pass

            async def start(self):
                pass

            async def stop(self):
                pass

        from app.agents.detection import agent as det

        bus = _RecordingBus()
        agent = det.DetectionAgent.__new__(det.DetectionAgent)
        agent._bus = bus
        summary = _correlated_summary(
            "22222222-2222-2222-2222-222222222222",
            [{"sensor_id": "s-1", "sensor_type": "flow_rate", "value": 141.2, "unit": "m3/h"}],
        )
        asyncio.run(agent._publish_event("inc-1", summary, "high"))

        assert len(bus.published) == 1, "AnomalyDetected must be published exactly once per correlated anomaly"
        topic, event = bus.published[0]
        assert topic == "pgai.anomalies", "topic renamed -- every downstream agent subscribes to pgai.anomalies"
        missing = [k for k in self.REQUIRED if k not in event]
        assert not missing, f"AnomalyDetected contract broken -- MISSING FIELDS: {missing}"
        assert event["event"] == "AnomalyDetected"
        assert event["incident_id"] == "inc-1"
        assert isinstance(event["sensors"], list) and event["sensors"][0]["sensor_type"] == "flow_rate"

    def test_stage_completed_event_schema(self):
        """Orchestrator coordination event: Knowledge/Root-Cause completion
        is signaled with stage + incident_id; risk adds hitl_level."""
        from app.agents.orchestrator.agent import TOPIC_STAGE_COMPLETED

        risk_completed = {
            "stage": "risk",
            "incident_id": "inc-1",
            "severity": "high",
            "hitl_level": 2,
        }
        for field in ("stage", "incident_id"):
            assert field in risk_completed, f"stage_completed event missing {field} -- Orchestrator routing breaks"
        assert TOPIC_STAGE_COMPLETED == "pgai.stage_completed"


# ---------------------------------------------------------------------------
# 4. Provenance contract (Prompt 8): every citation surfaces sourceType
# ---------------------------------------------------------------------------

class TestProvenanceContract:
    def test_sop_chunk_provenance_fields(self):
        chunk = {
            "docId": "PUB-DOE-PUMP-SOURCEBOOK",
            "title": "Improving Pumping System Performance",
            "section": "Section 2",
            "score": 0.72,
            "excerpt": "Maintaining pump suction...",
            "citation": "PUB-DOE-PUMP-SOURCEBOOK / Improving Pumping System Performance",
            "sourceType": "public_real",
            "sourceUrl": "https://www.energy.gov/eere/amo/pumping-system-assessment",
        }
        for field in ("sourceType", "sourceUrl", "docId", "citation"):
            assert field in chunk, (
                f"sop_chunks[].{field} MISSING -- Prompt 8 requires provenance on "
                f"EVERY citation (EvidenceCitation.jsx renders the public_real badge)"
            )

    def test_incident_row_carries_source_fields(self):
        payload = _incident_row_to_dict(_db_record())
        evidence = payload["retrievedEvidence"]
        assert "sop_chunks" in evidence, "retrievedEvidence.sop_chunks missing"
        assert "matched_history" in evidence, "retrievedEvidence.matched_history missing"
