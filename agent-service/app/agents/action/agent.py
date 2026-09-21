"""Action Agent (Prompt 5) -- the final agent in the roster, and the ONLY
one with write access to an external enterprise system (the mock ERP/CMMS,
stand-in for Buckman's real ERP/CMMS).

ARCHITECTURAL BOUNDARY (enforced in code, see docs/security-model.md):
  * Detection / Knowledge / Root-Cause / Risk / Recommendation / Orchestrator
    only ever READ sensors + knowledge and WRITE to our own incident tables.
  * The Action Agent alone calls app.core.cmms_client (the sole module with
    an outbound write), using SEPARATE credentials (PGAI_CMMS__API_KEY, not
    the DB password, not the LLM key), and ONLY for incidents whose status
    is exactly 'approved' -- a human decision must precede any external
    side effect.

Flow: subscribes to pgai.incident_approved (published by the HITL decision
endpoint), reads the approved recommendation, maps each action to a typed
CMMS operation via agents.action.mapping (deterministic -- never an LLM
decision), executes with bounded retry/backoff, then:

  success   -> status action_taken, CMMS references + audit entry recorded,
               pgai.action_completed published
  failure   -> status action_failed (never silently stuck), failure reason
               recorded, pgai.action_failed published for the dashboard

IDEMPOTENCY: before executing, the agent checks IncidentActions for
succeeded rows for this incident -- a crash-and-restart (or a duplicate
event) can never create a second work order for the same approval.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from app.agents.action.mapping import CmmsOperation, map_recommendation_to_cmms
from app.core.cmms_client import CmmsClient, CmmsError, get_cmms_client
from app.core.db import (
    close_incident_loop,
    get_action_taken_incident_ids,
    get_incident_action_record,
    get_incident_for_decision,
    get_executed_action_kinds,
    record_action_execution,
    save_action_result,
)
from app.core.logging_config import get_logger
from app.eventbus.base import EventBus

logger = get_logger(__name__)

TOPIC_INCIDENT_APPROVED = "pgai.incident_approved"
TOPIC_ACTION_COMPLETED = "pgai.action_completed"
TOPIC_ACTION_FAILED = "pgai.action_failed"

# Closing the loop: how often the agent checks the CMMS for resolved work
# orders tied to action_taken incidents (there is no push channel from the
# CMMS in the mock; a real integration would use a webhook/queue).
RESOLVE_POLL_SECONDS = 10.0


class ActionAgent:
    """Subscribes to approvals; the only agent that writes externally."""

    name = "action"

    def __init__(self, bus: EventBus, cmms: CmmsClient | None = None,
                 retry_attempts: int = 4, retry_backoff_seconds: float = 2.0) -> None:
        self._bus = bus
        # Explicit dependency (injectable for unit tests); None = build from
        # config (fail fast if PGAI_CMMS__* is missing).
        self._cmms = cmms
        self._retry_attempts = retry_attempts
        self._backoff = retry_backoff_seconds
        self._in_flight: set[str] = set()
        self._running = False

    async def start(self) -> None:
        await self._bus.subscribe(TOPIC_INCIDENT_APPROVED, self._on_incident_approved)
        logger.info("Action Agent subscribed to %s", TOPIC_INCIDENT_APPROVED)

    async def stop(self) -> None:
        self._running = False

    async def poll_resolutions_forever(self) -> None:
        """Outcome tracking (closing the learning loop): detect CMMS work
        orders resolved after the incident reached action_taken, move the
        incident to resolved, and write the learned record into
        HistoricalIncidents -- which is what makes the NEXT similar incident
        retrievable by the Knowledge Agent (the episodic-memory story).

        Started as a background task by app.main; DB-only reads here, never
        an external write."""
        self._running = True
        while self._running:
            try:
                await self._check_resolutions()
            except Exception as exc:
                logger.error("Resolution poll failed: %s", exc)
            await asyncio.sleep(RESOLVE_POLL_SECONDS)

    async def _check_resolutions(self) -> None:
        cmms = self._cmms
        if cmms is None:
            from app.core.cmms_client import get_cmms_client

            cmms = get_cmms_client()
        for incident_id in await get_action_taken_incident_ids():
            if incident_id in self._in_flight:
                continue
            record = await get_incident_action_record(incident_id)
            if record is None:
                continue
            for execution in record.get("operations") or []:
                if execution.get("operation") != "work_order" or execution.get("outcome") != "succeeded":
                    continue
                work_order_id = execution.get("reference")
                if not work_order_id:
                    continue
                try:
                    order = await cmms.get_work_order(work_order_id)
                except CmmsError as exc:
                    logger.warning("Resolution check for work order %s failed: %s",
                                   work_order_id, exc)
                    continue
                if order.get("status") != "resolved":
                    continue
                view = await get_incident_for_decision(incident_id)
                if view is None or view.get("status") != "action_taken":
                    continue  # already closed elsewhere (e.g. manual resolve)
                await self._close_loop(incident_id, view, order)
                break

    async def _close_loop(self, incident_id: str, view: dict, order: dict) -> None:
        """A work order tied to this incident was resolved: move the incident
        to resolved and write the learned record into HistoricalIncidents so
        the NEXT similar incident is retrievable by the Knowledge Agent."""
        summary = view.get("anomaly_summary") or {}
        symptom_pattern = [
            f"{s.get('sensor_type')} {s.get('reason', '')}".strip()
            for s in summary.get("sensors") or []
        ]
        actions = [a.get("action") for a in view.get("recommended_actions") or []
                   if isinstance(a, dict) and a.get("action")]
        learned_id = await close_incident_loop(
            incident_id=incident_id,
            equipment_id=view.get("equipment_id") or None,
            equipment_name=order.get("equipmentId") or "unknown equipment",
            symptoms={
                "pattern": symptom_pattern,
                "sensor_count": summary.get("sensor_count"),
            },
            root_cause=view.get("root_cause_hypothesis") or "unrecorded",
            action_taken="; ".join(actions) or order.get("description") or "unrecorded",
            action_worked=True,
            resolution=order.get("resolutionNote") or "work order resolved",
            source=f"HIST-AUTO-{incident_id[:8]} (CMMS work order {order.get('id')})",
            actor="action-agent",
        )
        await self._publish(TOPIC_ACTION_COMPLETED, incident_id,
                            loop_closed=True, learned_incident_id=learned_id,
                            work_order_id=order.get("id"))
        logger.info(
            "Learning loop closed for %s: work order %s resolved, historical "
            "incident %s recorded", incident_id, order.get("id"), learned_id)


    # -- event handling -------------------------------------------------------

    async def _on_incident_approved(self, payload: dict) -> None:
        incident_id = payload.get("incident_id")
        if not incident_id:
            logger.error("incident_approved event missing incident_id: %s", payload)
            return
        if incident_id in self._in_flight:
            return  # duplicate delivery for an incident already executing
        self._in_flight.add(incident_id)
        try:
            await self.handle(payload)
        except Exception as exc:
            logger.error("Action Agent failed for %s: %s", incident_id, exc)
        finally:
            self._in_flight.discard(incident_id)

    async def handle(self, payload: dict) -> dict:
        """Execute the approved recommendation on the CMMS. Returns a summary
        dict; raises nothing (failure is a persisted action_failed status)."""
        incident_id = payload["incident_id"]
        approved_by = payload.get("decided_by") or "unknown"
        decided_at = payload.get("decided_at")
        # Correlation continuity: the approval endpoint propagated the HTTP
        # request's correlation id through the bus event; the CMMS calls (and
        # the CMMS's own logs) carry the same trace id.
        correlation_id = payload.get("correlation_id")

        view = await get_incident_for_decision(incident_id)
        if view is None:
            logger.error("Action Agent: incident %s not found", incident_id)
            return {"incident_id": incident_id, "status": "not_found"}
        if view["status"] != "approved":
            logger.warning(
                "Action Agent: incident %s is '%s', not 'approved'; refusing to act "
                "(external writes require a human approval)", incident_id, view["status"])
            return {"incident_id": incident_id, "status": view["status"], "acted": False}

        # IDEMPOTENCY GUARD: if any operation for this incident already
        # succeeded (crash-and-restart, duplicate event), never create a
        # second work order -- just reconcile the incident status.
        succeeded = await get_executed_action_kinds(incident_id)
        if succeeded:
            logger.info(
                "Action Agent: incident %s already has executed operations %s; "
                "skipping execution (idempotent re-trigger)", incident_id, succeeded)
            if view["status"] != "action_taken":
                await save_action_result(incident_id, "action_taken", {
                    "reconciled": True, "note": "actions already executed before this trigger",
                })
            return {"incident_id": incident_id, "status": "action_taken", "acted": False}

        mapping = map_recommendation_to_cmms({
            "actions": view.get("recommended_actions") or [],
        })
        if not mapping.has_operations:
            logger.warning(
                "Action Agent: incident %s has no executable CMMS operations "
                "(%d unmapped actions); marking action_taken with no external "
                "writes", incident_id, len(mapping.unmapped))
            await save_action_result(incident_id, "action_taken", {
                "note": "no executable CMMS operations in the approved recommendation",
                "unmapped_actions": mapping.unmapped,
            })
            await self._publish(TOPIC_ACTION_COMPLETED, incident_id, executions=[],
                                unmapped=mapping.unmapped)
            return {"incident_id": incident_id, "status": "action_taken", "acted": False}

        logger.info(
            "Action Agent: executing %d CMMS operation(s) for approved incident %s "
            "(approved by %s): %s", len(mapping.operations), incident_id, approved_by,
            [op.kind for op in mapping.operations])

        executions: list[dict] = []
        failures: list[dict] = []
        equipment_id = view.get("equipment_id") or ""
        for operation in mapping.operations:
            try:
                executions.append(await self._execute_with_retry(
                    incident_id, operation, approved_by, equipment_id,
                    correlation_id))
            except CmmsError as exc:
                failures.append({
                    "kind": operation.kind,
                    "description": operation.description,
                    "product_key": operation.product_key,
                    "error": str(exc),
                })
                # Persist each operation's outcome as we go so a mid-flight
                # crash still leaves a truthful record of what executed.
                await record_action_execution(
                    incident_id, operation, "failed", error=str(exc))

        if failures:
            # NEVER leave the incident silently stuck: bounded retries are
            # exhausted -> action_failed with the recorded reasons, and an
            # event the dashboard can surface.
            reason = "; ".join(f["error"] for f in failures)[:900]
            await save_action_result(incident_id, "action_failed", {
                "failures": failures,
                "executed": executions,
                "reason": reason,
            })
            await self._publish(TOPIC_ACTION_FAILED, incident_id,
                                error=reason, failures=failures)
            logger.error("Action Agent: incident %s -> action_failed (%s)",
                         incident_id, reason)
            return {"incident_id": incident_id, "status": "action_failed"}

        await save_action_result(
            incident_id, "action_taken", {"executions": executions},
            executions=executions, approved_by=approved_by, decided_at=decided_at)
        await self._publish(TOPIC_ACTION_COMPLETED, incident_id,
                            executions=executions, unmapped=mapping.unmapped)
        logger.info("Action Agent: incident %s -> action_taken (%d operation(s) executed)",
                    incident_id, len(executions))
        return {"incident_id": incident_id, "status": "action_taken", "executions": executions}

    # -- execution -------------------------------------------------------------

    async def _execute_with_retry(self, incident_id: str, operation: CmmsOperation,
                                  approved_by: str, equipment_id: str,
                                  correlation_id: str | None = None) -> dict:
        """Run one mapped operation against the CMMS with bounded
        retry/backoff (the retry itself lives in CmmsClient; this wrapper
        records the attempt as pending first, so even a hard crash leaves a
        trace of the intended external write)."""
        await record_action_execution(incident_id, operation, "pending")
        cmms = self._cmms or get_cmms_client()
        if operation.kind == "work_order":
            result = await cmms.create_work_order(
                equipment_id=equipment_id,
                description=operation.description,
                priority=_priority_word(operation.priority),
                requested_by="processguard-ai-action-agent",
                source_incident_id=incident_id,
                correlation_id=correlation_id,
            )
        else:  # product_reorder
            result = await cmms.request_reorder(
                product_key=operation.product_key,
                quantity=operation.quantity,
                source_incident_id=incident_id,
                requested_by="processguard-ai-action-agent",
                correlation_id=correlation_id,
            )
        reference = _reference_of(operation, result)
        await record_action_execution(
            incident_id, operation, "succeeded", reference=reference, response=result)
        logger.info("CMMS operation executed for %s: %s -> %s",
                    incident_id, operation.kind, reference)
        return {"kind": operation.kind, "reference": reference,
                "cmms_response": result, "source_action": operation.source_action}

    async def _publish(self, topic: str, incident_id: str, **extra) -> None:
        if self._bus is not None:
            try:
                await self._bus.publish(topic, {"incident_id": incident_id, **extra})
            except Exception as exc:
                # The bus is the visibility channel, not the source of truth:
                # the DB already carries the outcome.
                logger.error("Failed publishing %s for %s: %s", topic, incident_id, exc)


def _priority_word(priority: int) -> str:
    # Deterministic mapping the mock CMMS accepts; documented in
    # docs/erp-integration.md.
    if priority <= 1:
        return "high"
    if priority == 2:
        return "medium"
    return "low"


def _reference_of(operation: CmmsOperation, result: dict) -> str:
    if operation.kind == "work_order":
        return str(result.get("id") or result.get("work_order_id") or "")
    return str(result.get("id") or result.get("reorder_id") or "")
