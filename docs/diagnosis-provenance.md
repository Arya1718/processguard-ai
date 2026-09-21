# Diagnosis Provenance

How a root-cause claim traces back to its cited evidence — and the
deterministic checks that make uncited claims impossible to store.

## The provenance contract

Every root-cause hypothesis stored on an incident satisfies, **verified in
code, not just by prompt instruction**:

1. There is a hypothesis line of the form
   `Most likely cause: <hypothesis> (confidence <N>%)`.
2. Every evidence point below it carries a citation resolving to something
   real in this system:
   - `[sensor reading]` — the point names a sensor type present in the
     incident's `anomaly_summary.sensors`;
   - `[SOP citation SOP-XXX-NNN]` — the doc id exists in the retrieved
     `sop_chunks` (which came from the knowledge base);
   - `[historical incident HIST-YYYY-NNNN]` — the source id exists in the
     incident's `matched_history` (rows of the `HistoricalIncidents` table).
3. At least one citation exists; a hypothesis with zero verifiable citations
   is rejected outright.
4. An honest "Insufficient evidence for a confident conclusion; human review
   required." answer is a *correct* outcome and is stored as
   `needs_human_review` — the model is allowed to be unconfident, never
   ungrounded.

Nothing fabricated can pass: a fabricated SOP id (`SOP-FAKE-999`) or a
sensor name absent from the summary fails step 2. If the model's first
answer fails, the agent retries **once** with a stricter reminder; a second
failure stores only a fixed meta-note (no model wording echoed) with status
`needs_human_review` and confidence 0. The rejection context goes to the
logs, never to the incident record.

## Pipeline and ordering

```
Detection Agent          Knowledge Agent                Root-Cause Agent
-------------            ----------------               ----------------
anomaly correlated  -->  pgai.anomalies  ──┐
                                           ├─> retrieve SOPs (TF-IDF)
                                           │   match history (SQL)
                                           │   write Incidents.Evidence
                                           │   publish pgai.diagnosis ──┐
                                           │                            ├─> LLM (anomaly+evidence)
                                           └────────────────────────────┘   validate citations
                                                                            write RootCauseHypothesis/
                                                                            Confidence/CitedEvidence
                                                                            status -> investigating
```

The Root-Cause Agent subscribes to **both** `pgai.diagnosis` (preferred:
evidence already written) and `pgai.anomalies` (fallback: waits up to ~10s
for evidence to land), so either agent finishing first converges on the
same result. A per-incident in-progress guard prevents double triggers, and
an already-diagnosed incident is skipped — re-delivered events and
re-triggered scenarios never cause a second LLM pass.

## Worked example — the reference demo scenario

Trigger `POST /api/v1/simulator/trigger-scenario/cooling-tower-incident`:

1. **Detection** correlates 5 drifting sensors on CP-04 into one open
   incident (severity `high`), publishes `AnomalyDetected`.
2. **Knowledge** retrieves: top chunk `SOP-COOL-014 / Cooling Water Flow
   and Vibration Response` plus 2–3 lower-ranked chunks; history match
   `demo-seed:HIST-2026-0141` (17 days ago, CP-04, same ↓flow ↑vibration
   ↑temperature signature, resolved as pump degradation + blocked strainer).
3. **Root-Cause** calls the LLM with that material and stores, e.g.:

```
Most likely cause: cooling-water pump degradation with a blocked suction
strainer (confidence 87%)
```

with `cited_evidence` rows like:

| type | ref | point |
|---|---|---|
| sensor | flow_rate | flow 106 m³/h outside band by ~88% [sensor reading] |
| sensor | vibration | vibration 4.6 mm/s over the 2.5 mm/s threshold [sensor reading] |
| sop | SOP-COOL-014 | inspect suction strainer / check filter blockage [SOP citation SOP-COOL-014] |
| history | demo-seed:HIST-2026-0141 | same combined signature 17 days ago, resolved as pump degradation [historical incident demo-seed:HIST-2026-0141] |

Each row's `ref` is checkable: the sensor types are in `anomaly_summary`,
the SOP id is a file in `app/rag/knowledge_base/`, and the history id is a
row in `HistoricalIncidents`.

## Where to look

- Incident detail: `GET /api/v1/incidents/{id}` (carries `rootCause` and
  `retrievedEvidence`), or focused: `/evidence` and `/root-cause`.
- `Incidents` columns: `Evidence` (jsonb), `RootCauseHypothesis`,
  `Confidence`, `CitedEvidence` (jsonb), `Status`.
- Agent implementation: `app/agents/root_cause/agent.py`
  (`_parse_and_validate`, `_validate_point` — the enforcement is here).
