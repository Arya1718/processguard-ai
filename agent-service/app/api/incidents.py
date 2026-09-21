"""Incident, diagnosis, and HITL decision APIs (consumed by the .NET
middleware -- the single entry point for the frontend).

Prompt 4 additions: the Risk Agent and Recommendation Agent outputs, the
approval/rejection decision endpoints (HITL levels 2-3), and the state
history that makes the orchestrator's state machine inspectable.

Prompt 7 additions (defense in depth -- the agent service is never directly
internet-facing but does NOT trust the middleware blindly):
  * every route validates the forwarded OIDC bearer token independently
    (signature via the provider's JWKS, issuer, audience, lifetime);
  * every incident read re-checks the incident's owning site against the
    token's site_id claim (a second, query-layer site check on top of the
    .NET middleware's clamping);
  * the approve/reject endpoints re-check the role->HITL-level rule from the
    shared config/rbac-policy.json, so a 403 stands even if the middleware
    policy were misconfigured.

Identity for decisions: decided_by ALWAYS comes from the validated token's
preferred_username -- never from the request body.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.db import (
    get_incident,
    get_incident_action_record,
    get_incident_for_decision,
    get_incident_state_history,
    latest_readings_for_site,
    list_incidents,
    record_approval,
    resolve_incident_manually,
)
from app.core.logging_config import correlation_id_var, get_logger
from app.core.runtime import get_event_bus
from app.core.security import (
    Principal,
    ensure_approval_level,
    ensure_role,
    ensure_site,
    get_rbac_policy,
    require_principal,
)
from app.agents.orchestrator.agent import is_valid_transition

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["incidents"])

# Every status the state machine can legally report (docs/state-machine.md,
# including the Prompt 5 execution tail).
STATUS_FILTER = "^(open|investigating|risk_assessed|recommended|awaiting_approval|auto_informed|approved|action_taken|action_failed|rejected|needs_human_review|resolved)$"


@router.get("/incidents")
async def get_incidents(
    status: str | None = Query(None, pattern=STATUS_FILTER),
    site_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_principal),
) -> dict:
    ensure_role(principal, get_rbac_policy().incident_read_roles, "list incidents")
    # Second-layer site enforcement: the query runs against the CALLER'S site
    # only. An explicit site_id for another site is a policy refusal.
    scoped = principal.site_id
    if site_id and site_id.lower() != scoped.lower():
        raise HTTPException(
            status_code=403,
            detail="requested site is outside your assigned site scope",
        )
    items, total = await list_incidents(status, scoped, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/incidents/{incident_id}")
async def get_incident_by_id(
    incident_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    incident = await get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, incident["siteId"])
    return incident


@router.get("/incidents/{incident_id}/evidence")
async def get_incident_evidence(
    incident_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    """Retrieved SOP chunks + matched historical incidents for one incident
    (Knowledge Agent output; None until it has run)."""
    incident = await get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, incident["siteId"])
    return {
        "incidentId": incident["id"],
        "collectedAt": (incident.get("retrievedEvidence") or {}).get("collected_at"),
        "evidence": incident.get("retrievedEvidence"),
    }


@router.get("/incidents/{incident_id}/root-cause")
async def get_incident_root_cause(
    incident_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    """Hypothesis + confidence + cited evidence for one incident (Root-Cause
    Agent output; hypothesis is null until it has run, so dashboards can poll)."""
    incident = await get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, incident["siteId"])
    root_cause = incident.get("rootCause") or {}
    return {
        "incidentId": incident["id"],
        "status": incident["status"],
        "hypothesis": root_cause.get("hypothesis"),
        "confidence": root_cause.get("confidence"),
        "citedEvidence": root_cause.get("citedEvidence") or [],
    }


@router.get("/incidents/{incident_id}/risk")
async def get_incident_risk(
    incident_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    """Risk Agent output: severity, applicable consequences, and the
    deterministic HITL level (None fields until the Risk Agent has run)."""
    incident = await get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, incident["siteId"])
    risk = incident.get("risk") or {}
    return {
        "incidentId": incident["id"],
        "status": incident["status"],
        "severity": risk.get("severity"),
        "consequences": risk.get("consequences") or [],
        "hitlLevel": risk.get("hitlLevel"),
        "requiredApprovalLevel": incident.get("requiredApprovalLevel"),
        "assessedAt": risk.get("assessedAt"),
    }


@router.get("/incidents/{incident_id}/recommendation")
async def get_incident_recommendation(
    incident_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    """Recommendation Agent output: specific actions, the operator-readable
    rationale (which includes the HITL note), and the VISIBLE tool-call log
    that proves the planning behavior. Nulls until the agent has run."""
    incident = await get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, incident["siteId"])
    rec = incident.get("recommendation") or {}
    risk = incident.get("risk") or {}
    return {
        "incidentId": incident["id"],
        "status": incident["status"],
        "actions": rec.get("actions") or [],
        "rationale": rec.get("rationale"),
        "toolCallLog": rec.get("toolCallLog") or [],
        "hitlLevel": risk.get("hitlLevel"),
        "recommendedAt": rec.get("recommendedAt"),
    }


@router.get("/incidents/{incident_id}/state-history")
async def get_state_history(
    incident_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    """Full, ordered state-machine transition history for one incident --
    the inspectable audit trail of the orchestrator's pipeline."""
    incident = await get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, incident["siteId"])
    rows = await get_incident_state_history(incident_id)
    return {
        "incidentId": incident["id"],
        "currentStatus": incident["status"],
        "transitions": rows,
    }


@router.post("/incidents/{incident_id}/approve")
async def approve_incident(
    incident_id: str, body: dict, principal: Principal = Depends(require_principal)
) -> dict:
    """HITL decision (levels 2-3): approve the pending recommendation.

    Moves the incident to 'approved' (the Action Agent executes approved
    actions), records the approver identity and timestamp, and appends the
    full decision chain to AuditLog. Identity comes from the validated
    token; role + site + level are re-checked here as the second layer."""
    return await _record_decision(incident_id, body, decision="approved", principal=principal)


@router.post("/incidents/{incident_id}/reject")
async def reject_incident(
    incident_id: str, body: dict, principal: Principal = Depends(require_principal)
) -> dict:
    """HITL decision (levels 2-3): reject the pending recommendation.

    A justification is REQUIRED (hard requirement at Level 3, enforced
    uniformly). Moves the incident to 'rejected' and logs the full chain."""
    return await _record_decision(incident_id, body, decision="rejected", principal=principal)


async def _record_decision(
    incident_id: str, body: dict, decision: str, principal: Principal
) -> dict:
    # Identity: from the VALIDATED TOKEN, never the body (cannot be spoofed).
    decided_by = principal.username

    view = await get_incident_for_decision(incident_id)
    if view is None:
        raise HTTPException(status_code=404, detail="incident not found")

    # Second-layer site check: the incident must belong to the caller's site.
    ensure_site(principal, str(view.get("site_id") or ""))

    if view["status"] != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"incident status is '{view['status']}'; only 'awaiting_approval' can be {decision}",
        )

    level = view["hitl_level"] or view["required_approval_level"] or 2

    # Second-layer RBAC: the shared policy table decides which roles may
    # decide which HITL levels (Operator = none, MaintenanceEngineer = <=2,
    # PlantManager = <=3). Mirrors the .NET middleware's policy check.
    ensure_approval_level(principal, level)

    justification = body.get("justification")
    if decision == "rejected" and level >= 3 and not (justification or "").strip():
        raise HTTPException(status_code=422, detail="justification is required to reject at HITL level 3")

    decided_at = await record_approval(incident_id, decision, decided_by, level, justification)
    if decided_at is None:
        raise HTTPException(status_code=404, detail="incident not found")

    # Prompt 5: tell the Action Agent (via the EventBus -- the stand-in for
    # Azure Service Bus) that this incident is cleared for external execution.
    # Only an 'approved' decision opens that door; the Action Agent re-checks
    # the DB status itself before acting.
    if decision == "approved":
        bus = get_event_bus()
        if bus is not None:
            try:
                await bus.publish("pgai.incident_approved", {
                    "incident_id": incident_id,
                    "decided_by": decided_by,
                    "decided_at": decided_at.isoformat(),
                    "hitl_level": level,
                    # Propagate the caller's correlation ID so the Action
                    # Agent's CMMS writes (and the CMMS's own logs) carry the
                    # same trace id as the HTTP decision request.
                    "correlation_id": correlation_id_var.get(None),
                })
                logger.info("Published pgai.incident_approved for %s", incident_id)
            except Exception as exc:
                # The DB decision stands even if the bus hiccups; the Action
                # Agent's guard would simply not fire until a re-approval
                # path exists (documented failure mode, docs/failure-modes.md).
                logger.error("Failed publishing pgai.incident_approved for %s: %s",
                             incident_id, exc)

    # Validate the post-decision state against the orchestrator's transition
    # table (defensive: the DB write and this check agree by construction).
    after = await get_incident_for_decision(incident_id)
    if after is not None and not is_valid_transition("awaiting_approval", after["status"]):
        logger.error("Post-approval status '%s' is not a legal transition", after["status"])
    logger.info("HITL decision recorded: incident=%s decision=%s by=%s level=%s",
                incident_id, decision, decided_by, level)
    return {
        "incidentId": incident_id,
        "decision": decision,
        "decidedBy": decided_by,
        "decidedAt": decided_at.isoformat() if decided_at else None,
        "status": after["status"] if after else decision,
        "hitlLevel": level,
        "justification": justification,
    }


@router.get("/incidents/{incident_id}/action")
async def get_incident_action(
    incident_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    """Prompt 5: what the Action Agent executed for this incident -- the CMMS
    work order / reorder references, per-operation outcomes, and status.
    Nulls until the Action Agent has run (dashboards poll)."""
    record = await get_incident_action_record(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, str(record.get("siteId") or ""))
    return record


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(
    incident_id: str, body: dict, principal: Principal = Depends(require_principal)
) -> dict:
    """Prompt 5: manually resolve an incident (for cases with no CMMS work
    order, or as an operator override). Requires a role with resolve
    capability (RBAC table); writes the state-history + audit rows.
    Closing the learning loop via a resolved CMMS work order is the Action
    Agent's job, not this endpoint's."""
    ensure_role(principal, get_rbac_policy().resolve_roles, "resolve incidents")
    note = body.get("note")
    incident = await get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    ensure_site(principal, incident["siteId"])
    if incident["status"] == "resolved":
        raise HTTPException(status_code=409, detail="incident is already resolved")
    if not is_valid_transition(incident["status"], "resolved"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot resolve an incident in status '{incident['status']}'",
        )
    result = await resolve_incident_manually(incident_id, principal.username, note)
    if result is None:
        raise HTTPException(status_code=404, detail="incident not found")
    logger.info("Incident %s manually resolved by %s", incident_id, principal.username)
    return {"incidentId": incident_id, **result}


@router.get("/sites/{site_id}/sensors/latest")
async def get_latest_sensor_readings(
    site_id: str, principal: Principal = Depends(require_principal)
) -> dict:
    ensure_role(principal, get_rbac_policy().incident_read_roles, "view sensor readings")
    if site_id.lower() != principal.site_id.lower():
        raise HTTPException(
            status_code=403,
            detail="requested site is outside your assigned site scope",
        )
    readings = await latest_readings_for_site(site_id)
    return {"site_id": site_id, "sensors": readings}
