"""Unit tests for the Recommendation Agent's tool-calling flow (Prompt 4).

Uses a mocked llm_client and (indirectly) the real tool dispatch -- the
tool implementations are monkeypatched -- to prove:
  * the planner actually calls the two typed tools, in order;
  * every call is logged with arguments, result, and round number;
  * the final recommendation is parsed and the HITL note is prepended;
  * an unknown tool surfaces an error to the model, not a silent failure.
"""
from __future__ import annotations

import json

import pytest

import app.agents.recommendation.agent as rec_module
from app.agents.recommendation.agent import (
    TOOL_SCHEMAS,
    RecommendationAgent,
    _parse_recommendation,
)


class FakeToolCallingLLM:
    """Scripted LLM: first round calls tools, second round answers.

    `generate` (used by unit callers) and `chat` (used by the planner loop)
    both return tool-call responses first, then the final text.
    """

    provider = "mock"
    model = "mock-1"

    def __init__(self, tool_rounds: list[list[tuple[str, dict]]], final_text: str):
        self._tool_rounds = list(tool_rounds)
        self._final_text = final_text
        self.calls: list[dict] = []

    def _next_response(self, messages):
        self.calls.append({"messages": messages})
        if self._tool_rounds:
            tools = self._tool_rounds.pop(0)
            tcs = [
                type("TC", (), {"id": f"call_{i}", "name": name, "arguments": args})()
                for i, (name, args) in enumerate(tools)
            ]
            return type("R", (), {"content": "", "provider": self.provider,
                                  "model": self.model, "tool_calls": tcs})()
        return type("R", (), {"content": self._final_text, "provider": self.provider,
                              "model": self.model, "tool_calls": []})()

    async def chat(self, messages, tools=None):
        return self._next_response(messages)

    async def generate(self, system_prompt, user_prompt, tools=None):
        return self._next_response([{"role": "user", "content": user_prompt}])


FINAL_TEXT = """RECOMMENDED ACTIONS:
1. Inspect Cooling Water Pump CP-04 suction strainer and backwash if differential is elevated
2. Reduce loop load by 10% if supply temperature exceeds 40 degC
RATIONALE: The pattern matches the strainer blockage and pump wear seen 17 days ago. Backwashing restored flow quickly last time, so try that first before any mechanical work.
HITL NOTE: Level 2 applies: a human must approve before any action is executed."""

MAINTENANCE = {
    "equipmentId": "e-1",
    "name": "Cooling Water Pump CP-04",
    "lastServicedAt": "2026-05-17T00:00:00+00:00",
    "conditionFlags": ["bearing_wear_suspected", "suction_strainer_differential_elevated"],
}

SIMILAR = [
    {
        "equipmentName": "Cooling Water Pump CP-04",
        "symptoms": ["flow_rate dropped 18%"],
        "rootCause": "Pump degradation + blocked strainer",
        "actionTaken": "Backwashed the suction strainer",
        "actionWorked": True,
        "resolution": "Flow restored within 20 minutes",
        "occurredAt": "2026-09-01T00:00:00+00:00",
        "source": "demo-seed:HIST-2026-0141",
    }
]


@pytest.fixture
def patched_tools(monkeypatch):
    """Replace the DB-backed tool implementations with canned responses,
    recording every invocation."""
    calls: list[tuple[str, dict]] = []

    async def fake_maintenance(equipment_id: str):
        calls.append(("get_equipment_maintenance_status", {"equipment_id": equipment_id}))
        return dict(MAINTENANCE)

    async def fake_similar(equipment_id: str, symptom_pattern: str):
        calls.append(("get_similar_incident_outcomes",
                      {"equipment_id": equipment_id, "symptom_pattern": symptom_pattern}))
        return [dict(SIMILAR[0])]

    monkeypatch.setattr(rec_module, "get_equipment_maintenance_status", fake_maintenance)
    monkeypatch.setattr(rec_module, "get_similar_incident_outcomes", fake_similar)
    return calls


@pytest.fixture
def incident(monkeypatch):
    """Minimal incident + risk state so handle() reaches the planner."""
    async def fake_diagnosis_state(incident_id: str):
        return {
            "anomaly_summary": {"equipment_id": "e-1",
                                 "sensors": [{"sensor_type": "flow_rate", "value": 106.0,
                                              "unit": "m3/h", "reason": "outside normal band"}]},
            "root_cause_hypothesis": "cooling-water pump degradation",
            "confidence": 87,
            "evidence": None,
        }

    async def fake_for_decision(incident_id: str):
        return {
            "status": "investigating",
            "hitl_level": 2,
            "required_approval_level": 2,
            "recommended_actions": None,
            "rationale": None,
            "tool_call_log": None,
            "risk_severity": "high",
            "consequences": [{"consequence": "equipment damage"}],
        }

    saved: dict = {}

    async def fake_save(incident_id, actions, rationale, tool_call_log, next_status):
        saved.update({"actions": actions, "rationale": rationale,
                      "tool_call_log": tool_call_log, "next_status": next_status})
        return True

    monkeypatch.setattr(rec_module, "get_incident_diagnosis_state", fake_diagnosis_state)
    monkeypatch.setattr(rec_module, "get_incident_for_decision", fake_for_decision)
    monkeypatch.setattr(rec_module, "save_recommendation", fake_save)
    return saved


@pytest.mark.asyncio
async def test_planner_calls_tools_in_order_and_logs_them(patched_tools, incident):
    """Tools are invoked before the final answer and every call lands in the
    tool_call_log in order -- the visible proof of planning."""
    llm = FakeToolCallingLLM(
        tool_rounds=[
            [("get_equipment_maintenance_status", {"equipment_id": "e-1"})],
            [("get_similar_incident_outcomes", {"equipment_id": "e-1", "symptom_pattern": "flow vibration"})],
        ],
        final_text=FINAL_TEXT,
    )
    agent = RecommendationAgent(llm, bus=None)
    result = await agent.handle({"incident_id": "inc-1"})

    # Both tools were called, in order, before the final response.
    names = [name for name, _ in patched_tools]
    assert names == ["get_equipment_maintenance_status", "get_similar_incident_outcomes"]
    assert len(llm.calls) == 3  # tool round 1, tool round 2, final answer

    log = incident["tool_call_log"]
    assert [entry["tool"] for entry in log] == [
        "get_equipment_maintenance_status",
        "get_similar_incident_outcomes",
    ]
    assert log[0]["arguments"] == {"equipment_id": "e-1"}
    assert log[1]["arguments"]["symptom_pattern"] == "flow vibration"
    assert log[0]["round"] == 1 and log[1]["round"] == 2
    assert log[0]["result"]["result"]["conditionFlags"] == [
        "bearing_wear_suspected", "suction_strainer_differential_elevated"]
    assert log[1]["result"]["result"][0]["actionWorked"] is True
    assert all(entry["at"] for entry in log)  # every call timestamped

    # The recommendation landed with the HITL note and level-2 status.
    assert incident["next_status"] == "awaiting_approval"
    assert incident["rationale"].startswith("[HITL Level 2 applies")
    assert result["next_status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_level_1_routes_to_auto_informed(patched_tools, incident, monkeypatch):
    """A level-1 recommendation moves straight to auto_informed (no
    approval), still recorded and visible."""
    async def fake_for_decision_low(incident_id: str):
        return {
            "status": "investigating", "hitl_level": 1,
            "required_approval_level": 1, "recommended_actions": None,
            "rationale": None, "tool_call_log": None,
            "risk_severity": "low", "consequences": [],
        }

    monkeypatch.setattr(rec_module, "get_incident_for_decision", fake_for_decision_low)

    llm = FakeToolCallingLLM(tool_rounds=[[("get_equipment_maintenance_status", {"equipment_id": "e-1"})]],
                             final_text=FINAL_TEXT)
    agent = RecommendationAgent(llm, bus=None)
    result = await agent.handle({"incident_id": "inc-1"})
    assert incident["next_status"] == "auto_informed"
    assert incident["rationale"].startswith("[HITL Level 1 applies")
    assert result["hitl_level"] == 1


@pytest.mark.asyncio
async def test_unknown_tool_surfaces_error_to_model(patched_tools, incident):
    """A hallucinated tool name returns an error payload the model can see
    (and that lands in the log), never a silent drop."""
    llm = FakeToolCallingLLM(
        tool_rounds=[[("delete_all_incidents", {})]],
        final_text=FINAL_TEXT,
    )
    agent = RecommendationAgent(llm, bus=None)
    await agent.handle({"incident_id": "inc-1"})

    log = incident["tool_call_log"]
    assert log[0]["tool"] == "delete_all_incidents"
    assert log[0]["result"] == {"ok": False, "error": "unknown tool 'delete_all_incidents'"}
    # The loop continued and still produced the final recommendation.
    assert incident["actions"][0]["action"].startswith("Inspect Cooling Water Pump")


def test_tool_schemas_are_typed_and_complete():
    """The two required tools are declared with typed parameters."""
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert names == {"get_equipment_maintenance_status", "get_similar_incident_outcomes"}
    for schema in TOOL_SCHEMAS:
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert params.get("required"), schema["function"]["name"]


def test_parse_recommendation_handles_malformed_output():
    """Even unparseable output is recorded rather than lost."""
    parsed = _parse_recommendation("I recommend checking the pump soon.")
    assert parsed["actions"][0]["action"].startswith("I recommend checking")
