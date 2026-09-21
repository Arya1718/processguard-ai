"""Database access (stand-in for Azure SQL).

Local Postgres via asyncpg. The real Azure SQL swap later means changing
this module only -- health endpoints, the Detection Agent and the API
routers are insulated from it.

Role of this module in Prompt 2:
  * init_db_pool / close_db_pool  -- shared asyncpg pool (API + agents)
  * check_postgres                -- health/ready probe
  * seed_demo_data                -- demo site/equipment/sensors (idempotent)
  * read helpers for the incidents + sensors queries exposed via the API
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.core.logging_config import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def init_db_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:  # noqa: SLF001 -- deliberate re-init on restart
        _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    return _pool


async def close_db_pool() -> None:
    global _pool
    if _pool is not None and not _pool._closed:  # noqa: SLF001
        await _pool.close()
    _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized; lifespan startup did not run")
    return _pool


async def check_postgres(dsn: str) -> bool:
    """Standalone probe used by /health/ready (does not need the pool)."""
    conn = None
    try:
        conn = await asyncpg.connect(dsn, timeout=3)
        await conn.fetchval("SELECT 1")
        return True
    except Exception as exc:
        logger.warning("Postgres probe failed: %s", exc)
        return False
    finally:
        if conn is not None:
            await conn.close()


# ---------------------------------------------------------------------------
# Demo seed data (idempotent)
# ---------------------------------------------------------------------------

DEMO_SITE_NAME = "Site 12 - Paper Mill Cooling Tower"
DEMO_EQUIPMENT_NAME = "Cooling Water Pump CP-04"

# Prompt 7: CANONICAL site UUIDs, mirrored by the local OIDC provider's user
# seed (oidc-provider/app/users.py) so the tokens' site_id claims match real
# "Sites" rows. Site 07 exists to PROVE multi-site isolation: its operator
# gets 403 on any Site 12 data (see tests/test_auth_isolation.py).
CANONICAL_SITES: dict[str, str] = {
    "11111111-1111-1111-1111-111111111111": "Site 12 - Paper Mill Cooling Tower",
    "22222222-2222-2222-2222-222222222222": "Site 07 - Leather Tannery Effluent Line",
}
ISOLATION_SITE_NAME = "Site 07 - Leather Tannery Effluent Line"

# (sensor_type, unit, normal_min, normal_max)
DEMO_SENSORS: list[tuple[str, str, float, float]] = [
    ("temperature", "degC", 29.0, 32.0),
    ("ph", "pH", 7.5, 8.2),
    ("flow_rate", "m3/h", 118.0, 132.0),
    ("vibration", "mm/s", 1.0, 2.5),
    ("conductivity", "uS/cm", 950.0, 1100.0),
]


async def seed_demo_data() -> dict:
    """Create the demo site, equipment and sensors if absent. Idempotent.

    Uses the enum type pgai.sensor_type created by the .NET-side migration;
    seeds run through the same database (see docs/ingestion-pipeline.md).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Canonical-UUID sites first (site_id claims in OIDC tokens point
            # at these exact ids); the legacy random-id path is kept for
            # databases seeded before Prompt 7.
            for canonical_id, site_name in CANONICAL_SITES.items():
                existing = await conn.fetchval(
                    'SELECT "Id" FROM "Sites" WHERE "Id" = $1',
                    _parse_uuid(canonical_id),
                )
                if existing is None:
                    name_conflict = await conn.fetchval(
                        'SELECT "Id" FROM "Sites" WHERE "Name" = $1', site_name
                    )
                    if name_conflict is None:
                        await conn.execute(
                            'INSERT INTO "Sites" ("Id", "Name", "CreatedAt", "UpdatedAt") '
                            "VALUES ($1, $2, $3, $3)",
                            _parse_uuid(canonical_id), site_name, _now(),
                        )
                        logger.info("Seeded canonical site %s (%s)", site_name, canonical_id)

            site_id = await conn.fetchval(
                'SELECT "Id" FROM "Sites" WHERE "Name" = $1', DEMO_SITE_NAME
            )
            if site_id is None:
                site_id = await conn.fetchval(
                    'INSERT INTO "Sites" ("Id", "Name", "CreatedAt", "UpdatedAt") '
                    "VALUES ($1, $2, $3, $3) RETURNING \"Id\"",
                    _uuid(), DEMO_SITE_NAME, _now(),
                )
                logger.info("Seeded demo site %s (%s)", DEMO_SITE_NAME, site_id)

            equipment_id = await conn.fetchval(
                'SELECT "Id" FROM "Equipment" WHERE "Name" = $1 AND "SiteId" = $2',
                DEMO_EQUIPMENT_NAME, site_id,
            )
            if equipment_id is None:
                equipment_id = await conn.fetchval(
                    'INSERT INTO "Equipment" ("Id", "Name", "SiteId", "CreatedAt", "UpdatedAt") '
                    "VALUES ($1, $2, $3, $4, $4) RETURNING \"Id\"",
                    _uuid(), DEMO_EQUIPMENT_NAME, site_id, _now(),
                )
                logger.info("Seeded demo equipment %s (%s)", DEMO_EQUIPMENT_NAME, equipment_id)

            for sensor_type, unit, nmin, nmax in DEMO_SENSORS:
                existing = await conn.fetchval(
                    'SELECT "Id" FROM "Sensors" WHERE "EquipmentId" = $1 AND "SensorType" = $2',
                    equipment_id, sensor_type,
                )
                if existing is None:
                    await conn.execute(
                        'INSERT INTO "Sensors" '
                        '("Id", "EquipmentId", "SensorType", "Unit", "NormalMin", "NormalMax", "CreatedAt", "UpdatedAt") '
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $7)",
                        _uuid(), equipment_id, sensor_type, unit, nmin, nmax, _now(),
                    )
            logger.info("Demo sensors ensured for equipment %s", equipment_id)

    return {"site_id": str(site_id), "equipment_id": str(equipment_id)}


# ---------------------------------------------------------------------------
# Read helpers used by the incidents / sensors API routers
# ---------------------------------------------------------------------------

INCIDENT_COLUMNS = (
    'i."Id", i."SiteId", i."EquipmentId", e."Name" AS "EquipmentName", i."Status", i."Severity", '
    'i."DetectedAt", i."AnomalySummary", i."Evidence", i."RootCauseHypothesis", '
    'i."Confidence", i."CitedEvidence", i."RiskSeverity", i."Consequences", '
    'i."HitlLevel", i."RequiredApprovalLevel", i."RecommendedActions", '
    'i."Rationale", i."ToolCallLog", i."RiskAssessedAt", i."RecommendedAt", '
    'i."ResolvedAt", i."CreatedAt", i."UpdatedAt", i."EstimatedCostUsd"'
)


async def list_incidents(
    status: str | None, site_id: str | None, limit: int, offset: int
) -> tuple[list[dict], int]:
    pool = get_pool()
    clauses, params = [], []

    def add(clause: str, value: Any) -> None:
        params.append(value)
        clauses.append(clause.replace("$N", f"${len(params)}"))

    if status:
        add("i.\"Status\" = $N", status)
    if site_id:
        try:
            site_uuid = _parse_uuid(site_id)
        except ValueError:
            return [], 0
        add('i."SiteId" = $N', site_uuid)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = await pool.fetchval(f'SELECT COUNT(*) FROM "Incidents" i {where}', *params)
    rows = await pool.fetch(
        f'SELECT {INCIDENT_COLUMNS} FROM "Incidents" i '
        f'LEFT JOIN "Equipment" e ON e."Id" = i."EquipmentId" {where} '
        f'ORDER BY i."DetectedAt" DESC LIMIT {int(limit)} OFFSET {int(offset)}',
        *params,
    )
    return [_incident_row_to_dict(r) for r in rows], int(total or 0)


async def get_incident(incident_id: str) -> dict | None:
    try:
        uuid_val = _parse_uuid(incident_id)
    except ValueError:
        return None
    pool = get_pool()
    row = await pool.fetchrow(
        f'SELECT {INCIDENT_COLUMNS} FROM "Incidents" i '
        f'LEFT JOIN "Equipment" e ON e."Id" = i."EquipmentId" WHERE i."Id" = $1', uuid_val
    )
    if row is None:
        return None
    incident = _incident_row_to_dict(row)
    incident["evidence"] = await _incident_evidence(pool, incident)
    # Prompt 6: inline the Action Agent's execution record so ONE detail call
    # feeds the whole timeline (the dedicated /action endpoint remains for
    # targeted polling).
    action_record = await get_incident_action_record(incident_id)
    if action_record is not None:
        incident["action"] = {
            "actionExecutedAt": action_record.get("actionExecutedAt"),
            "resolvedAt": action_record.get("resolvedAt"),
            "operations": action_record.get("operations") or [],
        }
    # Prompt 6: the recorded HITL decision(s), so the UI can show WHO
    # approved/rejected and when next to the recommendation.
    approvals = await pool.fetch(
        'SELECT "Decision", "DecidedBy", "DecidedAt", "HitlLevel", "Justification" '
        'FROM "Approvals" WHERE "IncidentId" = $1 ORDER BY "DecidedAt" DESC',
        uuid_val,
    )
    incident["approvals"] = [
        {
            "decision": r["Decision"],
            "decidedBy": r["DecidedBy"],
            "decidedAt": r["DecidedAt"].isoformat() if r["DecidedAt"] else None,
            "hitlLevel": r["HitlLevel"],
            "justification": r["Justification"],
        }
        for r in approvals
    ]
    return incident


async def _incident_evidence(pool: asyncpg.Pool, incident: dict) -> list[dict]:
    """The sensor readings that triggered the incident: every anomalous
    sensor's readings in the 2 minutes before detection (most recent first,
    capped). Sourced from the durable SensorReadings evidence table, so the
    detail endpoint works even after the correlation buffer is long gone."""
    summary = incident.get("anomalySummary") or {}
    sensor_ids = [s.get("sensorId") or s.get("sensor_id") for s in summary.get("sensors", [])]
    sensor_ids = [s for s in sensor_ids if s]
    detected_at = incident.get("detectedAt")
    if not sensor_ids or not detected_at:
        return []
    try:
        ids = [uuid.UUID(s) for s in sensor_ids]
        detected = datetime.fromisoformat(str(detected_at).replace("Z", "+00:00"))
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return []
    rows = await pool.fetch(
        'SELECT r."SensorId" AS sensor_id, s."SensorType" AS sensor_type, s."Unit" AS unit, '
        'r."Value" AS value, r."OccurredAt" AS occurred_at '
        'FROM "SensorReadings" r JOIN "Sensors" s ON s."Id" = r."SensorId" '
        'WHERE r."SensorId" = ANY($1::uuid[]) AND r."OccurredAt" <= $2 '
        'AND r."OccurredAt" >= $2 - interval \'2 minutes\' '
        # Only the readings that actually breached the normal band -- the
        # triggering evidence, not the quiet ramp-up leading in.
        'AND (r."Value" < s."NormalMin" OR r."Value" > s."NormalMax") '
        'ORDER BY r."OccurredAt" DESC LIMIT 100',
        ids,
        detected,
    )
    return [
        {
            "sensorId": str(r["sensor_id"]),
            "sensorType": r["sensor_type"],
            "value": float(r["value"]),
            "unit": r["unit"],
            "occurredAt": r["occurred_at"].isoformat(),
        }
        for r in rows
    ]


async def latest_readings_for_site(site_id: str) -> list[dict]:
    """Latest reading per sensor for a site (dashboard poll endpoint)."""
    try:
        site_uuid = _parse_uuid(site_id)
    except ValueError:
        return []
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (s."Id")
               s."Id" AS sensor_id, s."SensorType" AS sensor_type, s."Unit" AS unit,
               r."Value" AS value, r."OccurredAt" AS occurred_at,
               s."NormalMin" AS normal_min, s."NormalMax" AS normal_max
        FROM "Sensors" s
        JOIN "Equipment" e ON e."Id" = s."EquipmentId"
        LEFT JOIN "SensorReadings" r ON r."SensorId" = s."Id"
        WHERE e."SiteId" = $1
        ORDER BY s."Id", r."OccurredAt" DESC NULLS LAST
        """,
        site_uuid,
    )
    return [
        {
            "sensorId": str(r["sensor_id"]),
            "sensorType": r["sensor_type"],
            "unit": r["unit"],
            "value": r["value"],
            "occurredAt": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            "normalMin": r["normal_min"],
            "normalMax": r["normal_max"],
        }
        for r in rows
    ]


async def save_incident_evidence(incident_id: str, evidence: dict) -> bool:
    """Knowledge Agent output: merge retrieved SOP chunks + matched history
    onto the incident's Evidence jsonb. Returns False if the incident vanished."""
    pool = get_pool()
    result = await pool.execute(
        'UPDATE "Incidents" SET "Evidence" = $2, "UpdatedAt" = $3 WHERE "Id" = $1',
        _parse_uuid(incident_id),
        json.dumps(evidence),
        _now(),
    )
    return result == "UPDATE 1"


async def find_similar_history(
    equipment_id: str, sensor_types: list[str], limit: int = 3
) -> list[dict]:
    """Simple similarity match against HistoricalIncidents: same equipment
    (by EquipmentId) OR overlapping symptom-pattern keywords (sensor types
    named in the recorded Symptoms). Deliberately NOT a second ML model.
    Ordered: same-equipment matches first, then symptom overlap count."""
    if not sensor_types:
        return []
    pool = get_pool()
    rows = await pool.fetch(
        'SELECT "Id", "EquipmentId", "EquipmentName", "Symptoms", '
        '"RootCause", "Resolution", "OccurredAt", "Source", '
        "COALESCE(\"SourceType\", 'illustrative') AS \"SourceType\", \"SourceUrl\" "
        'FROM "HistoricalIncidents"'
    )
    scored: list[tuple[int, int, asyncpg.Record]] = []
    for r in rows:
        symptoms = r["Symptoms"]
        if isinstance(symptoms, (str, bytes, bytearray)):
            try:
                symptoms = json.loads(symptoms)
            except (TypeError, ValueError):
                symptoms = []
        symptom_text = " ".join(symptoms).lower() if isinstance(symptoms, list) else str(symptoms).lower()
        overlap = sum(1 for t in sensor_types if t.replace("_", " ") in symptom_text or t in symptom_text)
        same_equipment = equipment_id and r["EquipmentId"] is not None and str(r["EquipmentId"]) == equipment_id
        if not (same_equipment or overlap):
            continue
        scored.append((1 if same_equipment else 0, overlap, r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [_history_row_to_dict(r, equipment_id) for _, _, r in scored[:limit]]


def _history_row_to_dict(r: asyncpg.Record, equipment_id: str | None) -> dict:
    """Wire shape of a matched historical incident (Knowledge Agent evidence,
    contract-tested in tests/contract/test_api_contracts.py)."""
    return {
        "id": str(r["Id"]),
        "equipmentName": r["EquipmentName"],
        "symptoms": (json.loads(r["Symptoms"]) if isinstance(r["Symptoms"], str) else r["Symptoms"]),
        "rootCause": r["RootCause"],
        "resolution": r["Resolution"],
        "occurredAt": r["OccurredAt"].isoformat(),
        "source": r["Source"],
        # Prompt 8 provenance: public_real rows carry a working citation
        # URL; illustrative rows are authored for this project.
        "sourceType": r["SourceType"],
        "sourceUrl": r["SourceUrl"],
        "sameEquipment": bool(equipment_id and r["EquipmentId"] is not None and str(r["EquipmentId"]) == equipment_id),
    }


async def get_incident_diagnosis_state(incident_id: str) -> dict | None:
    """What the diagnosis pipeline needs in one read: current evidence (as
    written by the Knowledge Agent) and whether a root cause already exists."""
    try:
        uuid_val = _parse_uuid(incident_id)
    except ValueError:
        return None
    pool = get_pool()
    row = await pool.fetchrow(
        'SELECT "Evidence", "RootCauseHypothesis", "AnomalySummary", "Confidence" FROM "Incidents" WHERE "Id" = $1',
        uuid_val,
    )
    if row is None:
        return None
    evidence = row["Evidence"]
    if isinstance(evidence, (str, bytes, bytearray)):
        evidence = json.loads(evidence) if evidence else None
    summary = row["AnomalySummary"]
    if isinstance(summary, (str, bytes, bytearray)):
        summary = json.loads(summary) if summary else None
    sensor_count = len((summary or {}).get("sensors") or [])
    return {
        "evidence": evidence,
        "root_cause_hypothesis": row["RootCauseHypothesis"],
        "sensor_count": sensor_count,
        "anomaly_summary": summary,
        "confidence": row["Confidence"],
    }


async def save_root_cause(
    incident_id: str,
    hypothesis: str,
    confidence: int,
    cited_evidence: list[dict],
    status: str,
) -> bool:
    """Root-Cause Agent output: hypothesis + confidence + per-claim citations,
    plus the state-machine transition to `investigating` (or the
    `needs_human_review` side-exit when the answer fails citation checks).
    All other state-machine transitions go through transition_incident()."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                'UPDATE "Incidents" SET "RootCauseHypothesis" = $2, "Confidence" = $3, '
                '"CitedEvidence" = $4, "Status" = $5, "UpdatedAt" = $6 WHERE "Id" = $1',
                _parse_uuid(incident_id),
                hypothesis,
                int(confidence),
                json.dumps(cited_evidence),
                status,
                _now(),
            )
            if result != "UPDATE 1":
                return False
            # The open -> investigating transition (or the
            # needs_human_review side-exit) is part of the documented state
            # machine, so it gets its own history row like every other move.
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), _parse_uuid(incident_id), None, status,
                "root-cause-agent",
                f"diagnosis recorded (confidence {int(confidence)}%)",
                json.dumps({"confidence": int(confidence), "cited_points": len(cited_evidence)}),
                _now(),
            )
    return True


async def add_incident_cost(incident_id: str, cost_usd: float) -> bool:
    """Prompt 9: accumulate estimated LLM cost on the Incidents row.
    Uses a simple additive UPDATE so the agent-service and middleware can
    both read the same running total."""
    pool = get_pool()
    result = await pool.execute(
        'UPDATE "Incidents" SET "EstimatedCostUsd" = ('
        'COALESCE("EstimatedCostUsd", 0) + $2), "UpdatedAt" = $3 WHERE "Id" = $1',
        _parse_uuid(incident_id),
        cost_usd,
        _now(),
    )
    return result == "UPDATE 1"


# ---------------------------------------------------------------------------
# Prompt 4: state machine, risk, recommendation, HITL, audit
# ---------------------------------------------------------------------------

async def record_state_transition(
    incident_id: str,
    from_status: str | None,
    to_status: str,
    actor: str = "system",
    reason: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append one row to IncidentStateHistory (the inspectable state machine)."""
    pool = get_pool()
    await pool.execute(
        'INSERT INTO "IncidentStateHistory" '
        '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        _uuid(), _parse_uuid(incident_id), from_status, to_status,
        actor, reason, json.dumps(detail) if detail else None, _now(),
    )


async def transition_incident(
    incident_id: str,
    to_status: str,
    actor: str = "system",
    reason: str | None = None,
    detail: dict | None = None,
) -> bool:
    """Move an incident to `to_status` and record the transition atomically.

    This is the ONLY sanctioned way to change an incident's status after
    creation (aside from the detection upsert and the root-cause write).
    Returns False if the incident does not exist."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'UPDATE "Incidents" SET "Status" = $2, "UpdatedAt" = $3 WHERE "Id" = $1 RETURNING "Status"',
                _parse_uuid(incident_id), to_status, _now(),
            )
            if row is None:
                return False
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), _parse_uuid(incident_id), None, to_status,
                actor, reason, json.dumps(detail) if detail else None, _now(),
            )
    return True


async def get_incident_state_history(incident_id: str) -> list[dict]:
    """Full ordered transition history for one incident."""
    pool = get_pool()
    rows = await pool.fetch(
        'SELECT "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt" '
        'FROM "IncidentStateHistory" WHERE "IncidentId" = $1 ORDER BY "ChangedAt", "Id"',
        _parse_uuid(incident_id),
    )
    return [
        {
            "from": r["FromStatus"],
            "to": r["ToStatus"],
            "actor": r["Actor"],
            "reason": r["Reason"],
            "detail": _json_or_none(r["Detail"]),
            "changedAt": r["ChangedAt"].isoformat(),
        }
        for r in rows
    ]


async def save_risk_assessment(
    incident_id: str,
    severity: str,
    consequences: list[dict],
    hitl_level: int,
    required_approval_level: int,
) -> bool:
    """Risk Agent output + transition to risk_assessed."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'UPDATE "Incidents" SET "RiskSeverity" = $2, "Consequences" = $3, '
                '"HitlLevel" = $4, "RequiredApprovalLevel" = $5, "RiskAssessedAt" = $6, '
                '"Status" = \'risk_assessed\', "UpdatedAt" = $6 WHERE "Id" = $1 RETURNING "Status"',
                _parse_uuid(incident_id), severity, json.dumps(consequences),
                int(hitl_level), int(required_approval_level), _now(),
            )
            if row is None:
                return False
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, 'risk_assessed', $4, $5, $6, $7)",
                _uuid(), _parse_uuid(incident_id), None,
                "risk-agent",
                f"severity={severity}, hitl_level={hitl_level}",
                json.dumps({"severity": severity, "hitl_level": hitl_level,
                            "consequences": [c.get("consequence") for c in consequences]}),
                _now(),
            )
    return True


async def save_recommendation(
    incident_id: str,
    actions: list[dict],
    rationale: str,
    tool_call_log: list[dict],
    next_status: str,
) -> bool:
    """Recommendation Agent output + transition to the HITL stage
    (awaiting_approval for levels 2-3, auto_informed for level 1)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Stage 1: land on 'recommended' (the documented state machine
            # passes through it before the HITL gate routes the incident).
            row = await conn.fetchrow(
                'UPDATE "Incidents" SET "RecommendedActions" = $2, "Rationale" = $3, '
                '"ToolCallLog" = $4, "RecommendedAt" = $5, "Status" = \'recommended\', "UpdatedAt" = $5 '
                'WHERE "Id" = $1 RETURNING "Status"',
                _parse_uuid(incident_id), json.dumps(actions), rationale,
                json.dumps(tool_call_log), _now(),
            )
            if row is None:
                return False
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, 'recommended', $4, $5, $6, $7)",
                _uuid(), _parse_uuid(incident_id), None,
                "recommendation-agent",
                "recommendation recorded",
                json.dumps({"actions": [a.get("action") for a in actions]}),
                _now(),
            )
            # Stage 2: the HITL gate routes to awaiting_approval / auto_informed.
            await conn.execute(
                'UPDATE "Incidents" SET "Status" = $2, "UpdatedAt" = $3 WHERE "Id" = $1',
                _parse_uuid(incident_id), next_status, _now(),
            )
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), _parse_uuid(incident_id), "recommended", next_status,
                "hitl-gate",
                f"hitl routing: {next_status}",
                json.dumps({"next_status": next_status}),
                _now(),
            )
    return True


async def get_incident_for_decision(incident_id: str) -> dict | None:
    """Everything the HITL decision endpoints need to validate the gate."""
    try:
        uuid_val = _parse_uuid(incident_id)
    except ValueError:
        return None
    pool = get_pool()
    row = await pool.fetchrow(
        'SELECT "Id", "SiteId", "EquipmentId", "Status", "HitlLevel", "RequiredApprovalLevel", "RecommendedActions", '
        '"Rationale", "ToolCallLog", "RiskSeverity", "RootCauseHypothesis", "Confidence", "AnomalySummary" '
        'FROM "Incidents" WHERE "Id" = $1',
        uuid_val,
    )
    if row is None:
        return None
    return {
        "site_id": str(row["SiteId"]),
        "status": row["Status"],
        "equipment_id": str(row["EquipmentId"]),
        "hitl_level": row["HitlLevel"],
        "required_approval_level": row["RequiredApprovalLevel"],
        "recommended_actions": _json_or_none(row["RecommendedActions"]),
        "rationale": row["Rationale"],
        "tool_call_log": _json_or_none(row["ToolCallLog"]),
        "risk_severity": row["RiskSeverity"],
        "root_cause_hypothesis": row["RootCauseHypothesis"],
        "confidence": row["Confidence"],
        "anomaly_summary": _json_or_none(row["AnomalySummary"]),
    }


async def record_approval(
    incident_id: str,
    decision: str,
    decided_by: str,
    hitl_level: int | None,
    justification: str | None,
) -> datetime | None:
    """HITL decision: insert the Approval row, move the incident to
    approved/rejected, and append the FULL audit chain (recommendation ->
    risk -> approval request -> approver -> decision) to AuditLog.
    Returns the server-side decision timestamp, or None if the incident
    does not exist."""
    pool = get_pool()
    incident_uuid = _parse_uuid(incident_id)
    decided_at = _now()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'UPDATE "Incidents" SET "Status" = $2, "UpdatedAt" = $3 WHERE "Id" = $1 '
                'RETURNING "Status", "RecommendedActions", "Rationale", "ToolCallLog", "RiskSeverity", "Consequences", "HitlLevel"',
                incident_uuid, decision, decided_at,
            )
            if row is None:
                return None
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), incident_uuid, None, decision,
                decided_by,
                f"hitl_level={hitl_level}",
                json.dumps({"justification": justification}),
                decided_at,
            )
            # NOTE: asyncpg deduces one type per parameter -- never reuse a
            # placeholder across a varchar and a timestamptz column (that
            # raises AmbiguousParameterError); DecidedAt/CreatedAt/UpdatedAt
            # share $6 deliberately (all timestamptz).
            await conn.execute(
                'INSERT INTO "Approvals" '
                '("Id", "Name", "IncidentId", "Decision", "DecidedBy", "DecidedAt", "HitlLevel", "Justification", "CreatedAt", "UpdatedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $6, $6)",
                _uuid(), f"{decision} by {decided_by}", incident_uuid,
                decision, decided_by, decided_at, hitl_level, justification,
            )
            # Full, unsanitized chain to the audit log (Level 3 requirement,
            # applied uniformly): recommendation -> risk score -> approval
            # request -> approver identity -> decision.
            await conn.execute(
                'INSERT INTO "AuditLog" '
                '("Id", "Name", "Action", "IncidentId", "Actor", "Detail", "CreatedAt", "UpdatedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), f"hitl_{decision}", f"hitl_{decision}", incident_uuid,
                decided_by,
                json.dumps({
                    "decision": decision,
                    "hitl_level": hitl_level,
                    "justification": justification,
                    "chain": {
                        "recommendation": {
                            "actions": _json_or_none(row["RecommendedActions"]),
                            "rationale": row["Rationale"],
                            "tool_call_log": _json_or_none(row["ToolCallLog"]),
                        },
                        "risk": {
                            "severity": row["RiskSeverity"],
                            "consequences": _json_or_none(row["Consequences"]),
                            "hitl_level": row["HitlLevel"],
                        },
                        "approval_request": {"required_level": row["HitlLevel"]},
                        "approver": decided_by,
                        "decision": decision,
                    },
                }),
                decided_at,
                decided_at,
            )
    return decided_at


def _json_or_none(value):
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value) if value else None
    return value


async def get_equipment_maintenance_status(equipment_id: str) -> dict | None:
    """Tool data source for the Recommendation Agent: last serviced date and
    current condition flags. (Local stand-in for a CMMS integration; the
    equipment row carries seeded realistic data.)"""
    try:
        uuid_val = _parse_uuid(equipment_id)
    except ValueError:
        return None
    pool = get_pool()
    row = await pool.fetchrow(
        'SELECT "Id", "Name", "LastServicedAt", "ConditionFlags" FROM "Equipment" WHERE "Id" = $1',
        uuid_val,
    )
    if row is None:
        return None
    return {
        "equipmentId": str(row["Id"]),
        "name": row["Name"],
        "lastServicedAt": row["LastServicedAt"].isoformat() if row["LastServicedAt"] else None,
        "conditionFlags": _json_or_none(row["ConditionFlags"]) or [],
    }


async def get_similar_incident_outcomes(equipment_id: str, symptom_pattern: str) -> list[dict]:
    """Tool data source: what action was taken last time for similar symptoms
    on this equipment and whether it worked (from HistoricalIncidents)."""
    like = f"%{symptom_pattern.lower()}%"
    pool = get_pool()
    rows = await pool.fetch(
        'SELECT "EquipmentName", "Symptoms", "RootCause", "ActionTaken", "ActionWorked", "Resolution", "OccurredAt", "Source", '
        "COALESCE(\"SourceType\", 'illustrative') AS \"SourceType\", \"SourceUrl\" "
        'FROM "HistoricalIncidents" '
        'WHERE ("EquipmentId" = $1::uuid OR $1::uuid IS NULL) '
        'AND (LOWER("Symptoms"::text) LIKE $2 OR LOWER("RootCause") LIKE $2 OR LOWER("ActionTaken") LIKE $2) '
        'ORDER BY "OccurredAt" DESC LIMIT 5',
        equipment_id if equipment_id else None,
        like,
    )
    return [
        {
            "equipmentName": r["EquipmentName"],
            "symptoms": _json_or_none(r["Symptoms"]),
            "rootCause": r["RootCause"],
            "actionTaken": r["ActionTaken"],
            "actionWorked": r["ActionWorked"],
            "resolution": r["Resolution"],
            "occurredAt": r["OccurredAt"].isoformat(),
            "source": r["Source"],
            "sourceType": r["SourceType"],
            "sourceUrl": r["SourceUrl"],
        }
        for r in rows
    ]


def _incident_row_to_dict(row: asyncpg.Record) -> dict:
    """camelCase keys: this is the wire contract consumed by the .NET
    middleware DTOs and the frontend (see docs/anomaly-event-schema.md for
    the nested anomalySummary shape, which keeps its documented keys)."""
    summary = _json_or_none(row["AnomalySummary"])
    cited = _json_or_none(row["CitedEvidence"])
    consequences = _json_or_none(row["Consequences"])
    actions = _json_or_none(row["RecommendedActions"])
    tool_log = _json_or_none(row["ToolCallLog"])
    return {
        "id": str(row["Id"]),
        "siteId": str(row["SiteId"]),
        "equipmentId": str(row["EquipmentId"]),
        "equipmentName": row["EquipmentName"],
        "status": row["Status"],
        "severity": row["Severity"],
        "detectedAt": row["DetectedAt"].isoformat() if row["DetectedAt"] else None,
        "anomalySummary": summary,
        # Prompt 3: retrieved evidence + root-cause conclusion.
        "retrievedEvidence": _json_or_none(row["Evidence"]),
        "rootCause": (
            {
                "hypothesis": row["RootCauseHypothesis"],
                "confidence": row["Confidence"],
                "citedEvidence": cited,
            }
            if row["RootCauseHypothesis"] is not None
            else None
        ),
        # Prompt 4: risk + recommendation + HITL gating.
        "risk": (
            {
                "severity": row["RiskSeverity"],
                "consequences": consequences,
                "hitlLevel": row["HitlLevel"],
                "assessedAt": row["RiskAssessedAt"].isoformat() if row["RiskAssessedAt"] else None,
            }
            if row["RiskSeverity"] is not None
            else None
        ),
        "recommendation": (
            {
                "actions": actions,
                "rationale": row["Rationale"],
                "toolCallLog": tool_log,
                "recommendedAt": row["RecommendedAt"].isoformat() if row["RecommendedAt"] else None,
            }
            if row["RecommendedActions"] is not None
            else None
        ),
        "requiredApprovalLevel": row["RequiredApprovalLevel"],
        "estimatedCostUsd": float(row["EstimatedCostUsd"]) if row["EstimatedCostUsd"] is not None else None,
        "resolvedAt": row["ResolvedAt"].isoformat() if row["ResolvedAt"] else None,
        "createdAt": row["CreatedAt"].isoformat() if row["CreatedAt"] else None,
        "updatedAt": row["UpdatedAt"].isoformat() if row["UpdatedAt"] else None,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid():
    import uuid

    return uuid.uuid4()


def _parse_uuid(value: str):
    import uuid

    return uuid.UUID(value)


# ---------------------------------------------------------------------------
# Prompt 5: Action Agent + outcome tracking (closing the learning loop)
# ---------------------------------------------------------------------------

async def get_executed_action_kinds(incident_id: str) -> list[str]:
    """Kinds of CMMS operations that already SUCCEEDED for this incident.

    The Action Agent's idempotency guard: a crash-and-restart or a duplicate
    pgai.incident_approved delivery must never create a second work order
    for the same approval."""
    pool = get_pool()
    try:
        incident_uuid = _parse_uuid(incident_id)
    except ValueError:
        return []
    rows = await pool.fetch(
        'SELECT DISTINCT "Operation" FROM "IncidentActions" '
        'WHERE "IncidentId" = $1 AND "Outcome" = \'succeeded\'',
        incident_uuid,
    )
    return [r["Operation"] for r in rows]


async def record_action_execution(
    incident_id: str,
    operation,
    outcome: str,
    reference: str | None = None,
    response: dict | None = None,
    error: str | None = None,
) -> None:
    """One row per CMMS operation attempt (pending -> succeeded/failed).

    `operation` is an agents.action.mapping.CmmsOperation; passed loosely to
    avoid a circular import (the mapping module has no db dependency)."""
    pool = get_pool()
    try:
        incident_uuid = _parse_uuid(incident_id)
    except ValueError:
        return
    detail = {
        "product_key": getattr(operation, "product_key", "") or None,
        "quantity": getattr(operation, "quantity", 0) or None,
        "priority": getattr(operation, "priority", None),
        "source_action": getattr(operation, "source_action", "") or None,
        "response": response,
        "error": error,
    }
    await pool.execute(
        'INSERT INTO "IncidentActions" '
        '("Id", "IncidentId", "Operation", "Description", "Reference", "Outcome", "Detail", "AttemptedAt", "CompletedAt") '
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)",
        _uuid(), incident_uuid, getattr(operation, "kind", "unknown"),
        getattr(operation, "description", "") or getattr(operation, "source_action", ""),
        reference, outcome, json.dumps({k: v for k, v in detail.items() if v is not None}),
        _now(),
    )


async def save_action_result(
    incident_id: str,
    status: str,
    detail: dict,
    executions: list[dict] | None = None,
    approved_by: str | None = None,
    decided_at: str | None = None,
) -> bool:
    """Write the Action Agent's outcome: the incident status move
    (action_taken / action_failed), the per-execution jsonb, the state-history
    row, and -- on success -- the FULL external-write audit chain (who
    approved -> what was recommended -> what was executed -> CMMS reference)."""
    pool = get_pool()
    try:
        incident_uuid = _parse_uuid(incident_id)
    except ValueError:
        return False
    now = _now()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'UPDATE "Incidents" SET "Status" = $2, "UpdatedAt" = $3 WHERE "Id" = $1 '
                'RETURNING "Id"',
                incident_uuid, status, now,
            )
            if row is None:
                return False
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), incident_uuid, None, status,
                "action-agent",
                (detail.get("reason") or "external actions executed")[:300],
                json.dumps(detail), now,
            )
            if status == "action_taken" and executions is not None:
                await conn.execute(
                    'UPDATE "Incidents" SET "ActionExecutedAt" = $2 WHERE "Id" = $1',
                    incident_uuid, now,
                )
                # Full, unsanitized chain for the external write (the audit
                # counterpart of record_approval's decision chain).
                await conn.execute(
                    'INSERT INTO "AuditLog" '
                    '("Id", "Name", "Action", "IncidentId", "Actor", "Detail", "CreatedAt", "UpdatedAt") '
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    _uuid(), "action_executed", "action_executed", incident_uuid,
                    "action-agent",
                    json.dumps({
                        "approved_by": approved_by,
                        "approved_at": decided_at,
                        "executed": executions,
                        "detail": detail,
                    }),
                    now, now,
                )
    return True


async def resolve_incident_manually(incident_id: str, resolved_by: str,
                                    note: str | None) -> dict | None:
    """POST /incidents/{id}/resolve: manual resolution (no CMMS work order,
    or an operator override). Appends the state-history + audit rows."""
    pool = get_pool()
    try:
        incident_uuid = _parse_uuid(incident_id)
    except ValueError:
        return None
    now = _now()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'UPDATE "Incidents" SET "Status" = \'resolved\', "ResolvedAt" = $2, "UpdatedAt" = $2 '
                'WHERE "Id" = $1 RETURNING "Id", "Status"',
                incident_uuid, now,
            )
            if row is None:
                return None
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, 'resolved', $4, $5, $6, $7)",
                _uuid(), incident_uuid, None, resolved_by,
                "manual resolution", json.dumps({"note": note}) if note else None, now,
            )
            await conn.execute(
                'INSERT INTO "AuditLog" '
                '("Id", "Name", "Action", "IncidentId", "Actor", "Detail", "CreatedAt", "UpdatedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), "incident_resolved", "incident_resolved", incident_uuid,
                resolved_by, json.dumps({"note": note}) if note else None, now, now,
            )
    return {"status": "resolved", "resolvedAt": now.isoformat()}


async def record_learned_incident(
    equipment_id: str | None,
    equipment_name: str,
    symptoms: dict,
    root_cause: str,
    action_taken: str,
    action_worked: bool,
    resolution: str,
    source: str,
) -> str | None:
    """Close the learning loop: after a work order tied to an incident is
    resolved, write the incident into HistoricalIncidents so the Knowledge
    Agent can retrieve 'this matches a prior incident' on the NEXT one
    (the episodic-memory story)."""
    pool = get_pool()
    try:
        equipment_uuid = _parse_uuid(equipment_id) if equipment_id else None
    except ValueError:
        equipment_uuid = None
    row = await pool.fetchrow(
        'INSERT INTO "HistoricalIncidents" '
        '("Id", "EquipmentId", "EquipmentName", "Symptoms", "RootCause", "ActionTaken", '
        '"ActionWorked", "Resolution", "OccurredAt", "Source", "SourceType", "CreatedAt", "UpdatedAt") '
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'illustrative', $9, $9) RETURNING \"Id\"",
        _uuid(), equipment_uuid, equipment_name, json.dumps(symptoms), root_cause,
        action_taken, action_worked, resolution, _now(), source,
    )
    return str(row["Id"]) if row else None


async def get_incident_action_record(incident_id: str) -> dict | None:
    """What was executed, the CMMS references, and the current status
    (GET /incidents/{id}/action)."""
    try:
        incident_uuid = _parse_uuid(incident_id)
    except ValueError:
        return None
    pool = get_pool()
    incident = await pool.fetchrow(
        'SELECT "Status", "ActionExecutedAt", "ResolvedAt" FROM "Incidents" WHERE "Id" = $1',
        incident_uuid,
    )
    if incident is None:
        return None
    actions = await pool.fetch(
        'SELECT "Operation", "Description", "Reference", "Outcome", "Detail", '
        '"AttemptedAt", "CompletedAt" FROM "IncidentActions" '
        'WHERE "IncidentId" = $1 ORDER BY "AttemptedAt", "Id"',
        incident_uuid,
    )
    incident_site = await pool.fetchval(
        'SELECT "SiteId" FROM "Incidents" WHERE "Id" = $1', incident_uuid
    )
    return {
        "incidentId": str(incident_uuid),
        "siteId": str(incident_site) if incident_site else None,
        "status": incident["Status"],
        "actionExecutedAt": incident["ActionExecutedAt"].isoformat() if incident["ActionExecutedAt"] else None,
        "resolvedAt": incident["ResolvedAt"].isoformat() if incident["ResolvedAt"] else None,
        "operations": [
            {
                "operation": r["Operation"],
                "description": r["Description"],
                "reference": r["Reference"],
                "outcome": r["Outcome"],
                "detail": _json_or_none(r["Detail"]),
                "attemptedAt": r["AttemptedAt"].isoformat(),
                "completedAt": r["CompletedAt"].isoformat() if r["CompletedAt"] else None,
            }
            for r in actions
        ],
    }


async def get_action_taken_incident_ids() -> list[str]:
    """Incidents whose CMMS work is done and awaiting outcome confirmation
    (the Action Agent's resolution poll iterates these)."""
    pool = get_pool()
    rows = await pool.fetch(
        'SELECT "Id" FROM "Incidents" WHERE "Status" = \'action_taken\'')
    return [str(r["Id"]) for r in rows]


async def close_incident_loop(
    incident_id: str,
    equipment_id: str | None,
    equipment_name: str,
    symptoms: dict,
    root_cause: str,
    action_taken: str,
    action_worked: bool,
    resolution: str,
    source: str,
    actor: str = "action-agent",
) -> str | None:
    """A CMMS work order tied to this incident was resolved: mark the
    incident resolved (state history + audit) and write the learned record
    into HistoricalIncidents. Closing the loop is IDEMPOTENT per incident:
    the audit check makes a duplicate poll a no-op, and a resolved incident
    is never re-resolved (the caller filters on status = action_taken)."""
    pool = get_pool()
    try:
        incident_uuid = _parse_uuid(incident_id)
    except ValueError:
        return None
    equipment_uuid = None
    if equipment_id:
        try:
            equipment_uuid = _parse_uuid(equipment_id)
        except ValueError:
            equipment_uuid = None
    existing = await pool.fetchval(
        'SELECT COUNT(*) FROM "AuditLog" WHERE "IncidentId" = $1 AND "Action" = \'loop_closed\'',
        incident_uuid,
    )
    if existing:
        return None
    now = _now()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'UPDATE "Incidents" SET "Status" = \'resolved\', "ResolvedAt" = $2, "UpdatedAt" = $2 '
                'WHERE "Id" = $1 RETURNING "Id"',
                incident_uuid, now,
            )
            if row is None:
                return None
            await conn.execute(
                'INSERT INTO "IncidentStateHistory" '
                '("Id", "IncidentId", "FromStatus", "ToStatus", "Actor", "Reason", "Detail", "ChangedAt") '
                "VALUES ($1, $2, $3, 'resolved', $4, $5, $6, $7)",
                _uuid(), incident_uuid, None, actor,
                "CMMS work order resolved",
                json.dumps({"resolution": resolution, "source": source})[:300],
                now,
            )
            await conn.execute(
                'INSERT INTO "AuditLog" '
                '("Id", "Name", "Action", "IncidentId", "Actor", "Detail", "CreatedAt", "UpdatedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _uuid(), "loop_closed", "loop_closed", incident_uuid, actor,
                json.dumps({
                    "root_cause": root_cause,
                    "action_taken": action_taken,
                    "action_worked": action_worked,
                    "resolution": resolution,
                    "source": source,
                    "learned_symptoms": symptoms,
                }),
                now, now,
            )
            learned = await conn.fetchrow(
                'INSERT INTO "HistoricalIncidents" '
                '("Id", "EquipmentId", "EquipmentName", "Symptoms", "RootCause", "ActionTaken", '
                '"ActionWorked", "Resolution", "OccurredAt", "Source", "CreatedAt", "UpdatedAt") '
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $9, $9) RETURNING \"Id\"",
                _uuid(), equipment_uuid, equipment_name[:200], json.dumps(symptoms),
                root_cause, action_taken, action_worked, resolution, now, source[:100],
            )
            return str(learned["Id"]) if learned else None
