# Live Demo Script

Click-by-click rehearsal for the ProcessGuard AI reference demo. Every step
below is lifted from the automated e2e (`frontend/e2e/full-demo.spec.js`) —
if the script and the software ever diverge, the e2e is the source of truth,
and running it is the rehearsal:

```bash
docker compose up -d            # full stack incl. simulator + mock CMMS + oidc-provider
cd frontend && npm run e2e      # this entire script, automated
```

Timings assume the default fake-LLM mode. Total run: ~2.5 minutes.

---

## Setup (before the audience arrives)

1. `docker compose up -d` — wait until `docker compose ps` shows all 8
   containers (`frontend`, `middleware`, `agent-service`, `simulator`,
   `mock-erp-cmms`, `oidc-provider`, `postgres`, `redis`), the ones with
   healthchecks as `healthy`.
2. Open http://localhost:5173 (the frontend). Do **not** pre-trigger the
   scenario — the trigger click is part of the show.
3. Optional but wise: resolve/clean any stale open incidents from a previous
   run so the feed starts empty. The e2e does this in its fixture.

---

## The demo, click by click

### 1 · Log in — real OIDC in action
- The login screen is up. Click the **maintenance@site12.demo** demo-account
  chip — the app deep-links the local identity provider's sign-in page with
  the email pre-filled (authorization-code + PKCE; the app never touches a
  password).
- Type `maintenance-pass` and click **Sign in** on the provider's page. The
  provider (protocol-compatible stand-in for Entra ID) issues an RS256 token
  through `/auth/callback`; the app lands on the **Alert Feed**.
- *Talking points:* the header now shows the signed-in user, role
  (Maintenance Engineer) and site — the same claims the backend enforces;
  this flow is a configuration change away from real Entra ID
  (docs/auth-flow.md), and the role→capability table is one config file
  (docs/rbac-policy.md).
- *If asked:* log out and sign in as `operator@site12.demo` — the Approve/
  Reject buttons disappear from the timeline (Operator cannot decide), and
  the backend would 403 the call anyway. Defense in depth: UI hides what
  the policy forbids; the API enforces it.

### 2 · The alert feed — the system at rest
- Point out the feed: equipment, status badges, severity, time since
  detection. In normal mode nothing new appears — the simulator is emitting
  in-range readings every few seconds and the Detection Agent is scoring
  every one of them.
- *Talking point:* status is color-coded **and** text-labeled — never color
  alone (accessibility).

### 3 · Trigger the reference scenario — the "incident" happens
- Click **Trigger demo scenario**. A "Scenario triggered" confirmation
  appears immediately.
- *Talking point:* the simulator now ramps the five sensors on Cooling Water
  Pump CP-04 toward the reference fault over ~30–40s: temperature toward
  38 °C (normal 29–32), flow down ~18% (normal 118–132 m³/h), vibration
  toward ~4.6 mm/s (normal <2.5), pH and conductivity drifting.
- **Wait ~40–60s.** Polling (4s interval) surfaces the new incident card on
  its own — no refresh, no manual step.
- *Talking point:* ONE incident appears, not five alerts — the Detection
  Agent's correlation window collapsed five drifting sensors into a single
  incident. Click it.

### 4 · Investigation Timeline — the state machine, live
The vertical timeline (top) and stage panels (below) fill in as agents
complete. Let each appear:

1. **Detection** — all five sensors with reading vs normal band and a
   position bar per sensor: value outside the band, by how much, and the
   drift-z if the deviation was baseline-driven. The state history shows
   `investigating` set by the root-cause agent with the confidence recorded.
2. **Evidence** — retrieved SOPs with match scores (SOP-COOL-014 tops the
   list — the flow-drop + vibration SOP) and matched historical incidents,
   including the seeded one from 17 days ago on this same pump, and — if
   earlier demo runs resolved — incidents *this system learned* at previous
   shows. That's the memory loop proving itself.
3. **Root cause** — "Most likely cause: cooling-water pump degradation,
   confidence 87%". Every evidence point is a chip tied to a sensor reading,
   an SOP citation, or a historical incident — nothing uncited is displayed.
4. **Risk** — severity **high**, consequences that actually apply, and
   **HITL level 2** with a plain-language "why this level" line. The mapping
   is deterministic (severity × confidence), config-file-driven, never an
   LLM decision.
5. **Recommendation** — the actions, the rationale, and the **tool-call
   log**: the Recommendation Agent checked `get_equipment_maintenance_status`
   and `get_similar_incident_outcomes` before finalizing. This is the moment
   to say "it plans before it recommends — and here's the receipt."
6. **Approval** — the state history shows the human gate `awaiting_approval`
   set by the hitl-gate.

### 5 · The human decides — approve
- Click **Approve**. The incident moves to `approved`; the approval records
  who and when (JWT identity, not typed text).

### 6 · Action Agent — the only component that writes externally
- Within seconds the panel shows the executed operations and the **CMMS
  reference** (a UUID work-order id issued by the mock CMMS) — a real work
  order created on the mock ERP/CMMS.
- *Talking points:* the write path is deterministic typed logic (an LLM never
  chooses what gets executed); the mock CMMS is network-isolated so only the
  Action Agent can reach it; retries are bounded and a failure lands visibly
  on `action_failed`, never silently.
- *If asked "what changes for production?"* → `docs/erp-integration.md`:
  swap base URL + auth for the real ERP/CMMS; the mapping layer and audit
  trail are unchanged.

### 7 · "Why?" — grounded Q&A on the incident record
- In the **Why?** panel ask: "Why is the pump temperature high?"
- The answer is grounded in this incident's recorded evidence and carries the
  same citation chips as the root-cause panel (reused components, one
  provenance contract). Ask something outside the record and it explicitly
  says it has no evidence rather than speculating.

### 8 · Resolve — closing the learning loop
- Click **Resolve work order…**, type the resolution note ("inspected pump
  and suction filter, cleared blockage"), click **Confirm resolution**.
- The incident becomes `resolved`, and — the payoff — a new
  `historical_incidents` row is written. **The next incident like this one
  will retrieve today's resolution as prior art.** If the audience saw step
  4's history list, they just watched the source of that history being
  created.

### 9 · Reset (ready for the next run)
- Back on the alert feed, click **Reset simulator** — the simulator returns
  to normal mode and the system is ready to run the whole thing again.

---

## Failure modes during the show

| Symptom | Meaning / recovery |
|---|---|
| No new incident after ~60s | Check `docker compose logs simulator` and `agent-service`; the incident may have folded into a still-open one (idempotency working as designed). Resolve/clean stale incidents and re-trigger. |
| A stage panel stays empty | That agent hasn't finished; the panels appear as data arrives — wait a poll cycle before assuming failure. |
| "I don't have evidence for that" in Why? | Working as intended — grounded answering, not a bug. Rephrase to something on the record. |
| `action_failed` badge | The mock CMMS rejected or timed out after bounded retries; the reason is on the incident. Restart `mock-erp-cmms` and re-approve is *not* required — the Action Agent is idempotent per incident. |
