"""Unit tests for the Orchestrator Agent's state machine and event-driven
coordination logic (Prompt 4).

Tests cover:
  - VALID_TRANSITIONS table correctness
  - is_valid_transition() for all legal + illegal pairs
  - _on_stage_completed() routing (root_cause -> risk -> recommendation -> HITL gate)
  - in_flight deduplication (no concurrent handling of two events for same incident)
  - unknown stage warning
  - error handling (agent failure doesn't crash the orchestrator)
  - HITL gate enforcement (expected vs actual status)
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from app.agents.orchestrator.agent import (
    OrchestratorAgent,
    VALID_TRANSITIONS,
    is_valid_transition,
    TOPIC_STAGE_COMPLETED,
)


class FakeRiskAgent:
    def __init__(self, result=None):
        self._result = result or {"severity": "medium", "hitl_level": 2}
        self.calls: list[str] = []

    async def handle(self, payload: dict) -> dict:
        self.calls.append(payload["incident_id"])
        return self._result


class FakeRecommendationAgent:
    def __init__(self, result=None):
        self._result = result or {"next_status": "awaiting_approval", "hitl_level": 2}
        self.calls: list[str] = []

    async def handle(self, payload: dict) -> dict:
        self.calls.append(payload["incident_id"])
        return self._result


class InMemoryBus:
    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.published: list[tuple[str, dict]] = []
        self._started = False

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))
        if self._started:
            for handler in self.handlers.get(topic, []):
                result = handler(payload)
                if __import__("asyncio").iscoroutine(result):
                    await result

    async def subscribe(self, topic: str, handler) -> None:
        self.handlers.setdefault(topic, []).append(handler)

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False


# ---------------------------------------------------------------------------
# Transition table tests
# ---------------------------------------------------------------------------

class TestValidTransitions:
    def test_open_to_investigating(self):
        assert is_valid_transition("open", "investigating") is True

    def test_open_to_needs_human_review(self):
        assert is_valid_transition("open", "needs_human_review") is True

    def test_investigating_to_risk_assessed(self):
        assert is_valid_transition("investigating", "risk_assessed") is True

    def test_risk_assessed_to_recommended(self):
        assert is_valid_transition("risk_assessed", "recommended") is True

    def test_recommended_to_awaiting_approval(self):
        assert is_valid_transition("recommended", "awaiting_approval") is True

    def test_awaiting_approval_to_approved(self):
        assert is_valid_transition("awaiting_approval", "approved") is True

    def test_approved_to_action_taken(self):
        assert is_valid_transition("approved", "action_taken") is True

    def test_action_taken_to_resolved(self):
        assert is_valid_transition("action_taken", "resolved") is True

    def test_action_failed_to_action_taken_retry(self):
        assert is_valid_transition("action_failed", "action_taken") is True

    def test_rejected_to_investigating(self):
        assert is_valid_transition("rejected", "investigating") is True

    def test_needs_human_review_to_investigating(self):
        assert is_valid_transition("needs_human_review", "investigating") is True

    def test_resolved_to_anything_is_false(self):
        assert is_valid_transition("resolved", "investigating") is False
        assert is_valid_transition("resolved", "resolved") is False

    def test_unknown_status_is_false(self):
        assert is_valid_transition("bogus", "resolved") is False

    def test_invalid_transition_rejected(self):
        assert is_valid_transition("open", "resolved") is True  # open can go to resolved (early closure)
        assert is_valid_transition("open", "approved") is False


class TestOrchestratorRouting:
    @pytest.mark.asyncio
    async def test_root_cause_event_triggers_risk(self):
        bus = InMemoryBus()
        risk = FakeRiskAgent()
        rec = FakeRecommendationAgent()
        orch = OrchestratorAgent(bus, risk_agent=risk, recommendation_agent=rec)
        await orch.start()
        await bus.start()

        await bus.publish(TOPIC_STAGE_COMPLETED, {
            "stage": "root_cause",
            "incident_id": "inc-1",
        })

        assert "inc-1" in risk.calls
        assert len(risk.calls) == 1
        # The orchestrator publishes a risk stage_completed event
        assert any(
            p[0] == TOPIC_STAGE_COMPLETED and p[1].get("stage") == "risk"
            for p in bus.published
        )

    @pytest.mark.asyncio
    async def test_risk_event_triggers_recommendation(self):
        bus = InMemoryBus()
        risk = FakeRiskAgent()
        rec = FakeRecommendationAgent()
        orch = OrchestratorAgent(bus, risk_agent=risk, recommendation_agent=rec)
        await orch.start()
        await bus.start()

        await bus.publish(TOPIC_STAGE_COMPLETED, {
            "stage": "risk",
            "incident_id": "inc-1",
            "severity": "high",
            "hitl_level": 3,
        })

        assert "inc-1" in rec.calls
        assert any(
            p[0] == TOPIC_STAGE_COMPLETED and p[1].get("stage") == "recommendation"
            for p in bus.published
        )

    @pytest.mark.asyncio
    async def test_recommendation_event_triggers_hitl_gate(self):
        """The recommendation stage_completion triggers the HITL gate check,
        not a further agent."""
        bus = InMemoryBus()
        risk = FakeRiskAgent()
        rec = FakeRecommendationAgent()
        orch = OrchestratorAgent(bus, risk_agent=risk, recommendation_agent=rec)
        await orch.start()
        await bus.start()

        async def fake_get_incident(incident_id: str, *a, **kw):
            return {"status": "awaiting_approval", "required_approval_level": 3}

        async def fake_transition(*a, **kw):
            pass

        with patch("app.agents.orchestrator.agent.get_incident_for_decision", fake_get_incident), \
             patch("app.agents.orchestrator.agent.transition_incident", fake_transition):

            await bus.publish(TOPIC_STAGE_COMPLETED, {
                "stage": "recommendation",
                "incident_id": "inc-1",
                "next_status": "awaiting_approval",
                "hitl_level": 3,
            })

        # The recommendation stage triggers the HITL gate, not another agent call.
        # The recommendation agent was triggered by the "risk" stage, not here.
        assert risk.calls == []

    @pytest.mark.asyncio
    async def test_unknown_stage_warns_and_does_not_error(self):
        bus = InMemoryBus()
        risk = FakeRiskAgent()
        rec = FakeRecommendationAgent()
        orch = OrchestratorAgent(bus, risk_agent=risk, recommendation_agent=rec)
        await orch.start()
        await bus.start()

        await bus.publish(TOPIC_STAGE_COMPLETED, {
            "stage": "unknown_stage",
            "incident_id": "inc-1",
        })

        # No agents triggered
        assert risk.calls == []
        assert rec.calls == []

    @pytest.mark.asyncio
    async def test_in_flight_deduplication(self):
        """Two concurrent stage-completed events for the same incident must
        not trigger the downstream agent twice."""
        bus = InMemoryBus()
        risk = FakeRiskAgent()
        rec = FakeRecommendationAgent()
        orch = OrchestratorAgent(bus, risk_agent=risk, recommendation_agent=rec)
        await orch.start()
        await bus.start()

        # The in_flight set is added before the handler runs, so a second
        # event for the same incident_id is skipped.
        orch._in_flight.add("inc-dup")  # simulate an in-flight pass

        await bus.publish(TOPIC_STAGE_COMPLETED, {
            "stage": "root_cause",
            "incident_id": "inc-dup",
        })

        assert risk.calls == []  # skipped because inc-dup is in_flight

    @pytest.mark.asyncio
    async def test_missing_stage_and_incident_id_skipped(self):
        bus = InMemoryBus()
        orch = OrchestratorAgent(bus, risk_agent=FakeRiskAgent(), recommendation_agent=FakeRecommendationAgent())
        await orch.start()
        await bus.start()

        await bus.publish(TOPIC_STAGE_COMPLETED, {})
        await bus.publish(TOPIC_STAGE_COMPLETED, {"stage": "root_cause"})

        # No events published downstream
        assert not any(
            p[0] == TOPIC_STAGE_COMPLETED and p[1].get("stage") == "risk"
            for p in bus.published
        )

    @pytest.mark.asyncio
    async def test_orchestrator_error_does_not_crash(self):
        """If a downstream agent raises, the orchestrator logs and continues."""
        bus = InMemoryBus()

        class FailingRiskAgent:
            async def handle(self, payload: dict) -> dict:
                raise RuntimeError("risk agent blew up")

        orch = OrchestratorAgent(bus, risk_agent=FailingRiskAgent(),
                                 recommendation_agent=FakeRecommendationAgent())
        await orch.start()
        await bus.start()

        # Should not raise
        await bus.publish(TOPIC_STAGE_COMPLETED, {
            "stage": "root_cause",
            "incident_id": "inc-crash",
        })
        # The in_flight set should be cleaned up despite the error
        assert "inc-crash" not in orch._in_flight


class TestHITLGate:
    @pytest.mark.asyncio
    async def test_hitl_gate_reapplies_when_status_mismatch(self):
        """If the incident status doesn't match the required HITL level, the
        orchestrator re-applies the expected status."""
        bus = InMemoryBus()
        orch = OrchestratorAgent(bus, risk_agent=FakeRiskAgent(), recommendation_agent=FakeRecommendationAgent())
        await orch.start()
        await bus.start()

        applied_transitions: list[tuple] = []

        import app.agents.orchestrator.agent as orch_mod
        original_get = orch_mod.get_incident_for_decision
        original_transition = orch_mod.transition_incident

        async def fake_get(incident_id: str, *a, **kw):
            return {
                "status": "auto_informed",  # wrong status
                "required_approval_level": 3,  # requires awaiting_approval
            }

        async def fake_transition(*a, **kw):
            applied_transitions.append((a, kw))

        orch_mod.get_incident_for_decision = fake_get
        orch_mod.transition_incident = fake_transition

        try:
            await bus.publish(TOPIC_STAGE_COMPLETED, {
                "stage": "recommendation",
                "incident_id": "inc-gate",
                "next_status": "awaiting_approval",
                "hitl_level": 3,
            })
            assert len(applied_transitions) == 1
            # First positional arg of transition_incident is incident_id
            assert applied_transitions[0][0][0] == "inc-gate"
            # Second positional arg is the expected status
            assert applied_transitions[0][0][1] == "awaiting_approval"
        finally:
            orch_mod.get_incident_for_decision = original_get
            orch_mod.transition_incident = original_transition

    @pytest.mark.asyncio
    async def test_hitl_gate_noop_when_status_matches(self):
        """If the status already matches, no transition is applied."""
        import app.agents.orchestrator.agent as orch_mod
        original_get = orch_mod.get_incident_for_decision
        original_transition = orch_mod.transition_incident

        bus = InMemoryBus()
        orch = OrchestratorAgent(bus, risk_agent=FakeRiskAgent(), recommendation_agent=FakeRecommendationAgent())
        await orch.start()
        await bus.start()

        transition_called = []
        async def fake_get(principal_or_id, *a, **kw):
            """get_incident_for_decision takes (incident_id, principal) or just incident_id."""
            return {
                "status": "awaiting_approval",
                "required_approval_level": 3,
            }

        async def fake_transition(*a, **kw):
            transition_called.append(True)

        orch_mod.get_incident_for_decision = fake_get
        orch_mod.transition_incident = fake_transition

        try:
            await bus.publish(TOPIC_STAGE_COMPLETED, {
                "stage": "recommendation",
                "incident_id": "inc-gate-ok",
                "next_status": "awaiting_approval",
                "hitl_level": 3,
            })
            assert len(transition_called) == 0
        finally:
            orch_mod.get_incident_for_decision = original_get
            orch_mod.transition_incident = original_transition
