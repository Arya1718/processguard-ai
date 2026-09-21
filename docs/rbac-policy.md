# RBAC Policy (Prompt 7)

The role → capability table, in one place. **This is the Security slide.**

Single source of truth: [`config/rbac-policy.json`](../config/rbac-policy.json).
The .NET middleware builds its ASP.NET authorization policies from it; the
Python agent-service loads the SAME file and re-checks decisions as a second
layer. Roles can be tuned without a code change — the same discipline as the
HITL thresholds config (`docs/hitl-thresholds.md`).

## The three roles

| Capability | Operator | MaintenanceEngineer | PlantManager |
|---|:---:|:---:|:---:|
| View own-site incidents (feed, detail, state history) | ✅ | ✅ | ✅ |
| View evidence, root cause, risk, recommendation | ✅ | ✅ | ✅ |
| "Why?" chat (`POST /incidents/{id}/ask`) | ✅ | ✅ | ✅ |
| Approve / reject **HITL Level 2** (Recommend) | ❌ | ✅ | ✅ |
| Approve / reject **HITL Level 3** (Controlled Action) | ❌ | ❌ | ✅ |
| Resolve an incident | ❌ | ✅ | ✅ |
| Trigger / reset the simulator (dev surface) | ❌ | ✅ | ✅ |
| See another site's data — ever | ❌ | ❌ | ❌ |

`max_approval_level` per role: Operator **0**, MaintenanceEngineer **2**,
PlantManager **3**. A decision request is checked as
`role.max_approval_level >= incident.required_approval_level`; failure is a
**403 with a plain-language reason**, never a misleading 404.

## Where each rule is enforced (both layers, independently)

1. **Token validation** — middleware (JwtBearer) and agent-service
   (PyJWT) each verify signature, issuer, audience, expiry on their own.
2. **Site scoping** — the middleware's typed client forces the caller's
   `site_id` claim into every query (a Site 07 user requesting a Site 12
   incident by UUID gets 403 at the middleware); the agent-service
   re-checks the incident's owning site against the forwarded token's
   claim at its query layer (`ensure_site`), so bypassing the middleware
   gains nothing.
3. **Approval level** — the middleware's policy handler rejects
   insufficient roles before proxying; the Python decision endpoints
   re-validate role + required level from the token
   (`ensure_approval_level`).
4. **Approver identity** — always taken from the validated token's
   identity, never from the request body (the body field is ignored;
   there is a test proving it).

## Cross-site isolation (the provable part)

Site 07 exists in the seed data only to demonstrate isolation. With a
known/guessed Site 12 incident UUID:

- `operator@site07.demo` → `GET /incidents/{site12-id}` → **403**
- `operator@site07.demo` → `POST /incidents/{site12-id}/approve` → **403**
- the incident list endpoint is filtered by the caller's site claim at the
  query layer, so foreign rows never even leave the database

Automated proof: `agent-service/tests/test_rbac_auth.py` (per-role
approvals incl. the Level-3 gate, cross-site detail + approval refusals,
and per-layer rejection of missing/expired/tampered tokens).
