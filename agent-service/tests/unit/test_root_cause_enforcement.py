"""Unit tests for the Root-Cause Agent's evidence-citation enforcement.

Uses a mocked llm_client (no network): an uncited or fabricated answer must
be rejected, retried once, and never reach the incident record.
"""
from __future__ import annotations

import pytest

from app.agents.root_cause.agent import (
    RootCauseAgent,
    _parse_and_validate,
    _build_user_prompt,
)

ANOMALY = {
    "equipment_id": "e-1",
    "sensors": [
        {"sensor_type": "flow_rate", "value": 106.0, "unit": "m3/h",
         "reason": "outside normal band by 88%", "static_margin": 0.88, "drift_z": 0.2},
        {"sensor_type": "vibration", "value": 4.6, "unit": "mm/s",
         "reason": "outside normal band by 142%", "static_margin": 1.42, "drift_z": 0.6},
    ],
}

EVIDENCE = {
    "sop_chunks": [
        {"docId": "SOP-COOL-014", "title": "Cooling Water Flow and Vibration Response",
         "section": "Immediate response procedure", "score": 0.62,
         "excerpt": "Inspect the pump suction strainer for blockage..."},
    ],
    "matched_history": [
        {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "equipmentName": "Cooling Water Pump CP-04",
         "symptoms": ["flow_rate dropped 18%", "vibration above 3.2 mm/s"],
         "rootCause": "Pump degradation + blocked strainer",
         "resolution": "Backwashed strainer; bearings scheduled", "occurredAt": "2026-09-01T00:00:00+00:00",
         "source": "demo-seed:HIST-2026-0141", "sameEquipment": True},
    ],
}


class MockLLM:
    """Scripted fake LLM: pops canned responses; records calls."""

    provider = "mock"
    model = "mock-1"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def generate(self, system_prompt: str, user_prompt: str, tools=None):
        self.calls.append((system_prompt, user_prompt))
        return type("R", (), {"content": self.responses.pop(0), "provider": self.provider,
                              "model": self.model, "tool_calls": []})()


# ---------------------------------------------------------------------------
# Pure validator tests
# ---------------------------------------------------------------------------

GOOD_ANSWER = """Most likely cause: cooling-water pump degradation with a blocked suction strainer (confidence 87%)
Evidence:
- flow_rate 106.0 m3/h is outside normal band by 88% [sensor reading]
- vibration 4.6 mm/s exceeds the 2.5 mm/s threshold [sensor reading]
- SOP-COOL-014: inspect the pump suction strainer for blockage [SOP citation SOP-COOL-014]
- HIST-2026-0141 shows the same combined flow/vibration signature resolved as pump degradation [historical incident demo-seed:HIST-2026-0141]"""


def test_good_answer_passes_validation() -> None:
    result = _parse_and_validate(GOOD_ANSWER, ANOMALY, EVIDENCE)
    assert result["valid"] is True
    assert result["confidence"] == 87
    assert result["hypothesis"].startswith("cooling-water pump degradation")
    kinds = {c["type"] for c in result["cited"]}
    assert kinds == {"sensor", "sop", "history"}


def test_uncited_point_fails_validation() -> None:
    answer = GOOD_ANSWER + "\n- the impeller is definitely shattered [sensor reading]"
    result = _parse_and_validate(answer, ANOMALY, EVIDENCE)
    # Tagged sensor but mentions no known sensor type value -> unverifiable.
    assert result["valid"] is False


def test_fabricated_sop_id_fails_validation() -> None:
    answer = """Most likely cause: bearing seizure (confidence 90%)
Evidence:
- SOP-FAKE-999 says to replace the bearing immediately [SOP citation SOP-FAKE-999]"""
    result = _parse_and_validate(answer, ANOMALY, EVIDENCE)
    assert result["valid"] is False


def test_answer_without_any_citation_fails() -> None:
    answer = """Most likely cause: cavitation (confidence 75%)
Evidence:
- The pump is clearly cavitating based on my training data."""
    result = _parse_and_validate(answer, ANOMALY, EVIDENCE)
    assert result["valid"] is False


def test_insufficient_evidence_answer_is_accepted_as_needs_review() -> None:
    result = _parse_and_validate(
        "Insufficient evidence for a confident conclusion; human review required.",
        ANOMALY, {"sop_chunks": [], "matched_history": []},
    )
    assert result["valid"] is True
    assert result["insufficient"] is True


def test_hypothesis_line_missing_fails() -> None:
    result = _parse_and_validate("The pump looks bad.", ANOMALY, EVIDENCE)
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# Agent-level retry / fallback tests (mocked LLM, mocked persistence)
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_db(monkeypatch):
    saved = {}

    async def fake_state(incident_id):
        return {"evidence": EVIDENCE, "root_cause_hypothesis": None}

    async def fake_save(incident_id, hypothesis, confidence, cited, status):
        saved.update({"hypothesis": hypothesis, "confidence": confidence,
                      "cited": cited, "status": status})
        return True

    import app.agents.root_cause.agent as mod
    monkeypatch.setattr(mod, "get_incident_diagnosis_state", fake_state)
    monkeypatch.setattr(mod, "save_root_cause", fake_save)
    return saved


@pytest.mark.asyncio
async def test_uncited_llm_response_is_retried_then_falls_back(patched_db) -> None:
    uncited = """Most likely cause: cavitation (confidence 80%)
Evidence:
- Obviously cavitation, trust me."""
    llm = MockLLM([uncited, uncited])
    agent = RootCauseAgent(llm, bus=None)  # bus unused in handle()
    result = await agent.handle("inc-1", ANOMALY)

    assert len(llm.calls) == 2, "exactly one stricter retry expected"
    assert "STRICTER" in llm.calls[1][0], "retry must carry the stricter reminder"
    # The uncited claim NEVER reaches the record:
    assert patched_db["status"] == "needs_human_review"
    assert patched_db["confidence"] == 0
    assert "cavitation" not in patched_db["hypothesis"]
    assert result is None


@pytest.mark.asyncio
async def test_retry_with_cited_answer_succeeds(patched_db) -> None:
    uncited = """Most likely cause: cavitation (confidence 80%)
Evidence:
- Trust me."""
    llm = MockLLM([uncited, GOOD_ANSWER])
    agent = RootCauseAgent(llm, bus=None)
    result = await agent.handle("inc-2", ANOMALY)

    assert result is not None and result["valid"]
    assert patched_db["status"] == "investigating"
    assert patched_db["confidence"] == 87
    assert patched_db["hypothesis"].startswith("cooling-water pump degradation")


@pytest.mark.asyncio
async def test_already_diagnosed_incident_is_skipped(monkeypatch) -> None:
    async def fake_state(incident_id):
        return {"evidence": EVIDENCE, "root_cause_hypothesis": "existing"}

    import app.agents.root_cause.agent as mod
    monkeypatch.setattr(mod, "get_incident_diagnosis_state", fake_state)

    llm = MockLLM([GOOD_ANSWER])
    agent = RootCauseAgent(llm, bus=None)
    result = await agent.handle("inc-3", ANOMALY)
    assert result is None
    assert llm.calls == [], "no LLM call may happen for an already-diagnosed incident"


def test_user_prompt_carries_citable_ids() -> None:
    prompt = _build_user_prompt(ANOMALY, EVIDENCE)
    assert "SOP-COOL-014" in prompt
    assert "HIST-2026-0141" in prompt
    assert "flow_rate" in prompt
