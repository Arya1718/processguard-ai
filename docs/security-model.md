# Security Model (started in Prompt 5; full build is Prompt 7)

This doc records the security-relevant architecture decisions as they are
made, starting with the single most important one in this system.

## The write-access boundary (the headline decision)

**The Action Agent is the ONLY component in ProcessGuard AI with write
access to an external enterprise system.**

Every other agent (Detection, Knowledge, Root-Cause, Risk, Recommendation,
Orchestrator) only ever:

- reads sensor data (Redis stream) and the knowledge base (local files + DB),
- writes to ProcessGuard AI's own incident tables in its own Postgres.

This distinction is **enforced in code, not by convention**:

1. **One module with an outbound write.** `app/core/cmms_client.py` is the
   only code in the repository that performs an external write. If a future
   feature needs to touch an external system, it must go through this
   client and the Action Agent — it cannot grow its own HTTP calls without
   visibly adding a second write path (which review should reject).
2. **Separate credentials.** The CMMS API key (`PGAI_CMMS__API_KEY`) is a
   distinct secret, separate from the database password, Redis, and the
   LLM key. It is the only credential in the process that can cause an
   external side effect, so it can be rotated or revoked independently
   without touching anything else. In staging/prod it lives in Azure Key
   Vault under its own secret name, scoped to the workload identity the
   Action Agent runs as (managed identity, no secrets in code or env
   files — the `.env` here is a local-dev stand-in, see Prompt 1).
3. **Approval precedes execution.** The Action Agent acts only on
   `pgai.incident_approved` events, and *re-checks the database* that the
   incident status is exactly `approved` before any external call. A
   replayed or forged bus message cannot trigger a write for an
   unapproved incident.
4. **No LLM decides what gets executed.** Deciding *which* external write
   to perform is deterministic, typed code (`agents/action/mapping.py`):
   a small closed vocabulary of action types mapped to CMMS calls.
   LLMs reason and explain; they never choose what is executed. This
   removes prompt-injection from the write path entirely.
5. **Network isolation.** The mock ERP/CMMS container is attached only to
   the internal `cmms` Docker network together with the agent-service. It
   has no published ports, so the host cannot reach it; the frontend,
   middleware, and simulator are not on that network, so they cannot reach
   it either. The middleware exposes **no route** to the CMMS — all CMMS
   interaction is mediated by the Action Agent. You can verify this:
   `docker compose exec frontend curl -sf http://mock-erp-cmms:8100/health/live`
   fails by DNS resolution, not by policy.
6. **Full audit of every external write.** Success produces an
   `action_executed` AuditLog row carrying the complete chain: who
   approved, what was recommended, what was executed, the CMMS reference,
   and timestamps. Failures are recorded and published
   (`pgai.action_failed`) — nothing silently stuck.

## The rest of the picture (current state)

| Layer | Decision | Where |
|---|---|---|
| AuthN | **Real OIDC (Prompt 7)**: authorization-code + PKCE against a protocol-compatible local provider standing in for Entra ID; standard JwtBearer validation, no hand-rolled token parsing | Prompts 1 → 7, `docs/auth-flow.md` |
| AuthZ (roles) | Policy table in `config/rbac-policy.json` — one source of truth, loaded by BOTH services; Operator/MaintenanceEngineer/PlantManager → max HITL level 0/2/3 | Prompt 7, `docs/rbac-policy.md` |
| Approver identity | Taken from the validated JWT, never the request body — cannot be spoofed | Prompt 4 |
| Site isolation | Enforced at BOTH query layers: the middleware forces the caller's `site_id` claim into every request; the agent-service re-checks the owning site against the forwarded token. Cross-site reads AND approvals are 403s | Prompt 7 |
| Defense in depth | The agent-service is never internet-facing but does NOT trust the middleware: it re-validates signature + expiry itself and re-applies site + approval-level rules | Prompt 7 |
| HITL gating | Deterministic severity × confidence → approval level; low confidence raises the gate (safety floor) | Prompt 4, `docs/hitl-thresholds.md` |
| Diagnosis provenance | Every root-cause claim must cite sensor/SOP/history evidence; uncited claims are rejected before they reach the record | Prompt 3 |
| Action typing | Recommendation → CMMS call mapping is explicit typed logic, never a free-text LLM decision | Prompt 5 |
| Secrets | All config via environment variables, fail-fast; Key Vault via managed identity in staging/prod | Prompt 1 |
| Containers | Multi-stage builds, non-root users in every image; the mock CMMS is reachable only from the agent-service network | Prompts 1 & 5 |
| Correlation IDs | End to end, including the CMMS's own logs | Prompts 1 & 5 |

## Residual risks / what production adds (honest list)

- **Key rotation**: the demo provider mints an ephemeral key per start and
  fingerprints the kid, so clients re-fetch JWKS automatically — but a
  real deployment wants a persistent key in Key Vault with a documented
  rotation policy.
- **Token lifetime**: ~15 minutes in the demo. Production adds refresh-token
  handling and conditional-access policies.
- **Secrets in env**: compose still passes secrets as environment variables
  for local dev; staging/prod swaps the source to Key Vault (the config
  loader interface was built for this in Prompt 1).
- **Audit log integrity**: `audit_log` is append-only within the database;
  production would ship it to immutable storage.

## Planned (Prompt 8)

- Observability: metrics, dashboards, alerting on `action_failed` and
  dead-letter volumes.
- Automated test-suite polish (Prompt 9).
