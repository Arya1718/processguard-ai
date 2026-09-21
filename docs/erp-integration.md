# ERP / CMMS Integration (Prompt 5)

How ProcessGuard AI executes approved actions, what the mock ERP/CMMS
provides, and **exactly what changes to point this at Buckman's real
ERP/CMMS** — the answer to "what actually changes for production."

## The mock ERP/CMMS service

`mock-erp-cmms/` is a **separate FastAPI application** with its own
namespaced tables (`cmms_work_orders`, `cmms_inventory`,
`cmms_reorder_requests`). It models a system ProcessGuard AI does **not**
own: the incidents schema never reads or writes `cmms_*` tables, and the
CMMS never touches `Incidents`/`Approvals`/etc. They only meet over HTTPS
with an API key.

It runs as its own container on the isolated `cmms` Docker network —
no published ports, reachable **only** by the agent-service
(see `docs/security-model.md`).

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/work-orders` | Create a work order (`equipment_id`, `description`, `priority`, `requested_by`, `source_incident_id`) |
| GET | `/work-orders/{id}` | Retrieve one |
| GET | `/work-orders` | List, filter by `equipment_id` / `status` |
| POST | `/work-orders/{id}/resolve` | Mark resolved with a `resolution_note` (required) |
| GET | `/inventory/{product}` | Quantity on hand + reorder threshold |
| POST | `/inventory/{product}/reorder` | Record a reorder request (decrements nothing — fulfillment is a separate real process we do not model) |
| GET | `/health/live`, `/health/ready` | Container health |

Auth: static `X-Api-Key` (dev value `cmms-dev-key-change-me`, from
`CMMS_API_KEY` in `.env`). Missing/wrong key → 401.

Seeded inventory: `coagulant` (450 kg on hand, reorder at 200),
`biocide` (120 L, reorder at 80), `corrosion_inhibitor` (300 L, reorder
at 150).

## How the Action Agent maps recommendations to CMMS calls

The mapping lives in `agent-service/app/agents/action/mapping.py` and is
**explicit, typed, and deterministic — no LLM decides what external write
is performed**. This is the second safety boundary (the first is HITL
approval; see `docs/security-model.md`):

1. The Recommendation Agent's output schema carries an optional `type` per
   action — `work_order`, `product_reorder`, or `manual` — plus optional
   `product_key` and `quantity` fields (Prompt 5 extension; the agent can
   emit a `PRODUCT REORDER:` line which is parsed into a typed action).
2. `map_recommendation_to_cmms()` walks the approved `recommended_actions`
   and emits `CmmsOperation` objects:
   - `work_order` → `POST /work-orders` (description = the action text,
     priority 1→high / 2→medium / 3+→low, `source_incident_id` set);
   - `product_reorder` → `POST /inventory/{product}/reorder`;
   - `manual`/unknown → **skipped and reported** (`unmapped`), never
     guessed into an external write. Legacy actions without a `type` are
     classified by maintenance-keyword heuristics.
3. The Action Agent (`agents/action/agent.py`) executes each operation
   through `app/core/cmms_client.py` — the only module in the codebase
   with an outbound external write — with bounded retry/backoff
   (4xx is never retried; network/5xx retried up to `PGAI_CMMS__RETRY_ATTEMPTS`).

### Bookkeeping on success / failure

- Every attempt gets an `IncidentActions` row (`pending` → `succeeded` /
  `failed`) with the CMMS reference — this is also the **idempotency
  record**: a duplicate approval event or a crash-and-restart can never
  create a second work order, because the agent checks for already-
  succeeded operations before acting.
- Success → incident `action_taken`, `ActionExecutedAt` set, a full
  `action_executed` AuditLog chain (who approved → what was recommended →
  what executed → CMMS reference), and `pgai.action_completed` published.
- Failure → incident `action_failed` with the recorded reason and
  `pgai.action_failed` published (the dashboard must surface this) — the
  incident is never left silently stuck.

## Closing the loop (outcome tracking)

`POST /work-orders/{id}/resolve` on the CMMS → the Action Agent's
resolution poll notices the resolved work order, moves the incident to
`resolved`, and writes a **new `HistoricalIncidents` row** (equipment,
symptom pattern, root cause, action taken, outcome, source tag
`HIST-AUTO-<incident>`). That row is what the Knowledge Agent retrieves
on the *next* similar incident — the episodic-memory learning loop.
The loop close is idempotent (guarded by a `loop_closed` audit row).

## What changes to point this at Buckman's real ERP/CMMS

Deliberately small, and that is the point:

| What | Today (mock) | Production (real ERP/CMMS) |
|---|---|---|
| `PGAI_CMMS__BASE_URL` | `http://mock-erp-cmms:8100` | The real CMMS/ERP endpoint |
| `PGAI_CMMS__API_KEY` | shared dev key | Per-environment secret in Azure Key Vault (managed identity), scoped to the Action Agent's workload identity |
| `app/core/cmms_client.py` | REST + `X-Api-Key` | Same interface; swap the auth header for OAuth2 client-credentials / mutual TLS if the real system demands it — callers are insulated, only this file changes |
| Field mapping | `mapping.py` constants | Adjust payload field names / vocabularies if the real CMMS uses different enums (priority codes, work-type codes) |
| Resolution push | polling `GET /work-orders/{id}` | Prefer a webhook/queue from the real CMMS if available; the poll stays as fallback |
| Fulfillment | not modeled (reorder only records a request) | Real inventory/procurement flows, out of ProcessGuard AI's scope |

Nothing else changes: the recommendation schema, the Action Agent, the
audit chain, HITL gating, and idempotency are all CMMS-agnostic already.
