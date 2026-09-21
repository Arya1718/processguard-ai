"""Telemetry API endpoints for the agent service (Prompt 9).

Exposes:
  GET /api/v1/telemetry/summary      -- aggregate view over a time window
  GET /api/v1/incidents/{id}/cost    -- cost breakdown for one incident

The summary endpoint rolls up metrics from the DB (incident counts, agent
outcomes, HITL decisions) and from the in-process Prometheus registry
(token/cost figures that cannot be queried from SQL).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.db import get_pool
from app.core.logging_config import get_logger
from app.core.security import Principal, require_principal

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["telemetry"])


@router.get("/telemetry/summary")
async def telemetry_summary(
    window_hours: int = Query(24, ge=1, le=168, description="Look-back window in hours"),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Aggregate telemetry over the last `window_hours` for the caller's site.

    Requires a valid OIDC token (site-scoped like every other read). Returns
    incident counts, agent outcomes, HITL decision counts, LLM token/cost
    totals from the live Prometheus registry, and per-agent success rates.
    """
    site_id = principal.site_id
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    pool = get_pool()
    async with pool.acquire() as conn:
        incidents = await conn.fetch(
            """
            SELECT "Status", "Severity", "HitlLevel", "RequiredApprovalLevel",
                   "RootCauseHypothesis", "Confidence", "RiskSeverity",
                   "DetectedAt", "ResolvedAt"
            FROM "Incidents"
            WHERE "SiteId" = $1::uuid AND "DetectedAt" >= $2
            """,
            _parse_uuid(site_id), since,
        )

    total_incidents = len(incidents)
    by_status: dict[str, int] = {}
    by_agent_outcome: dict[str, int] = {}
    hitl_decisions: dict[str, int] = {}

    for row in incidents:
        status = row["Status"] or "unknown"
        by_status[status] = by_status.get(status, 0) + 1

        confidence = row["Confidence"]
        if row["RootCauseHypothesis"] is not None and confidence is not None:
            if confidence == 0:
                by_agent_outcome["root_cause__needs_human_review"] = (
                    by_agent_outcome.get("root_cause__needs_human_review", 0) + 1
                )
            else:
                by_agent_outcome["root_cause__accepted"] = (
                    by_agent_outcome.get("root_cause__accepted", 0) + 1
                )

        hitl = row["HitlLevel"] or row["RequiredApprovalLevel"] or 0
        if status == "approved":
            key = f"approved_level_{hitl}"
            hitl_decisions[key] = hitl_decisions.get(key, 0) + 1
        elif status == "rejected":
            key = f"rejected_level_{hitl}"
            hitl_decisions[key] = hitl_decisions.get(key, 0) + 1

    confidence_values = [r["Confidence"] for r in incidents if r["Confidence"] is not None]
    if confidence_values:
        avg_confidence = sum(confidence_values) / len(confidence_values)
        confidence_bucket = {
            "0-20": sum(1 for c in confidence_values if 0 <= c <= 20),
            "21-40": sum(1 for c in confidence_values if 21 <= c <= 40),
            "41-60": sum(1 for c in confidence_values if 41 <= c <= 60),
            "61-80": sum(1 for c in confidence_values if 61 <= c <= 80),
            "81-100": sum(1 for c in confidence_values if 81 <= c <= 100),
        }
    else:
        avg_confidence = 0
        confidence_bucket = {}

    from app.core.observability import (
        LLM_CALL_COUNT,
        LLM_COST_USD,
        LLM_TOKEN_USAGE,
    )

    token_totals: dict[str, int] = {}
    cost_totals: dict[str, float] = {}
    call_counts: dict[str, int] = {}

    for metric in LLM_CALL_COUNT.collect():
        for sample in metric.samples:
            if sample.name == "pgai_llm_calls_total":
                agent = dict(sample.labels).get("agent", "unknown")
                call_counts[agent] = call_counts.get(agent, 0) + int(sample.value)

    for metric in LLM_TOKEN_USAGE.collect():
        for sample in metric.samples:
            if sample.name == "pgai_llm_tokens_total_total":
                agent = dict(sample.labels).get("agent", "unknown")
                token_totals[agent] = token_totals.get(agent, 0) + int(sample.value)

    for metric in LLM_COST_USD.collect():
        for sample in metric.samples:
            if sample.name == "pgai_llm_cost_usd_total_total":
                agent = dict(sample.labels).get("agent", "unknown")
                cost_totals[agent] = cost_totals.get(agent, 0.0) + sample.value

    total_llm_cost = sum(cost_totals.values())
    total_tokens = sum(token_totals.values())

    cost_per_incident = (
        round(total_llm_cost / total_incidents, 6) if total_incidents > 0 else 0
    )

    # Agent success rate (accepted / total attempted)
    accepted = by_agent_outcome.get("root_cause__accepted", 0)
    reviewed = by_agent_outcome.get("root_cause__needs_human_review", 0)
    rc_total = accepted + reviewed
    root_cause_success_rate = round(accepted / rc_total, 4) if rc_total > 0 else None

    return {
        "window_hours": window_hours,
        "site_id": site_id,
        "incidents": {
            "total": total_incidents,
            "by_status": by_status,
        },
        "root_cause_agent": {
            "success_rate": root_cause_success_rate,
            "accepted": accepted,
            "needs_human_review": reviewed,
            "confidence_avg": round(avg_confidence, 1) if confidence_values else None,
            "confidence_distribution": confidence_bucket,
        },
        "hitl_decisions": hitl_decisions,
        "llm": {
            "calls_by_agent": call_counts,
            "tokens_by_agent": token_totals,
            "cost_usd_by_agent": cost_totals,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_llm_cost, 6),
            "cost_per_incident_usd": cost_per_incident,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/incidents/{incident_id}/cost")
async def incident_cost(
    incident_id: str,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Cost breakdown for a single incident: per-agent token counts and USD.

    Costs are derived from the live Prometheus registry (in-process counters
    tagged by agent). If the incident is from a prior process restart and the
    counters have reset, the DB-stored `EstimatedCostUsd` on the incident row
    is used as the authoritative fallback.
    """
    from app.core.db import _parse_uuid

    try:
        incident_uuid = _parse_uuid(incident_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="incident not found")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT "Id", "SiteId", "RootCauseHypothesis", "Confidence", "Status"
            FROM "Incidents"
            WHERE "Id" = $1
            """,
            incident_uuid,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="incident not found")

    from app.core.security import ensure_site
    ensure_site(principal, str(row["SiteId"]))

    # In-memory cost for this incident (tagged by incident_id in the counter
    # labels). Falls back to 0 if the counters were reset since the incident
    # was created (process restart).
    incident_cost = 0.0
    incident_tokens = 0
    incident_calls = 0

    from app.core.observability import (
        INCIDENT_COST_USD,
        LLM_CALL_COUNT,
        LLM_TOKEN_USAGE,
    )

    for metric in INCIDENT_COST_USD.collect():
        for sample in metric.samples:
            if sample.name == "pgai_incident_cost_usd_total_total":
                if dict(sample.labels).get("incident_id") == incident_id:
                    incident_cost += sample.value

    for metric in LLM_TOKEN_USAGE.collect():
        for sample in metric.samples:
            if sample.name == "pgai_llm_tokens_total_total":
                if dict(sample.labels).get("incident_id") == incident_id:
                    incident_tokens += int(sample.value)

    for metric in LLM_CALL_COUNT.collect():
        for sample in metric.samples:
            if sample.name == "pgai_llm_calls_total":
                if dict(sample.labels).get("incident_id") == incident_id:
                    incident_calls += int(sample.value)

    return {
        "incident_id": incident_id,
        "status": row["Status"],
        "confidence": row["Confidence"],
        "has_root_cause": row["RootCauseHypothesis"] is not None,
        "cost_usd": round(incident_cost, 8),
        "total_tokens": incident_tokens,
        "llm_calls": incident_calls,
        "note": (
            "Cost reflects in-memory Prometheus counters since process start. "
            "If the agent service was restarted, the DB-stored EstimatedCostUsd "
            "is authoritative."
        ),
    }


def _parse_uuid(value: str):
    import uuid
    return uuid.UUID(value)
