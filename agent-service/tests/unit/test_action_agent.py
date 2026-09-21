"""Unit tests for the Action Agent (Prompt 5).

Pure asyncio with a mocked CMMS client and a monkeypatched db layer -- no
database, no Redis, no live services. Covers the three required behaviors:

  1. mapping: a known recommendation shape produces the correct typed CMMS
     call (and nothing else);
  2. idempotency: a second trigger for the same approved incident creates
     NO second work order;
  3. failure path: a failing CMMS client exhausts bounded retries and the
     incident lands on action_failed with a recorded reason -- never stuck
     silently.
"""
from __future__ import annotations

import json

import pytest

import app.agents.action.agent as action_module
from app.agents.action.mapping import (
    ACTION_TYPE_MANUAL,
    ACTION_TYPE_PRODUCT_REORDER,
    ACTION_TYPE_WORK_ORDER,
    map_recommendation_to_cmms,
)
from app.core.cmms_client import CmmsError

# ACTION_TYPE_MANUAL is asserted implicitly through unmapped-action tests;
# keep the import honest for type vocabulary documentation.
assert ACTION_TYPE_MANUAL == "manual"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeCmms:
    """Records every call; scripted failures optional."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self._seq = 0

    async def create_work_order(self, **kwargs) -> dict:
        self.calls.append(("create_work_order", kwargs))
        if self.fail:
            raise CmmsError("POST", "/work-orders", "connection refused")
        self._seq += 1
        return {"id": f"wo-{self._seq}", "status": "open"}

    async def request_reorder(self, **kwargs) -> dict:
        self.calls.append(("request_reorder", kwargs))
        if self.fail:
            raise CmmsError("POST", "/inventory/coagulant/reorder", "connection refused")
        self._seq += 1
        return {"id": f"ro-{self._seq}", "status": "requested"}

    async def get_work_order(self, work_order_id: str) -> dict:
        self.calls.append(("get_work_order", {"work_order_id": work_order_id}))
        return {"id": work_order_id, "status": "open"}


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))


# ---------------------------------------------------------------------------
# db monkeypatch helpers
# ---------------------------------------------------------------------------

def _patch_db(monkeypatch, *, status="approved", executed_kinds=None,
              recommended_actions=None, equipment_id="eq-1") -> dict:
    """Point the agent's db helpers at in-memory fakes; returns the fake
    store so tests can assert on writes."""
    store = {
        "executions": [],
        "results": [],
        "status": status,
        "executed_kinds": executed_kinds or [],
        "recommended_actions": recommended_actions or [],
        "equipment_id": equipment_id,
    }

    async def fake_get_view(incident_id):
        return {
            "status": store["status"],
            "equipment_id": store["equipment_id"],
            "recommended_actions": store["recommended_actions"],
        }

    async def fake_executed_kinds(incident_id):
        return list(store["executed_kinds"])

    async def fake_record_execution(incident_id, operation, outcome,
                                    reference=None, response=None, error=None):
        store["executions"].append({
            "incident_id": incident_id, "operation": operation.kind,
            "outcome": outcome, "reference": reference, "error": error,
        })

    async def fake_save_result(incident_id, status, detail,
                               executions=None, approved_by=None, decided_at=None):
        store["results"].append({
            "incident_id": incident_id, "status": status,
            "detail": detail, "executions": executions,
        })
        store["status"] = status
        return True

    monkeypatch.setattr(action_module, "get_incident_for_decision", fake_get_view)
    monkeypatch.setattr(action_module, "get_executed_action_kinds", fake_executed_kinds)
    monkeypatch.setattr(action_module, "record_action_execution", fake_record_execution)
    monkeypatch.setattr(action_module, "save_action_result", fake_save_result)
    return store


# ---------------------------------------------------------------------------
# 1. Mapping: recommendation shape -> typed CMMS call
# ---------------------------------------------------------------------------

def test_mapping_maintenance_action_becomes_work_order() -> None:
    result = map_recommendation_to_cmms({
        "actions": [{"action": "Inspect pump and suction filter", "priority": 1,
                     "type": "work_order"}],
    })
    assert result.has_operations
    assert len(result.operations) == 1
    op = result.operations[0]
    assert op.kind == ACTION_TYPE_WORK_ORDER
    assert op.description == "Inspect pump and suction filter"
    assert result.unmapped == []


def test_mapping_reorder_action_becomes_reorder_call() -> None:
    result = map_recommendation_to_cmms({
        "actions": [{"action": "Reorder 50 of biocide", "priority": 2,
                     "type": "product_reorder", "product_key": "biocide",
                     "quantity": 50.0}],
    })
    assert len(result.operations) == 1
    op = result.operations[0]
    assert op.kind == ACTION_TYPE_PRODUCT_REORDER
    assert op.product_key == "biocide"
    assert op.quantity == 50.0


def test_mapping_mixed_and_unmapped_actions() -> None:
    result = map_recommendation_to_cmms({
        "actions": [
            {"action": "Inspect the pump", "priority": 1, "type": "work_order"},
            {"action": "Watch the gauges manually", "priority": 2, "type": "manual"},
            {"action": "Order chemical", "priority": 3, "type": "product_reorder"},  # no product
        ],
    })
    kinds = [op.kind for op in result.operations]
    assert kinds == [ACTION_TYPE_WORK_ORDER]
    assert len(result.unmapped) == 2


def test_mapping_legacy_action_without_type_classified_by_keyword() -> None:
    result = map_recommendation_to_cmms({
        "actions": [{"action": "Verify pump bearing condition", "priority": 1}],
    })
    assert result.operations[0].kind == ACTION_TYPE_WORK_ORDER


def test_mapping_result_is_pure_data() -> None:
    """The mapping carries everything execution + audit need -- no LLM and
    no I/O involved anywhere in the decision."""
    result = map_recommendation_to_cmms({
        "actions": [{"action": "Backwash the strainer", "priority": 1}],
    })
    payload = result.operations[0].to_payload()
    assert set(payload) == {"kind", "description", "product_key", "quantity",
                            "priority", "source_action"}


# ---------------------------------------------------------------------------
# 2. Execution + idempotency
# ---------------------------------------------------------------------------

async def test_execution_creates_exactly_one_work_order(monkeypatch) -> None:
    store = _patch_db(
        monkeypatch,
        recommended_actions=[{"action": "Inspect pump and suction filter",
                              "priority": 1, "type": "work_order"}],
    )
    cmms = FakeCmms()
    agent = action_module.ActionAgent(FakeBus(), cmms=cmms)
    summary = await agent.handle({"incident_id": "0b7fd1d4-0000-0000-0000-000000000001"})

    assert summary["status"] == "action_taken"
    assert [c[0] for c in cmms.calls] == ["create_work_order"]
    call = cmms.calls[0][1]
    assert call["description"] == "Inspect pump and suction filter"
    assert call["source_incident_id"] == "0b7fd1d4-0000-0000-0000-000000000001"
    assert store["results"][0]["status"] == "action_taken"
    # Audit-critical: the CMMS reference is persisted with the execution.
    assert store["executions"][-1]["reference"] == "wo-1"


async def test_idempotent_second_trigger_creates_no_second_work_order(monkeypatch) -> None:
    """The Action Agent triggered twice for the same approved incident must
    not create a duplicate work order."""
    store = _patch_db(
        monkeypatch,
        executed_kinds=["work_order"],  # a previous run already succeeded
        recommended_actions=[{"action": "Inspect pump and suction filter",
                              "priority": 1, "type": "work_order"}],
    )
    cmms = FakeCmms()
    agent = action_module.ActionAgent(FakeBus(), cmms=cmms)
    summary = await agent.handle({"incident_id": "0b7fd1d4-0000-0000-0000-000000000002"})

    assert cmms.calls == [], "no external call may be made on a re-trigger"
    assert summary["acted"] is False
    assert summary["status"] == "action_taken"


async def test_unapproved_incident_is_never_executed(monkeypatch) -> None:
    """The safety gate: without status 'approved' there is no external write."""
    store = _patch_db(
        monkeypatch,
        status="awaiting_approval",
        recommended_actions=[{"action": "Inspect pump", "priority": 1,
                              "type": "work_order"}],
    )
    cmms = FakeCmms()
    agent = action_module.ActionAgent(FakeBus(), cmms=cmms)
    summary = await agent.handle({"incident_id": "0b7fd1d4-0000-0000-0000-000000000003"})

    assert cmms.calls == []
    assert summary["acted"] is False
    assert len(store["results"]) == 0


async def test_execution_publishes_completion_event(monkeypatch) -> None:
    _patch_db(
        monkeypatch,
        recommended_actions=[{"action": "Replace the filter", "priority": 1,
                              "type": "work_order"}],
    )
    bus = FakeBus()
    agent = action_module.ActionAgent(bus, cmms=FakeCmms())
    await agent.handle({"incident_id": "0b7fd1d4-0000-0000-0000-000000000004"})
    topics = [t for t, _ in bus.published]
    assert topics == [action_module.TOPIC_ACTION_COMPLETED]


# ---------------------------------------------------------------------------
# 3. Failure path
# ---------------------------------------------------------------------------

async def test_cmms_failure_lands_on_action_failed(monkeypatch) -> None:
    """A failing CMMS -> action_failed with the reason recorded, and the
    failure becomes visible via an event. (Bounded retry/backoff is proven
    separately against CmmsClient itself -- see test_cmms_client_retry.py --
    since the retry lives inside the client, which is the production shape.)"""
    store = _patch_db(
        monkeypatch,
        recommended_actions=[{"action": "Inspect pump and suction filter",
                              "priority": 1, "type": "work_order"}],
    )
    cmms = FakeCmms(fail=True)
    bus = FakeBus()
    agent = action_module.ActionAgent(bus, cmms=cmms)
    summary = await agent.handle({"incident_id": "0b7fd1d4-0000-0000-0000-000000000005"})

    assert summary["status"] == "action_failed"
    assert len(cmms.calls) == 1
    assert store["results"][0]["status"] == "action_failed"
    reason = store["results"][0]["detail"]["reason"]
    assert "connection refused" in reason
    # Visible, not silent: pgai.action_failed was published.
    assert [t for t, _ in bus.published] == [action_module.TOPIC_ACTION_FAILED]
    failed_ops = [e for e in store["executions"] if e["outcome"] == "failed"]
    assert failed_ops and "connection refused" in failed_ops[-1]["error"]


async def test_partial_failure_records_what_did_execute(monkeypatch) -> None:
    """Two operations where the second fails: the first is still recorded as
    succeeded, the incident lands action_failed, and the failure carries both."""
    store = _patch_db(
        monkeypatch,
        recommended_actions=[
            {"action": "Inspect pump", "priority": 1, "type": "work_order"},
            {"action": "Reorder biocide", "priority": 2, "type": "product_reorder",
             "product_key": "biocide", "quantity": 40},
        ],
    )

    class PartialCmms(FakeCmms):
        async def request_reorder(self, **kwargs):
            self.calls.append(("request_reorder", kwargs))
            raise CmmsError("POST", "/inventory/biocide/reorder", "HTTP 500: boom")

    cmms = PartialCmms()
    agent = action_module.ActionAgent(
        cmms=cmms, bus=FakeBus(), retry_attempts=2, retry_backoff_seconds=0.01)
    summary = await agent.handle({"incident_id": "0b7fd1d4-0000-0000-0000-000000000006"})

    assert summary["status"] == "action_failed"
    succeeded = [e for e in store["executions"] if e["outcome"] == "succeeded"]
    failed = [e for e in store["executions"] if e["outcome"] == "failed"]
    assert len(succeeded) == 1 and succeeded[0]["reference"] == "wo-1"
    assert failed and failed[-1]["operation"] == ACTION_TYPE_PRODUCT_REORDER
