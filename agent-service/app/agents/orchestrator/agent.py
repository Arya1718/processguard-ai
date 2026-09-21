"""Orchestrator Agent (Prompt 4) -- the coordination point.

The incident state machine (docs/state-machine.md):

    open -> investigating -> risk_assessed -> recommended
         -> [awaiting_approval | auto_informed] -> approved | rejected -> resolved
    (needs_human_review is a side exit from any pre-approval stage)

Design: the specialist agents (Knowledge, Root-Cause, Risk, Recommendation)
keep doing the work as before, but they no longer advance the pipeline by
implication. Stage COMPLETION is signaled on the EventBus and the
Orchestrator reacts -- no polling loop:

  * pgai.stage_completed {stage: "root_cause", ...}  -- published by the
    Root-Cause Agent after its write;
  * pgai.stage_completed {stage: "risk", hitl_level: N} -- published by the
    Risk Agent;
  * pgai.stage_completed {stage: "recommendation", ...} -- published by the
    Recommendation Agent.

On root_cause completion the Orchestrator triggers the Risk Agent; on risk
completion it triggers the Recommendation Agent; on recommendation
completion it applies the HITL gate (the recommendation agent already moved
the incident to awaiting_approval/auto_informed; the orchestrator validates
that the landing status matches the required approval level and re-applies
it if needed, so the gate is enforced in exactly one place).

The stage-completion events (not the incident rows) are the coordination
signals, which keeps the flow event-driven and inspectable: every pipeline
advance is one pub/sub message plus one state-history row.
"""
from __future__ import annotations

import asyncio

from app.core.db import get_incident_for_decision, transition_incident
from app.core.logging_config import get_logger
from app.eventbus.base import EventBus

logger = get_logger(__name__)

TOPIC_STAGE_COMPLETED = "pgai.stage_completed"

# Legal transitions (docs/state-machine.md). The orchestrator enforces these
# when it drives the pipeline; the API-side approval transitions are checked
# in the endpoints themselves. Prompt 5 appends the execution tail:
# approved -> action_taken | action_failed -> resolved.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "open": {"investigating", "needs_human_review", "resolved"},
    "investigating": {"risk_assessed", "needs_human_review", "resolved"},
    "risk_assessed": {"recommended", "needs_human_review", "resolved"},
    "recommended": {"awaiting_approval", "auto_informed", "needs_human_review", "resolved"},
    "awaiting_approval": {"approved", "rejected", "resolved"},
    "auto_informed": {"resolved"},
    "approved": {"action_taken", "action_failed", "resolved"},
    "action_taken": {"resolved"},
    "action_failed": {"action_taken", "resolved"},  # retry or manual resolution
    "rejected": {"resolved", "investigating"},  # rejected work may be re-investigated
    "needs_human_review": {"investigating", "resolved"},  # human restarts the pipeline
    "resolved": set(),
}


def is_valid_transition(current: str, nxt: str) -> bool:
    return nxt in VALID_TRANSITIONS.get(current, set())


class OrchestratorAgent:
    """Drives the incident state machine from stage-completion events."""

    name = "orchestrator"

    def __init__(self, bus: EventBus, risk_agent=None, recommendation_agent=None) -> None:
        self._bus = bus
        self._risk = risk_agent
        self._recommendation = recommendation_agent
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        await self._bus.subscribe(TOPIC_STAGE_COMPLETED, self._on_stage_completed)
        logger.info("Orchestrator subscribed to %s", TOPIC_STAGE_COMPLETED)

    async def stop(self) -> None:
        pass

    # -- event handling -------------------------------------------------------

    async def _on_stage_completed(self, payload: dict) -> None:
        stage = payload.get("stage")
        incident_id = payload.get("incident_id")
        if not stage or not incident_id:
            logger.error("stage_completed event missing stage/incident_id: %s", payload)
            return
        if incident_id in self._in_flight:
            return  # a pass for this incident is already running
        self._in_flight.add(incident_id)
        try:
            if stage == "root_cause":
                await self._trigger_risk(incident_id)
            elif stage == "risk":
                await self._trigger_recommendation(incident_id)
            elif stage == "recommendation":
                await self._apply_hitl_gate(incident_id, payload)
            else:
                logger.warning("Unknown stage '%s' for %s", stage, incident_id)
        except Exception as exc:
            logger.error("Orchestrator failed handling stage=%s for %s: %s", stage, incident_id, exc)
        finally:
            self._in_flight.discard(incident_id)

    # -- stage drivers ---------------------------------------------------------

    async def _trigger_risk(self, incident_id: str) -> None:
        logger.info("Orchestrator: root cause ready for %s -> triggering Risk Agent", incident_id)
        result = await self._risk.handle({"incident_id": incident_id})
        await self._bus.publish(TOPIC_STAGE_COMPLETED, {
            "stage": "risk",
            "incident_id": incident_id,
            "severity": result["severity"],
            "hitl_level": result["hitl_level"],
        })

    async def _trigger_recommendation(self, incident_id: str) -> None:
        logger.info("Orchestrator: risk assessed for %s -> triggering Recommendation Agent", incident_id)
        result = await self._recommendation.handle({"incident_id": incident_id})
        await self._bus.publish(TOPIC_STAGE_COMPLETED, {
            "stage": "recommendation",
            "incident_id": incident_id,
            "next_status": result["next_status"],
            "hitl_level": result["hitl_level"],
        })

    async def _apply_hitl_gate(self, incident_id: str, payload: dict) -> None:
        """Final gate: verify the landing status matches the required
        approval level, and record the gate decision on the state history."""
        decision = await get_incident_for_decision(incident_id)
        if decision is None:
            logger.error("Orchestrator: incident %s vanished before HITL gate", incident_id)
            return
        level = int(decision.get("required_approval_level") or 2)
        expected = "auto_informed" if level <= 1 else "awaiting_approval"
        actual = decision.get("status")
        if actual != expected:
            logger.warning(
                "Orchestrator: incident %s in '%s' but HITL level %d requires '%s'; re-applying",
                incident_id, actual, level, expected,
            )
            # Re-applying IS a transition (recorded by transition_incident);
            # a matching gate is a no-op -- the recommendation agent already
            # wrote the awaiting_approval/auto_informed history row, so no
            # redundant same-status entry is appended here.
            await transition_incident(
                incident_id, expected, actor="orchestrator",
                reason=f"HITL gate enforcement (level {level})",
                detail={"expected": expected, "actual": actual},
            )
            actual = expected
        logger.info("Orchestrator: HITL gate verified for %s (level %d -> %s)", incident_id, level, actual)
