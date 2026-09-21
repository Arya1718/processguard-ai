# Agent Components (final — all six agents, Prompt 5)

ProcessGuard AI runs **six agents** in the Python agent-service. Each is a
focused component with one job; the Orchestrator coordinates them over the
EventBus (Redis pub/sub — the stand-in for Azure Service Bus). No agent
polls the database to discover work.

| # | Agent | Input | Output | Trigger |
|---|---|---|---|---|
| 1 | **Detection** (`agents/detection/`) | raw sensor readings (Redis Stream `sensor-readings` — the Event Hub stand-in) | `Incidents` row + `AnomalyDetected` event | stream consumer, continuous |
| 2 | **Knowledge/RAG** (`agents/knowledge/`) | anomaly summary | SOP chunks (TF-IDF index — the Azure AI Search stand-in) + matched historical incidents → incident `Evidence` jsonb | `AnomalyDetected` |
| 3 | **Root-Cause** (`agents/root_cause/`) | anomaly summary + evidence | citation-checked hypothesis + confidence → incident record | `AnomalyDetected` (+ evidence-ready nudge) |
| 4 | **Orchestrator** (`agents/orchestrator/`) | `pgai.stage_completed` events | stage sequencing + HITL gate enforcement (the explicit state machine) | stage-completion events |
| 5 | **Risk** (`agents/risk/`) | anomaly summary + hypothesis + confidence | severity, consequences, deterministic HITL level (config-file table) | triggered by Orchestrator after `root_cause` |
| 6 | **Recommendation** (`agents/recommendation/`) | root cause + evidence + risk | actions + operator rationale + visible `tool_call_log`; optional typed `product_reorder` action | triggered by Orchestrator after `risk` |
| 7 | **Action** (`agents/action/`) | `pgai.incident_approved` + approved recommendation | CMMS work orders / reorders; `action_taken`/`action_failed`; on CMMS resolve → `resolved` + a new `HistoricalIncidents` row | approval event; resolution poll |

(Seven rows because the Orchestrator is listed in the flow order — the
roster is the six specialists + the Orchestrator named in the prompts.)

## Event flow

```
sensor-readings stream ──► Detection ──AnomalyDetected──► Knowledge ─┐
                                              │                      ├─(evidence ready)
                                              └────────────────────► Root-Cause
                                                                       │
                                            pgai.stage_completed: root_cause
                                                                       ▼
                                                            ┌── Orchestrator ──┐
                                                            │  triggers Risk   │
                                                            │  triggers Recom. │
                                                            │  applies HITL    │
                                                            └──────────────────┘
                                                                       │
                                            pgai.incident_approved (HITL decision endpoint)
                                                                       ▼
                                            Action ──► mock ERP/CMMS (the ONLY external write)
                                                                       │
                              CMMS work order resolved ◄──────────────┘
                                       │ (resolution poll)
                                       ▼
                       incident resolved + HistoricalIncidents row
                        (learning loop: next diagnosis retrieves it)
```

## Non-negotiable boundaries (see docs/security-model.md)

- **Write access**: only the Action Agent touches an external system, and
  only through `app/core/cmms_client.py` with its own credential, only
  after human approval. Everything else writes inside our own database.
- **Execution decisions**: deterministic typed mapping — never an LLM.
  LLMs (Root-Cause, Recommendation) reason and explain; they do not
  choose what is executed.
- **Evidence discipline**: Root-Cause claims must cite retrievable
  evidence; the Recommendation Agent's tool-call log proves its planning
  behavior; the Action Agent's `IncidentActions` rows prove what was
  actually executed externally.

## Status

All six agents are implemented and covered by unit + integration tests
(`agent-service/tests/`). The Action Agent's outcome tracking closes the
learning loop end to end: trigger → incident → diagnosis → risk →
recommendation → approval → CMMS work order → resolve → learned history.
