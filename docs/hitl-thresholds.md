# HITL Thresholds (Prompt 4)

The Risk Agent maps `severity x confidence` to a human-in-the-loop level.
**The table lives in code-external config**
(`agent-service/app/agents/risk/config/hitl_thresholds.json`) so it can be
tuned without a code change. This page documents the shipped values and —
more importantly — the safety interaction between severity and confidence.

## The three levels

| Level | Name | Meaning | Landing status |
|---|---|---|---|
| 1 | **Inform** | Recommendation is recorded and visible; no approval needed | `auto_informed` |
| 2 | **Recommend** | A human must approve before anything happens | `awaiting_approval` |
| 3 | **Controlled Action** | Same approval flow as 2, plus: justification REQUIRED on rejection, and the FULL chain (recommendation → risk → request → approver → decision) is written to `AuditLog` | `awaiting_approval` |

## Base mapping: severity → level

`severity_to_base_level`:

| Severity | Base level |
|---|---|
| low | 1 |
| medium | 2 |
| high | 2 |

High severity does NOT jump straight to level 3 — the base table treats
"serious, well-understood" and "serious, uncertain" the same; the confidence
rules below decide how much human control is needed.

## Confidence floors: ambiguity can only RAISE the level

`confidence_floor.rules` (first matching rule wins, levels combine with the
base via `max`):

| Confidence | Minimum level |
|---|---|
| < 50 | **3** (even for LOW severity) |
| < 70 | **2** |
| missing | treated as 0 → level 3 (max caution) |

**The safety design decision (this is the important part):** low confidence
never *lowers* the gating tier. `high severity + low confidence` still
requires review — e.g. severity=high with confidence=40 lands at level 3,
not level 1. An ambiguous diagnosis of a serious anomaly is exactly the case
where a human is most needed, so the interaction is deliberately one-way:
confidence can only push the level UP from the severity base, never down.
The second rule (`safety_floor`) enforces the same principle at the boundary:
any high-severity incident with confidence below 85 is at least level 2,
whatever the base table would say.

Worked examples:

| Severity | Confidence | Base | Floors applied | Final |
|---|---|---|---|---|
| low | 95 | 1 | none | **1** (inform) |
| low | 45 | 1 | conf<50 → 3 | **3** |
| medium | 60 | 2 | conf<70 → 2 | **2** |
| medium | 45 | 2 | conf<50 → 3 | **3** |
| high | 95 | 2 | none | **2** |
| high | 60 | 2 | conf<70 → 2 | **2** |
| high | 40 | 2 | conf<50 → 3 | **3** |
| any | missing | — | missing → 3 | **3** |

The reference demo scenario (fake diagnosis confidence = 87, severity
medium-or-high depending on when the risk snapshot lands during the ramp)
lands deterministically at **level 2 → `awaiting_approval`** — the
integration test asserts this specific level.

## Why rules, not an LLM

This component decides whether a human gets involved at all. Making it
probabilistic would make the gating itself unauditable, so the Risk Agent is
pure deterministic rule evaluation over a config file: same inputs, same
level, every time, and the exact mapping can be shown in a review or demo.

## Severity scoring (how the anomaly becomes a severity)

`severity_thresholds`: the worst per-sensor score across the flagged sensors
(static band overshoot fraction normalized by `static_margin_high`, baseline
drift z normalized by `drift_z_high`) maps to:

| Normalized worst score | Severity |
|---|---|
| ≥ 1.0 | high |
| ≥ `medium_fraction` (0.3) | medium |
| below | low |

## Consequences: evidence-driven, never all four

Only consequences with a keyword hit in the hypothesis or flagged-sensor
types are emitted (keyword tables in `agents/risk/agent.py`): equipment
damage (vibration/pump/bearing...), production loss (flow/temperature/heat
rejection...), chemical imbalance (ph/conductivity/dosing...), unplanned
shutdown (failure/seizure/trip...). No hits → no fabricated consequences.

## Changing the thresholds

Edit `hitl_thresholds.json` and restart the agent service. A config missing
any required section fails fast at load (`load_hitl_thresholds` raises) — the
agent never silently falls back to in-code defaults, because silent fallback
would defeat the point of an auditable, tunable gate.
