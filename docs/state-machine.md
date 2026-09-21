# Incident State Machine (Prompt 4)

The incident lifecycle is an explicit state machine, enforced in
`agent-service/app/agents/orchestrator/agent.py` (`VALID_TRANSITIONS`) and
recorded row-by-row in the `IncidentStateHistory` table. Every transition is
queryable — this is the "Investigation Workflow" made real and inspectable,
not just described.

```
                        ┌────────────────────────────────────────────┐
                        │            (side exit, any pre-approval    │
                        │             stage: citation check failed)  │
                        ▼                                            │
  ┌──────┐   ┌───────────────┐   ┌───────────────┐   ┌────────────┐ │
  │ open │──►│ investigating │──►│ risk_assessed │──►│recommended │ │
  └──────┘   └───────────────┘   └───────────────┘   └─────┬──────┘ │
      │               │                  │                 │        │
      │               │                  │                 ▼        │
      │               │                  │         ┌───────────────────────┐
      │               │                  │         │ HITL gate (Risk Agent │
      │               │                  │         │ level, config-driven) │
      │               │                  │         └───────┬───────────────┘
      │               │                  │        level 1  │  levels 2-3
      │               │                  │                 ▼        ▼
      │               │                  │   ┌───────────────┐ ┌───────────────────┐
      │               │                  │   │ auto_informed │ │ awaiting_approval │◄──┐
      │               │                  │   └───────┬───────┘ └────────┬──────────┘ │
      │               │                  │           │                  │  POST /approve or /reject │
      │               │                  │           │                  ▼            │
      │               │                  │           │        ┌─────────────────┐    │
      │               │                  │           │        │ approved        │────┘ (no: rejected)
      │               │                  │           │        │ rejected        │
      │               │                  │           │        └────────┬────────┘
      │               │                  │           │                 │
      ▼               ▼                  ▼           ▼                 ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │                             resolved                                 │
  └──────────────────────────────────────────────────────────────────────┘
```

## The transitions and who makes them

| # | From | To | Actor | Trigger |
|---|------|----|-------|---------|
| 1 | `open` | `investigating` | root-cause-agent | citation-checked diagnosis recorded (`save_root_cause`) |
| 2 | `investigating` | `risk_assessed` | risk-agent | Risk Agent scored severity/consequences/HITL level (`save_risk_assessment`) |
| 3 | `risk_assessed` | `recommended` | recommendation-agent | recommendation + tool-call log written (`save_recommendation`, stage 1) |
| 4 | `recommended` | `awaiting_approval` | hitl-gate | Risk level 2-3 (`save_recommendation`, stage 2) |
| 4' | `recommended` | `auto_informed` | hitl-gate | Risk level 1 (`save_recommendation`, stage 2) |
| 5 | `awaiting_approval` | `approved` | JWT identity | `POST /api/v1/incidents/{id}/approve` (via middleware passthrough) |
| 5' | `awaiting_approval` | `rejected` | JWT identity | `POST /api/v1/incidents/{id}/reject` (justification REQUIRED at level 3) |
| 6 | `approved` | `action_taken` \| `action_failed` | action-agent | external execution on the mock ERP/CMMS (Prompt 5) |
| 7 | `action_taken` / `action_failed` / `rejected` / `auto_informed` | `resolved` | action-agent / system | closure — via resolved CMMS work order (which also writes the learned HistoricalIncidents row) or manual `/resolve` |

Side exits and repairs:

- `→ needs_human_review` — from any pre-approval stage when the Root-Cause
  Agent's answer fails citation validation twice (uncited claims can never be
  surfaced). A human can restart the pipeline from there: `needs_human_review
  → investigating`.
- `rejected → investigating` — a rejected recommendation may be re-investigated.
- The Orchestrator's `_apply_hitl_gate` re-applies the landing status if the
  recorded `required_approval_level` and the actual status ever disagree
  (defensive consistency check; the common case is a no-op).

## Where to see it

- `GET /api/v1/incidents/{id}/state-history` (middleware passthrough) returns
  the full ordered history: `[{from, to, actor, reason, detail, changedAt}, ...]`.
- For the reference demo scenario the observed chain is exactly:

  ```
  investigating    <- root-cause-agent
  risk_assessed    <- risk-agent
  recommended      <- recommendation-agent
  awaiting_approval<- hitl-gate
  ```

  and after a human approves, the Prompt 5 execution tail is appended:

  ```
  approved         <- <jwt-user>
  action_taken     <- action-agent
  resolved         <- action-agent (CMMS work order resolved)
  ```

- Every state-history row carries the incident's correlation context: the
  async pipeline stages publish `pgai.stage_completed` events on the EventBus
  (see `docs/agent-components.md`), and the HTTP-triggered decisions run under
  the caller's `X-Correlation-Id`, so a full investigation is traceable.
