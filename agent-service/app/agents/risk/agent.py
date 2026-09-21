"""Risk Agent (Prompt 4).

Rule-based and deterministic BY DESIGN: this component decides whether a
human gets involved at all, so safety-gating logic must never depend on LLM
output. Inputs: anomaly_summary + root-cause hypothesis + confidence.

Outputs:
  * severity (low/medium/high) -- from how far/fast the anomaly deviated;
  * potential consequences -- only the ones that actually apply, selected
    by symptom/cause keywords (equipment damage / production loss /
    chemical imbalance / unplanned shutdown);
  * HITL level (1/2/3) from the severity x confidence mapping table loaded
    from config/hitl_thresholds.json (tunable without a code change).

THE SAFETY-DESIGN DECISION (documented in docs/hitl-thresholds.md): the
mapping table alone is not enough. A HIGH-severity anomaly with LOW
confidence still lands in Level 2+ -- ambiguity must never lower the
gating tier just because severity alone maps lower when the diagnosis is
uncertain. The table gives the base level; the safety-floor rule then
raises it. Deterministic, auditable, and testable in isolation.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.db import get_incident_diagnosis_state, save_risk_assessment
from app.core.logging_config import get_logger

logger = get_logger(__name__)

THRESHOLDS_PATH = Path(__file__).resolve().parent / "config" / "hitl_thresholds.json"

# Keyword tables mapping the anomaly + hypothesis to the consequences that
# actually apply. Only matching categories are emitted -- never all four.
CONSEQUENCE_KEYWORDS: dict[str, list[str]] = {
    "equipment damage": [
        "vibration", "pump", "bearing", "impeller", "degradation", "mechanical",
        "wear", "seizure", "misalignment",
    ],
    "production loss": [
        "flow", "temperature", "heat rejection", "cooling capacity", "load",
        "shutdown", "throughput",
    ],
    "chemical imbalance": [
        "ph", "conductivity", "chemistry", "dosing", "biocide", "scale",
        "corrosion", "inhibitor",
    ],
    "unplanned shutdown": [
        "failure", "seizure", "trip", "outage", "unplanned", "emergency",
        "loss of cooling", "loss of heat rejection",
    ],
}


def load_hitl_thresholds(path: Path | None = None) -> dict:
    """Load the severity x confidence -> HITL level table from config.

    Raised at import-time-of-first-use if missing/corrupt: the Risk Agent
    must never silently fall back to an in-code table (that would defeat
    'tunable without a code change' and could drift from the documented
    matrix)."""
    p = path or THRESHOLDS_PATH
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    for sev in ("low", "medium", "high"):
        if sev not in data.get("severity_to_base_level", {}):
            raise ValueError(f"hitl config missing severity '{sev}'")
    for key in ("confidence_floor", "safety_floor", "consequence_amplifiers"):
        if key not in data:
            raise ValueError(f"hitl config missing '{key}'")
    return data


def hitl_level_for(severity: str, confidence: int | None, config: dict | None = None) -> int:
    """severity x confidence -> HITL level (1/2/3), deterministic.

    Two-stage logic:
      1. base level from the severity row of the config table;
      2. safety floors: low confidence RAISES the level (ambiguity never
         lowers gating), as does a high-severity-with-low-confidence
         combination and the configured absolute floor.
    """
    cfg = config or load_hitl_thresholds()
    base = int(cfg["severity_to_base_level"][severity])

    conf = confidence if confidence is not None else cfg["confidence_floor"]["default_when_missing"]
    floor = 1
    for rule in cfg["confidence_floor"]["rules"]:
        if conf < rule["below"]:
            floor = max(floor, int(rule["minimum_level"]))
    if severity == "high" and conf < cfg["safety_floor"]["high_severity_confidence_below"]:
        floor = max(floor, int(cfg["safety_floor"]["minimum_level"]))
    return max(base, floor)


def select_consequences(hypothesis: str, anomaly_summary: dict, config: dict | None = None) -> list[dict]:
    """Which consequences actually apply, from symptom + cause keywords."""
    cfg = config or load_hitl_thresholds()
    text = (hypothesis or "").lower()
    for s in anomaly_summary.get("sensors") or []:
        text += f" {s.get('sensor_type', '')}".lower()
        if s.get("reason"):
            text += f" {s['reason']}".lower()

    out: list[dict] = []
    for consequence, keywords in CONSEQUENCE_KEYWORDS.items():
        hits = [k for k in keywords if k in text]
        if hits:
            out.append({
                "consequence": consequence,
                "rationale": cfg["consequence_amplifiers"].get(consequence,
                                f"matched on: {', '.join(hits[:3])}"),
            })
    return out


class RiskAgent:
    """Deterministic risk scoring + HITL gating for diagnosed incidents."""

    name = "risk"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config  # injected for tests; None -> load from file

    async def handle(self, payload: dict) -> dict:
        """Score one incident and persist risk_assessed transition."""
        incident_id = payload["incident_id"]
        state = await get_incident_diagnosis_state(incident_id)
        if state is None:
            raise RuntimeError(f"incident {incident_id} not found for risk scoring")

        summary = state.get("anomaly_summary") or payload.get("summary") or {}
        hypothesis = state.get("root_cause_hypothesis") or ""
        confidence = state.get("confidence")

        cfg = self._config or load_hitl_thresholds()

        severity = self._severity_from(summary, cfg)
        consequences = select_consequences(hypothesis, summary, cfg)
        level = hitl_level_for(severity, confidence, cfg)

        await save_risk_assessment(
            incident_id,
            severity=severity,
            consequences=consequences,
            hitl_level=level,
            required_approval_level=level,
        )
        logger.info(
            "Risk assessed for %s: severity=%s consequences=%d hitl_level=%d (confidence=%s)",
            incident_id, severity, len(consequences), level, confidence,
        )
        return {
            "incident_id": incident_id,
            "severity": severity,
            "consequences": consequences,
            "hitl_level": level,
        }

    @staticmethod
    def _severity_from(summary: dict, cfg: dict) -> str:
        """Worst-case static margin / drift z across flagged sensors, scored
        against the configured band/drift thresholds."""
        thresholds = cfg["severity_thresholds"]
        worst = 0.0
        for s in summary.get("sensors") or []:
            margin = s.get("static_margin")
            z = s.get("drift_z")
            if margin is not None:
                worst = max(worst, margin / thresholds["static_margin_high"])
            if z is not None:
                worst = max(worst, z / thresholds["drift_z_high"])
        if worst >= 1.0:
            return "high"
        if worst >= thresholds["medium_fraction"]:
            return "medium"
        return "low"
