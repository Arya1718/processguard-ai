"""Mock ERP/CMMS service (Prompt 5) -- a STAND-IN for Buckman's real ERP/CMMS.

This is a deliberately separate FastAPI application with its own namespaced
tables (cmms_*): it models a system ProcessGuard AI does NOT own, the same
way a real CMMS is an external enterprise system. It runs in its own
container on its own Docker network, reachable ONLY from the agent-service
(see docker-compose.yml + docs/erp-integration.md).

Endpoints:
    POST /work-orders                 create a work order
    GET  /work-orders/{id}            retrieve one
    GET  /work-orders                 list (filter: equipment_id, status)
    POST /work-orders/{id}/resolve    mark resolved (with a resolution note)
    GET  /inventory/{product}         quantity on hand + reorder threshold
    POST /inventory/{product}/reorder record a reorder request (decrements
                                      nothing -- fulfillment is out of scope)

Auth: a static X-Api-Key (separate credential, owned by the CMMS "enterprise"
system -- this is the ONLY key that can cause an external write). Correlation
IDs: honors X-Correlation-Id so a request is traceable from the middleware
through the agent-service into the CMMS's own logs.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------

CMMS_API_KEY = os.environ.get("CMMS_API_KEY", "")
if not CMMS_API_KEY:
    # Fail fast: never run a write-capable service unauthenticated.
    print(json.dumps({
        "level": "ERROR", "service": "mock-erp-cmms",
        "message": "CMMS_API_KEY is required -- refusing to start unauthenticated",
    }), file=sys.stderr)
    raise SystemExit(1)

PG_DSN = (
    f"postgres://{os.environ.get('PGAI_DATABASE__USER', 'pgai')}:"
    f"{os.environ.get('PGAI_DATABASE__PASSWORD', 'pgai_dev_password')}@"
    f"{os.environ.get('PGAI_DATABASE__HOST', 'postgres')}:"
    f"{os.environ.get('PGAI_DATABASE__PORT', '5432')}/"
    f"{os.environ.get('PGAI_DATABASE__NAME', 'processguard')}"
)

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class JsonFormatter(logging.Formatter):
    """One JSON object per log line: ts, level, service, correlation_id."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "mock-erp-cmms",
            "correlation_id": correlation_id_var.get(),
            "message": record.getMessage(),
        })


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
logger = logging.getLogger("mock-erp-cmms")


async def _pool() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None or _db_pool._closed:  # noqa: SLF001
        _db_pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=5)
    return _db_pool


_db_pool: asyncpg.Pool | None = None


async def ensure_schema() -> None:
    """Create the CMMS's OWN tables (cmms_* namespace). They live in the same
    local Postgres for dev convenience, but are a separate schema owned by
    the 'enterprise system' -- the incidents schema never reads or writes
    them, and vice versa (see docs/erp-integration.md)."""
    pool = await _pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS cmms_work_orders (
            "Id" uuid PRIMARY KEY,
            "EquipmentId" uuid,
            "Description" text NOT NULL,
            "Priority" varchar(20) NOT NULL,
            "Status" varchar(20) NOT NULL DEFAULT 'open',
            "RequestedBy" varchar(200) NOT NULL,
            "SourceIncidentId" uuid,
            "ResolutionNote" text,
            "CreatedAt" timestamptz NOT NULL DEFAULT NOW(),
            "UpdatedAt" timestamptz NOT NULL DEFAULT NOW()
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS cmms_inventory (
            "ProductKey" varchar(100) PRIMARY KEY,
            "Name" varchar(200) NOT NULL,
            "QuantityOnHand" double precision NOT NULL DEFAULT 0,
            "ReorderThreshold" double precision NOT NULL DEFAULT 0,
            "Unit" varchar(20) NOT NULL DEFAULT 'unit'
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS cmms_reorder_requests (
            "Id" uuid PRIMARY KEY,
            "ProductKey" varchar(100) NOT NULL REFERENCES cmms_inventory("ProductKey"),
            "Quantity" double precision NOT NULL,
            "SourceIncidentId" uuid,
            "RequestedBy" varchar(200) NOT NULL,
            "Status" varchar(20) NOT NULL DEFAULT 'requested',
            "CreatedAt" timestamptz NOT NULL DEFAULT NOW()
        )
    """)
    # Seed water-treatment products (idempotent).
    for key, name, qty, threshold, unit in [
        ("coagulant", "Coagulant (Alum-based)", 450.0, 200.0, "kg"),
        ("biocide", "Biocide (oxidizing)", 120.0, 80.0, "L"),
        ("corrosion_inhibitor", "Corrosion Inhibitor", 300.0, 150.0, "L"),
    ]:
        await pool.execute(
            'INSERT INTO cmms_inventory ("ProductKey", "Name", "QuantityOnHand", "ReorderThreshold", "Unit") '
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (\"ProductKey\") DO NOTHING",
            key, name, qty, threshold, unit,
        )
    logger.info("CMMS schema ensured (cmms_work_orders, cmms_inventory, cmms_reorder_requests)")


# ---------------------------------------------------------------------------

app = FastAPI(title="Mock ERP/CMMS", version="0.1.0", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup() -> None:
    await ensure_schema()


@app.middleware("http")
async def _correlation(request, call_next):
    cid = (request.headers.get("X-Correlation-Id") or "").strip()[:128] or uuid.uuid4().hex
    token = correlation_id_var.set(cid)
    try:
        logger.info("Request started %s %s", request.method, request.url.path)
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        logger.info("Request finished %s %s status=%d", request.method, request.url.path, response.status_code)
        return response
    finally:
        correlation_id_var.reset(token)


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != CMMS_API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-Api-Key")


# -- work orders -------------------------------------------------------------

@app.post("/work-orders", status_code=201)
async def create_work_order(body: dict, _: None = Depends(require_api_key)) -> dict:
    description = str(body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=422, detail="description is required")
    pool = await _pool()
    order_id = str(uuid.uuid4())
    row = await pool.fetchrow(
        'INSERT INTO cmms_work_orders '
        '("Id", "EquipmentId", "Description", "Priority", "RequestedBy", "SourceIncidentId") '
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
        uuid.UUID(order_id),
        _uuid_or_none(body.get("equipment_id")),
        description,
        _priority(body.get("priority")),
        str(body.get("requested_by") or "unknown")[:200],
        _uuid_or_none(body.get("source_incident_id")),
    )
    logger.info("Work order created: %s (priority=%s, incident=%s)",
                order_id, row["Priority"], row["SourceIncidentId"])
    return _order_to_dict(row)


@app.get("/work-orders")
async def list_work_orders(
    equipment_id: str | None = Query(None),
    status: str | None = Query(None),
    _: None = Depends(require_api_key),
) -> list[dict]:
    pool = await _pool()
    clauses, params = [], []
    if equipment_id:
        params.append(_uuid_or_none(equipment_id))
        clauses.append(f'"EquipmentId" = ${len(params)}')
    if status:
        params.append(status)
        clauses.append(f'"Status" = ${len(params)}')
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await pool.fetch(
        f'SELECT * FROM cmms_work_orders {where} ORDER BY "CreatedAt" DESC LIMIT 200', *params)
    return [_order_to_dict(r) for r in rows]


@app.get("/work-orders/{order_id}")
async def get_work_order(order_id: str, _: None = Depends(require_api_key)) -> dict:
    row = await _fetch_order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="work order not found")
    return _order_to_dict(row)


@app.post("/work-orders/{order_id}/resolve")
async def resolve_work_order(order_id: str, body: dict, _: None = Depends(require_api_key)) -> dict:
    """Close the loop: mark a work order resolved with a resolution note.
    The Action Agent listens for this (via the pgai.cmms_work_order_resolved
    side channel it polls through /work-orders?status=) -- resolution is what
    turns the incident into a learned historical record."""
    row = await _fetch_order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="work order not found")
    if row["Status"] == "resolved":
        raise HTTPException(status_code=409, detail="work order is already resolved")
    note = str(body.get("resolution_note") or "").strip()
    if not note:
        raise HTTPException(status_code=422, detail="resolution_note is required")
    pool = await _pool()
    updated = await pool.fetchrow(
        'UPDATE cmms_work_orders SET "Status" = \'resolved\', "ResolutionNote" = $2, "UpdatedAt" = NOW() '
        'WHERE "Id" = $1 RETURNING *',
        uuid.UUID(order_id), note,
    )
    logger.info("Work order resolved: %s", order_id)
    return _order_to_dict(updated)


# -- inventory ---------------------------------------------------------------

@app.get("/inventory/{product_key}")
async def get_inventory(product_key: str, _: None = Depends(require_api_key)) -> dict:
    pool = await _pool()
    row = await pool.fetchrow('SELECT * FROM cmms_inventory WHERE "ProductKey" = $1', product_key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown product '{product_key}'")
    return {
        "productKey": row["ProductKey"],
        "name": row["Name"],
        "quantityOnHand": float(row["QuantityOnHand"]),
        "reorderThreshold": float(row["ReorderThreshold"]),
        "unit": row["Unit"],
    }


@app.post("/inventory/{product_key}/reorder", status_code=201)
async def reorder(product_key: str, body: dict, _: None = Depends(require_api_key)) -> dict:
    """Record a reorder request. Decrements nothing: fulfillment is a
    separate real process we deliberately do not model."""
    pool = await _pool()
    product = await pool.fetchrow('SELECT * FROM cmms_inventory WHERE "ProductKey" = $1', product_key)
    if product is None:
        raise HTTPException(status_code=404, detail=f"unknown product '{product_key}'")
    try:
        quantity = float(body.get("quantity") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="quantity must be a number")
    if quantity <= 0:
        raise HTTPException(status_code=422, detail="quantity must be positive")
    request_id = str(uuid.uuid4())
    await pool.execute(
        'INSERT INTO cmms_reorder_requests '
        '("Id", "ProductKey", "Quantity", "SourceIncidentId", "RequestedBy") '
        "VALUES ($1, $2, $3, $4, $5)",
        uuid.UUID(request_id), product_key, quantity,
        _uuid_or_none(body.get("source_incident_id")),
        str(body.get("requested_by") or "unknown")[:200],
    )
    logger.info("Reorder recorded: %s x%s %s (incident=%s)",
                quantity, product["Unit"], product_key, body.get("source_incident_id"))
    return {
        "id": request_id,
        "productKey": product_key,
        "quantity": quantity,
        "unit": product["Unit"],
        "status": "requested",
        "sourceIncidentId": body.get("source_incident_id"),
    }


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict:
    try:
        pool = await _pool()
        await pool.fetchval("SELECT 1")
        return {"status": "ok", "postgres": "ok"}
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "degraded", "postgres": str(exc)[:200]})


# -- helpers -----------------------------------------------------------------

def _uuid_or_none(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError):
        return None


def _priority(value) -> str:
    word = str(value or "medium").lower()
    return word if word in ("low", "medium", "high") else "medium"


async def _fetch_order(order_id: str):
    order_uuid = _uuid_or_none(order_id)
    if order_uuid is None:
        raise HTTPException(status_code=422, detail="work order id must be a UUID")
    pool = await _pool()
    return await pool.fetchrow('SELECT * FROM cmms_work_orders WHERE "Id" = $1', order_uuid)


def _order_to_dict(row) -> dict:
    return {
        "id": str(row["Id"]),
        "equipmentId": str(row["EquipmentId"]) if row["EquipmentId"] else None,
        "description": row["Description"],
        "priority": row["Priority"],
        "status": row["Status"],
        "requestedBy": row["RequestedBy"],
        "sourceIncidentId": str(row["SourceIncidentId"]) if row["SourceIncidentId"] else None,
        "resolutionNote": row["ResolutionNote"],
        "createdAt": row["CreatedAt"].isoformat(),
        "updatedAt": row["UpdatedAt"].isoformat(),
    }
