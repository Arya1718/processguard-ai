"""Unit tests for app/core/db.py read/write helpers.

Uses a FakePool with a shared FakeConn to mock asyncpg. No real Postgres needed.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

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


class FakeRecord:
    """Dict-like mock that behaves like an asyncpg Record."""

    def __init__(self, **fields):
        self._data = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data


def _mk_row(**fields):
    return FakeRecord(**fields)


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def pool(monkeypatch):
    """Inject a mock pool with a shared conn into app.core.db."""
    from app.core import db as db_mod

    shared_conn = MagicMock()

    class _Txn:
        async def __aenter__(self):
            return shared_conn
        async def __aexit__(self, *a):
            return None

    shared_conn.transaction = MagicMock(return_value=_Txn())

    mock_pool = MagicMock()
    mock_pool._closed = False

    class _AcquireCtx:
        async def __aenter__(self):
            return shared_conn
        async def __aexit__(self, *a):
            return None

    mock_pool.acquire = MagicMock(return_value=_AcquireCtx())

    # For functions that call pool.fetch/fetchrow directly (no transaction)
    async def _fetch(q, *p):
        return mp._rows
    async def _fetchrow(q, *p):
        return mp._row
    async def _fetchval(q, *p):
        return mp._val
    async def _execute(q, *p):
        return mp._exec_status

    mp = mock_pool
    mock_pool._rows = []
    mock_pool._row = None
    mock_pool._val = None
    mock_pool._exec_status = "UPDATE 1"
    mock_pool.fetch = AsyncMock(side_effect=_fetch)
    mock_pool.fetchrow = AsyncMock(side_effect=_fetchrow)
    mock_pool.fetchval = AsyncMock(side_effect=_fetchval)
    mock_pool.execute = AsyncMock(side_effect=_execute)

    # Conn-level methods (used inside transactions)
    shared_conn.fetch = AsyncMock(return_value=[])
    shared_conn.fetchrow = AsyncMock(return_value=None)
    shared_conn.fetchval = AsyncMock(return_value=None)
    shared_conn.execute = AsyncMock(return_value="UPDATE 1")

    monkeypatch.setattr(db_mod, "_pool", mock_pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: mock_pool)
    return mock_pool, shared_conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_parse_uuid_valid(self):
        from app.core.db import _parse_uuid
        val = _parse_uuid(str(uuid.uuid4()))
        assert isinstance(val, uuid.UUID)

    def test_parse_uuid_invalid(self):
        from app.core.db import _parse_uuid
        with pytest.raises(ValueError):
            _parse_uuid("not-a-uuid")

    def test_json_or_none_none(self):
        from app.core.db import _json_or_none
        assert _json_or_none(None) is None

    def test_json_or_none_empty(self):
        from app.core.db import _json_or_none
        assert _json_or_none("") is None

    def test_json_or_none_string(self):
        from app.core.db import _json_or_none
        assert _json_or_none('{"key": "val"}') == {"key": "val"}

    def test_json_or_none_passthrough(self):
        from app.core.db import _json_or_none
        obj = {"key": "val"}
        assert _json_or_none(obj) == obj


# ---------------------------------------------------------------------------
# list_incidents
# ---------------------------------------------------------------------------

class TestListIncidents:
    @pytest.mark.asyncio
    async def test_list_no_filter(self, pool):
        from app.core import db
        mp, conn = pool
        now = _now()
        row = _mk_row(
            Id=uuid.uuid4(), SiteId=uuid.uuid4(), EquipmentName="Pump",
            Status="open", Severity="high", DetectedAt=now,
            AnomalySummary=None, Evidence=None, RootCauseHypothesis=None,
            Confidence=None, CitedEvidence=None, RiskSeverity=None,
            Consequences=None, HitlLevel=None, RequiredApprovalLevel=1,
            RecommendedActions=None, Rationale=None, ToolCallLog=None,
            RiskAssessedAt=None, RecommendedAt=None, ResolvedAt=None,
            CreatedAt=now, UpdatedAt=now, EstimatedCostUsd=None,
            EquipmentId=uuid.uuid4(),
        )
        mp._rows = [row]
        mp._val = 1

        items, total = await db.list_incidents(None, str(uuid.uuid4()), 50, 0)
        assert total == 1
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_list_invalid_site_uuid(self, pool):
        from app.core import db
        items, total = await db.list_incidents(None, "not-a-uuid", 50, 0)
        assert items == []
        assert total == 0


# ---------------------------------------------------------------------------
# get_incident
# ---------------------------------------------------------------------------

class TestGetIncident:
    @pytest.mark.asyncio
    async def test_get_incident_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = None
        result = await db.get_incident(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_get_incident_invalid_uuid(self):
        from app.core import db
        result = await db.get_incident("not-a-uuid")
        assert result is None


# ---------------------------------------------------------------------------
# add_incident_cost
# ---------------------------------------------------------------------------

class TestAddIncidentCost:
    @pytest.mark.asyncio
    async def test_add_cost_success(self, pool):
        from app.core import db
        mp, conn = pool
        mp._exec_status = "UPDATE 1"
        result = await db.add_incident_cost(str(uuid.uuid4()), 0.05)
        assert result is True

    @pytest.mark.asyncio
    async def test_add_cost_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._exec_status = "UPDATE 0"
        result = await db.add_incident_cost(str(uuid.uuid4()), 0.05)
        assert result is False


# ---------------------------------------------------------------------------
# get_incident_state_history
# ---------------------------------------------------------------------------

class TestStateHistory:
    @pytest.mark.asyncio
    async def test_get_history(self, pool):
        from app.core import db
        mp, conn = pool
        now = _now()
        row = _mk_row(
            FromStatus="open", ToStatus="investigating", Actor="system",
            Reason="diagnosis started", Detail=None, ChangedAt=now,
        )
        mp._rows = [row]
        result = await db.get_incident_state_history(str(uuid.uuid4()))
        assert len(result) == 1
        assert result[0]["from"] == "open"
        assert result[0]["to"] == "investigating"


# ---------------------------------------------------------------------------
# get_incident_for_decision
# ---------------------------------------------------------------------------

class TestGetIncidentForDecision:
    @pytest.mark.asyncio
    async def test_get_for_decision_found(self, pool):
        from app.core import db
        mp, conn = pool
        row = _mk_row(
            Id=uuid.uuid4(), SiteId=uuid.uuid4(), EquipmentId=uuid.uuid4(),
            Status="awaiting_approval", HitlLevel=2, RequiredApprovalLevel=2,
            RecommendedActions=None, Rationale="test", ToolCallLog=None,
            RiskSeverity="high", RootCauseHypothesis="bearing failure",
            Confidence=90, AnomalySummary=None,
        )
        mp._row = row
        result = await db.get_incident_for_decision(str(uuid.uuid4()))
        assert result is not None
        assert result["status"] == "awaiting_approval"
        assert result["hitl_level"] == 2

    @pytest.mark.asyncio
    async def test_get_for_decision_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = None
        result = await db.get_incident_for_decision(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_get_for_decision_invalid_uuid(self):
        from app.core import db
        result = await db.get_incident_for_decision("not-a-uuid")
        assert result is None


# ---------------------------------------------------------------------------
# save_incident_evidence
# ---------------------------------------------------------------------------

class TestSaveIncidentEvidence:
    @pytest.mark.asyncio
    async def test_save_success(self, pool):
        from app.core import db
        mp, conn = pool
        mp._exec_status = "UPDATE 1"
        result = await db.save_incident_evidence(str(uuid.uuid4()), {"key": "val"})
        assert result is True

    @pytest.mark.asyncio
    async def test_save_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._exec_status = "UPDATE 0"
        result = await db.save_incident_evidence(str(uuid.uuid4()), {"key": "val"})
        assert result is False


# ---------------------------------------------------------------------------
# save_root_cause
# ---------------------------------------------------------------------------

class TestSaveRootCause:
    @pytest.mark.asyncio
    async def test_save_root_cause_success(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=_mk_row(Status="investigating"))
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await db.save_root_cause(
            str(uuid.uuid4()), "bearing failure", 90, [], "investigating"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_save_root_cause_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="UPDATE 0")

        result = await db.save_root_cause(
            str(uuid.uuid4()), "bearing failure", 90, [], "investigating"
        )
        assert result is False


# ---------------------------------------------------------------------------
# save_risk_assessment
# ---------------------------------------------------------------------------

class TestSaveRiskAssessment:
    @pytest.mark.asyncio
    async def test_save_risk_success(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=_mk_row(Status="risk_assessed"))

        result = await db.save_risk_assessment(
            str(uuid.uuid4()), "high", [{"consequence": "damage"}], 3, 3
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_save_risk_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=None)

        result = await db.save_risk_assessment(
            str(uuid.uuid4()), "high", [], 2, 2
        )
        assert result is False


# ---------------------------------------------------------------------------
# save_recommendation
# ---------------------------------------------------------------------------

class TestSaveRecommendation:
    @pytest.mark.asyncio
    async def test_save_recommendation_success(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=_mk_row(Status="recommended"))

        result = await db.save_recommendation(
            str(uuid.uuid4()), [{"action": "inspect"}], "bearing noise", [], "awaiting_approval"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_save_recommendation_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=None)

        result = await db.save_recommendation(
            str(uuid.uuid4()), [], "test", [], "awaiting_approval"
        )
        assert result is False


# ---------------------------------------------------------------------------
# transition_incident
# ---------------------------------------------------------------------------

class TestTransitionIncident:
    @pytest.mark.asyncio
    async def test_transition_success(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=_mk_row(Status="investigating"))

        result = await db.transition_incident(str(uuid.uuid4()), "investigating")
        assert result is True

    @pytest.mark.asyncio
    async def test_transition_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=None)

        result = await db.transition_incident(str(uuid.uuid4()), "investigating")
        assert result is False


# ---------------------------------------------------------------------------
# resolve_incident_manually
# ---------------------------------------------------------------------------

class TestResolveIncident:
    @pytest.mark.asyncio
    async def test_resolve_success(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=_mk_row(Id=uuid.uuid4(), Status="open"))

        result = await db.resolve_incident_manually(
            str(uuid.uuid4()), "test_user", "fixed"
        )
        assert result is not None
        assert result["status"] == "resolved"

    @pytest.mark.asyncio
    async def test_resolve_invalid_uuid(self, pool):
        from app.core import db
        result = await db.resolve_incident_manually("not-a-uuid", "user", "note")
        assert result is None


# ---------------------------------------------------------------------------
# get_incident_action_record
# ---------------------------------------------------------------------------

class TestGetActionRecord:
    @pytest.mark.asyncio
    async def test_get_action_record_found(self, pool):
        from app.core import db
        mp, conn = pool
        now = _now()
        mp._row = _mk_row(
            Status="action_taken", ActionExecutedAt=now, ResolvedAt=None,
        )
        mp._rows = [_mk_row(
            Operation="reorder_part", Description="Replace bearing", Reference="WO-123",
            Outcome="succeeded", Detail=None, AttemptedAt=now, CompletedAt=now,
        )]
        mp._val = uuid.uuid4()
        result = await db.get_incident_action_record(str(uuid.uuid4()))
        assert result is not None
        assert result["status"] == "action_taken"
        assert len(result["operations"]) == 1
        assert result["operations"][0]["operation"] == "reorder_part"

    @pytest.mark.asyncio
    async def test_get_action_record_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = None
        result = await db.get_incident_action_record(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_get_action_record_invalid_uuid(self):
        from app.core import db
        result = await db.get_incident_action_record("not-a-uuid")
        assert result is None


# ---------------------------------------------------------------------------
# record_approval
# ---------------------------------------------------------------------------

class TestRecordApproval:
    @pytest.mark.asyncio
    async def test_record_approval_success(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=_mk_row(
            Status="awaiting_approval", RecommendedActions=None,
            Rationale="test rationale", ToolCallLog=None,
            RiskSeverity="high", Consequences=None, HitlLevel=2,
        ))

        result = await db.record_approval(
            str(uuid.uuid4()), "approved", "test_user", 2, "ok"
        )
        assert result is not None
        assert isinstance(result, datetime)

    @pytest.mark.asyncio
    async def test_record_approval_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=None)

        result = await db.record_approval(
            str(uuid.uuid4()), "approved", "test_user", 2, "ok"
        )
        assert result is None


# ---------------------------------------------------------------------------
# record_state_transition
# ---------------------------------------------------------------------------

class TestRecordStateTransition:
    @pytest.mark.asyncio
    async def test_record_transition(self, pool):
        from app.core import db
        mp, conn = pool
        mp._exec_status = "UPDATE 1"
        await db.record_state_transition(
            str(uuid.uuid4()), "open", "investigating", actor="system"
        )
        mp.execute.assert_called_once()

class TestEquipmentMaintenanceStatus:
    @pytest.mark.asyncio
    async def test_found(self, pool):
        from app.core import db
        mp, conn = pool
        now = _now()
        mp._row = _mk_row(
            Id=uuid.uuid4(), Name="Pump", LastServicedAt=now, ConditionFlags=None,
        )
        result = await db.get_equipment_maintenance_status(str(uuid.uuid4()))
        assert result is not None
        assert result["name"] == "Pump"

    @pytest.mark.asyncio
    async def test_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = None
        result = await db.get_equipment_maintenance_status(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_uuid(self):
        from app.core import db
        result = await db.get_equipment_maintenance_status("not-a-uuid")
        assert result is None


# ---------------------------------------------------------------------------
# latest_readings_for_site
# ---------------------------------------------------------------------------

class TestLatestReadings:
    @pytest.mark.asyncio
    async def test_found(self, pool):
        from app.core import db
        mp, conn = pool
        now = _now()
        mp._rows = [_mk_row(
            sensor_id=str(uuid.uuid4()), sensor_type="temperature", unit="degC",
            value=80, occurred_at=now, normal_min=29, normal_max=32,
        )]
        result = await db.latest_readings_for_site(str(uuid.uuid4()))
        assert len(result) == 1
        assert result[0]["sensorType"] == "temperature"

    @pytest.mark.asyncio
    async def test_invalid_uuid(self):
        from app.core import db
        result = await db.latest_readings_for_site("not-a-uuid")
        assert result == []


# ---------------------------------------------------------------------------
# find_similar_history
# ---------------------------------------------------------------------------

class TestFindSimilarHistory:
    @pytest.mark.asyncio
    async def test_no_sensor_types(self):
        from app.core import db
        result = await db.find_similar_history(str(uuid.uuid4()), [], 3)
        assert result == []

    @pytest.mark.asyncio
    async def test_with_matches(self, pool):
        from app.core import db
        mp, conn = pool
        now = _now()
        mp._rows = [_mk_row(
            Id=uuid.uuid4(), EquipmentId=uuid.uuid4(), EquipmentName="Pump",
            Symptoms='["vibration high"]', RootCause="bearing", Resolution="replaced",
            OccurredAt=now, Source="log", SourceType="illustrative", SourceUrl=None,
        )]
        result = await db.find_similar_history(str(uuid.uuid4()), ["vibration"], 3)
        assert len(result) == 1
        assert result[0]["rootCause"] == "bearing"


# ---------------------------------------------------------------------------
# get_incident_diagnosis_state
# ---------------------------------------------------------------------------

class TestDiagnosisState:
    @pytest.mark.asyncio
    async def test_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = _mk_row(
            Evidence=None, RootCauseHypothesis="bearing", AnomalySummary='{"sensors": []}',
            Confidence=90,
        )
        result = await db.get_incident_diagnosis_state(str(uuid.uuid4()))
        assert result is not None
        assert result["root_cause_hypothesis"] == "bearing"

    @pytest.mark.asyncio
    async def test_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = None
        result = await db.get_incident_diagnosis_state(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_uuid(self):
        from app.core import db
        result = await db.get_incident_diagnosis_state("not-a-uuid")
        assert result is None


# ---------------------------------------------------------------------------
# get_similar_incident_outcomes
# ---------------------------------------------------------------------------

class TestSimilarIncidentOutcomes:
    @pytest.mark.asyncio
    async def test_found(self, pool):
        from app.core import db
        mp, conn = pool
        now = _now()
        mp._rows = [_mk_row(
            EquipmentName="Pump", Symptoms='["vibration"]', RootCause="bearing",
            ActionTaken="replace", ActionWorked=True, Resolution="fixed",
            OccurredAt=now, Source="log", SourceType="real_public", SourceUrl="http://example.com",
        )]
        result = await db.get_similar_incident_outcomes(str(uuid.uuid4()), "vibration")
        assert len(result) == 1
        assert result[0]["rootCause"] == "bearing"


# ---------------------------------------------------------------------------
# get_executed_action_kinds
# ---------------------------------------------------------------------------

class TestExecutedActionKinds:
    @pytest.mark.asyncio
    async def test_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._rows = [_mk_row(Operation="reorder_part")]
        result = await db.get_executed_action_kinds(str(uuid.uuid4()))
        assert len(result) == 1
        assert result[0] == "reorder_part"

    @pytest.mark.asyncio
    async def test_invalid_uuid(self, pool):
        from app.core import db
        result = await db.get_executed_action_kinds("not-a-uuid")
        assert result == []


# ---------------------------------------------------------------------------
# record_action_execution
# ---------------------------------------------------------------------------

class TestRecordActionExecution:
    @pytest.mark.asyncio
    async def test_record(self, pool):
        from app.core import db
        mp, conn = pool
        await db.record_action_execution(
            str(uuid.uuid4()), "reorder_part", "succeeded", reference="WO-123",
        )
        mp.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_uuid(self, pool):
        from app.core import db
        await db.record_action_execution("not-a-uuid", "reorder", "ok")


# ---------------------------------------------------------------------------
# save_action_result
# ---------------------------------------------------------------------------

class TestSaveActionResult:
    @pytest.mark.asyncio
    async def test_success_with_executions(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=_mk_row(Id=uuid.uuid4()))
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await db.save_action_result(
            str(uuid.uuid4()), "action_taken", {"reason": "ok"},
            executions=[{"action": "work_order"}], approved_by="user", decided_at="2026-01-01T00:00:00Z",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_not_found(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchrow = AsyncMock(return_value=None)

        result = await db.save_action_result(
            str(uuid.uuid4()), "action_failed", {"reason": "err"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_invalid_uuid(self, pool):
        from app.core import db
        result = await db.save_action_result("not-a-uuid", "action_taken", {}, None, None, None)
        assert result is False


# ---------------------------------------------------------------------------
# record_learned_incident
# ---------------------------------------------------------------------------

class TestRecordLearnedIncident:
    @pytest.mark.asyncio
    async def test_record(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = _mk_row(Id=uuid.uuid4())
        result = await db.record_learned_incident(
            str(uuid.uuid4()), "Pump", {"symptoms": "vibration"},
            "bearing failure", "replaced", True, "fixed", "log-1",
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_record_no_equipment(self, pool):
        from app.core import db
        mp, conn = pool
        mp._row = _mk_row(Id=uuid.uuid4())
        result = await db.record_learned_incident(
            None, "Pump", {"symptoms": "vibration"},
            "bearing failure", "replaced", True, "fixed", "log-1",
        )
        assert result is not None


# ---------------------------------------------------------------------------
# get_action_taken_incident_ids
# ---------------------------------------------------------------------------

class TestGetActionTakenIds:
    @pytest.mark.asyncio
    async def test_found(self, pool):
        from app.core import db
        mp, conn = pool
        mp._rows = [_mk_row(Id=uuid.uuid4()), _mk_row(Id=uuid.uuid4())]
        result = await db.get_action_taken_incident_ids()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_empty(self, pool):
        from app.core import db
        mp, conn = pool
        mp._rows = []
        result = await db.get_action_taken_incident_ids()
        assert result == []


# ---------------------------------------------------------------------------
# close_incident_loop
# ---------------------------------------------------------------------------

class TestCloseIncidentLoop:
    @pytest.mark.asyncio
    async def test_success(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchval = AsyncMock(return_value=0)  # no existing audit log
        conn.fetchrow = AsyncMock(return_value=_mk_row(Id=uuid.uuid4()))
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await db.close_incident_loop(
            str(uuid.uuid4()), str(uuid.uuid4()), "Pump",
            {"symptoms": "vibration"}, "bearing failure", "replaced",
            True, "fixed", "log-1",
        )
        assert result is not None
        assert str(result).replace("-", "").isalnum() or "-" in str(result)

    @pytest.mark.asyncio
    async def test_already_closed(self, pool):
        from app.core import db
        mp, conn = pool
        conn.fetchval = AsyncMock(return_value=1)  # audit log exists
        result = await db.close_incident_loop(
            str(uuid.uuid4()), None, "Pump", {}, "bearing", "replace", True, "ok", "log",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_uuid(self, pool):
        from app.core import db
        result = await db.close_incident_loop(
            "not-a-uuid", None, "Pump", {}, "bearing", "replace", True, "ok", "log",
        )
        assert result is None


# ---------------------------------------------------------------------------
# check_postgres
# ---------------------------------------------------------------------------

class TestCheckPostgres:
    @pytest.mark.asyncio
    async def test_check_ok(self, monkeypatch):
        from app.core import db

        class _FakeConn:
            async def fetchval(self, q):
                return 1
            async def close(self):
                pass

        async def _connect(*args, **kwargs):
            return _FakeConn()
        monkeypatch.setattr("asyncpg.connect", _connect)
        result = await db.check_postgres("test-dsn")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_fail(self, monkeypatch):
        from app.core import db

        async def _connect(*args, **kwargs):
            raise ConnectionError("refused")
        monkeypatch.setattr("asyncpg.connect", _connect)
        result = await db.check_postgres("test-dsn")
        assert result is False


# ---------------------------------------------------------------------------
# init_db_pool / close_db_pool / get_pool
# ---------------------------------------------------------------------------

class TestPoolLifecycle:
    @pytest.mark.asyncio
    async def test_init_and_close(self, monkeypatch):
        from app.core import db
        # With pool fixture active, get_pool should work
        # Without pool (None), it should raise
        monkeypatch.setattr(db, "_pool", None)
        with pytest.raises(RuntimeError):
            db.get_pool()

    @pytest.mark.asyncio
    async def test_init_db_pool(self, monkeypatch):
        from app.core import db

        async def _create_pool(*args, **kwargs):
            return MagicMock(_closed=False)
        monkeypatch.setattr("asyncpg.create_pool", _create_pool)
        pool = await db.init_db_pool("test-dsn")
        assert pool is not None

    @pytest.mark.asyncio
    async def test_init_db_pool_idempotent(self, monkeypatch):
        from app.core import db
        existing = MagicMock(_closed=False)
        monkeypatch.setattr(db, "_pool", existing)
        pool = await db.init_db_pool("test-dsn")
        assert pool is existing  # returns existing if not closed

    @pytest.mark.asyncio
    async def test_init_db_pool_reinit_on_closed(self, monkeypatch):
        from app.core import db

        async def _create_pool(*args, **kwargs):
            return MagicMock(_closed=False)
        monkeypatch.setattr("asyncpg.create_pool", _create_pool)
        monkeypatch.setattr(db, "_pool", MagicMock(_closed=True))
        pool = await db.init_db_pool("test-dsn")
        assert pool is not None

    @pytest.mark.asyncio
    async def test_close_db_pool(self, monkeypatch):
        from app.core import db
        mock_pool = MagicMock(_closed=False)
        mock_pool.close = AsyncMock()
        monkeypatch.setattr(db, "_pool", mock_pool)
        await db.close_db_pool()
        mock_pool.close.assert_called_once()
        assert db._pool is None

    @pytest.mark.asyncio
    async def test_close_db_pool_already_none(self, monkeypatch):
        from app.core import db
        monkeypatch.setattr(db, "_pool", None)
        await db.close_db_pool()  # should not raise
