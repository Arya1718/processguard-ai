"""Root-Cause Agent (Prompt 3).

Subscribes to BOTH:
  * pgai.diagnosis  (EvidenceCollected nudge from the Knowledge Agent --
                     preferred path, evidence is already on the incident)
  * pgai.anomalies  (the raw Detection Agent event -- fallback path; the
                     agent then waits briefly for evidence to land, so the
                     pipeline is robust to either agent finishing first)

Calls llm_client.generate(...) with the anomaly summary + retrieved evidence
and a system prompt that STRICTLY requires every claim to cite evidence
(sensor reading, SOP citation, or matched historical incident). The model's
answer is then RE-VALIDATED deterministically in code -- prompt-level
instructions are not trusted on their own:

  * an answer with any uncited or fabricated citation is REJECTED,
    retried once with a stricter reminder, and if it fails again the
    incident goes to status needs_human_review -- an uncited claim never
    reaches the incident record;
  * an honest "insufficient evidence" answer is recorded as
    needs_human_review (that is a correct outcome, not a failure).

See docs/diagnosis-provenance.md for the provenance contract.
"""
from __future__ import annotations

import asyncio
import re

from app.core.db import add_incident_cost, get_incident_diagnosis_state, save_root_cause
from app.core.llm_client import LLMClient
from app.core.logging_config import get_logger
from app.core.observability import (
    AGENT_OUTCOME_COUNT,
    INCIDENT_COST_USD,
    ROOT_CAUSE_CONFIDENCE,
    current_agent_var,
    current_incident_var,
    estimate_cost_usd,
    get_tracer,
    record_agent_stage,
)
from app.eventbus.base import EventBus

logger = get_logger(__name__)

TOPIC_ANOMALY = "pgai.anomalies"
TOPIC_EVIDENCE = "pgai.evidence"
TOPIC_ROOT_CAUSE_NUDGE = "pgai.diagnosis"

EVIDENCE_WAIT_ATTEMPTS = 5
EVIDENCE_WAIT_SECONDS = 2.0

# Correlation maturity: the Detection Agent's upsert folds more sensors into
# the same open incident as the scenario ramps, so the FIRST correlated
# cluster is usually partial (often one sensor). Diagnosing that would mean
# confidence computed from an incomplete anomaly signature. Wait until the
# anomaly summary carries the full sensor set (or a shorter timeout for
# single-sensor incidents).
FULL_SIGNATURE_WAIT_ATTEMPTS = 8
FULL_SIGNATURE_WAIT_SECONDS = 3.0

SYSTEM_PROMPT = """You are the Root-Cause Agent for ProcessGuard AI, an industrial anomaly \
investigation system for water-treatment sites (Ackumen-style monitoring).

You receive: (1) an anomaly summary listing which sensors breached their normal bands, by \
how much, and how fast; (2) retrieved evidence: SOP excerpts with document ids, and similar \
past incidents with their root causes and outcomes.

Respond in EXACTLY this format:

Most likely cause: <one-sentence hypothesis> (confidence <N>%)
Evidence:
- <evidence point> [sensor reading]
- <evidence point> [SOP citation SOP-XXX-NNN]
- <evidence point> [historical incident HIST-YYYY-NNNN]

ABSOLUTE RULES:
1. Every evidence point MUST carry one of the three bracket tags, and every id, sensor, or \
value it mentions MUST come from the provided anomaly summary or evidence. NEVER invent \
sensor values, SOP ids, incident references, or numbers.
2. NEVER state a cause without at least one cited evidence point.
3. If the evidence is insufficient for a confident conclusion, respond with exactly: \
"Insufficient evidence for a confident conclusion; human review required." and nothing else.
4. Do not recommend actions; a separate agent handles that."""

STRICTER_REMINDER = """

RETRY WITH STRICTER RULES: your previous answer was REJECTED because it contained at least \
one evidence point without a verifiable citation (or cited an id/value that was not in the \
provided material). Return ONLY evidence points that cite sensors, SOP ids, or incident \
references EXACTLY as given to you above. If you cannot do that, respond with exactly: \
"Insufficient evidence for a confident conclusion; human review required."
"""


class RootCauseAgent:
    """Turns anomaly + evidence into a citation-checked root-cause hypothesis."""

    name = "root_cause"

    def __init__(self, llm: LLMClient, bus: EventBus | None, retry_attempts: int = 3) -> None:
        self._llm = llm
        self._bus = bus
        self._retry_attempts = retry_attempts
        self._in_progress: set[str] = set()

    async def start(self) -> None:
        await self._bus.subscribe(TOPIC_ROOT_CAUSE_NUDGE, self._on_nudge)
        await self._bus.subscribe(TOPIC_ANOMALY, self._on_anomaly)
        logger.info("Root-cause agent subscribed to %s and %s", TOPIC_ROOT_CAUSE_NUDGE, TOPIC_ANOMALY)

    async def stop(self) -> None:
        pass

    # -- event handling -----------------------------------------------------

    async def _on_nudge(self, payload: dict) -> None:
        await self._handle_event(payload.get("incident_id"), payload.get("summary"))

    async def _on_anomaly(self, payload: dict) -> None:
        # Fallback trigger: knowledge evidence may not be written yet; the
        # evidence-wait loop inside handle() makes this ordering-safe. The
        # event carries `sensors` at the top level (see
        # docs/anomaly-event-schema.md); derive the summary from it when a
        # nested `summary` object is absent.
        summary = payload.get("summary") or payload.get("anomaly_summary") or {
            "sensors": payload.get("sensors") or [],
            "equipment_id": payload.get("equipment_id"),
        }
        await self._handle_event(payload.get("incident_id"), summary)

    async def _handle_event(self, incident_id: str | None, summary: dict | None) -> None:
        if not incident_id:
            logger.error("Diagnosis event missing incident_id: %s", incident_id)
            return
        if incident_id in self._in_progress:
            logger.info("Diagnosis already in progress for %s; ignoring duplicate trigger", incident_id)
            return
        self._in_progress.add(incident_id)
        try:
            await self.handle(incident_id, summary or {})
        except Exception as exc:
            logger.error("Root-cause agent failed for incident %s: %s", incident_id, exc)
        finally:
            self._in_progress.discard(incident_id)

    # -- core ----------------------------------------------------------------

    async def handle(self, incident_id: str, anomaly_summary: dict) -> dict | None:
        """Diagnose one incident. Idempotent: an already-diagnosed incident is
        skipped, so AnomalyDetected + EvidenceCollected double triggers and
        redelivered events never produce a second LLM write."""
        # Prompt 9: set contextvars so LLM metrics are tagged with agent + incident.
        agent_token = current_agent_var.set("root_cause")
        incident_token = current_incident_var.set(incident_id)
        tracer = get_tracer()
        try:
            with record_agent_stage("root_cause", "handle"), tracer.start_as_current_span(
                "root_cause_diagnose", attributes={"incident_id": incident_id}
            ):
                state = await get_incident_diagnosis_state(incident_id)
                if state is None:
                    logger.error("Incident %s not found for diagnosis", incident_id)
                    return None
                if state["root_cause_hypothesis"] is not None:
                    logger.info("Incident %s already diagnosed; skipping", incident_id)
                    return None

                # Wait for the correlated anomaly signature to mature before
                # diagnosing (see FULL_SIGNATURE_WAIT_* above). Use the FRESH summary
                # from the incident record, not the possibly-partial one carried by
                # the triggering event -- the LLM must describe the full anomaly.
                if state.get("sensor_count", 0) < 5:
                    for _ in range(FULL_SIGNATURE_WAIT_ATTEMPTS):
                        await asyncio.sleep(FULL_SIGNATURE_WAIT_SECONDS)
                        state = await get_incident_diagnosis_state(incident_id) or state
                        if state.get("sensor_count", 0) >= 5:
                            break
                anomaly_summary = (
                    state.get("anomaly_summary")
                    or anomaly_summary
                    or {"sensors": []}
                )

                evidence = state["evidence"]
                if not evidence:
                    # Fallback trigger won the race: wait briefly for the Knowledge
                    # Agent to finish (robust to either agent completing first).
                    for _ in range(EVIDENCE_WAIT_ATTEMPTS):
                        await asyncio.sleep(EVIDENCE_WAIT_SECONDS)
                        state = await get_incident_diagnosis_state(incident_id) or state
                        evidence = (state or {}).get("evidence")
                        if evidence:
                            break
                evidence = evidence or {"sop_chunks": [], "matched_history": []}

                user_prompt = _build_user_prompt(anomaly_summary, evidence)

                last_rejection = ""
                for attempt in (1, 2):  # one normal attempt + one stricter retry
                    system_prompt = SYSTEM_PROMPT + (STRICTER_REMINDER if attempt == 2 else "")
                    response = await self._llm.generate(system_prompt, user_prompt)
                    logger.info(
                        "Root-cause LLM attempt %d for %s (provider=%s model=%s)",
                        attempt, incident_id, response.provider, response.model,
                    )
                    parsed = _parse_and_validate(response.content, anomaly_summary, evidence)
                    if parsed["valid"]:
                        await self._persist_with_retry(
                            incident_id, parsed["hypothesis"], parsed["confidence"],
                            parsed["cited"], status="investigating",
                        )
                        # Record confidence distribution + agent outcome metrics.
                        ROOT_CAUSE_CONFIDENCE.observe(parsed["confidence"])
                        if parsed.get("confidence", 0) == 0:
                            AGENT_OUTCOME_COUNT.labels(
                                agent="root_cause", outcome="needs_human_review"
                            ).inc()
                        else:
                            AGENT_OUTCOME_COUNT.labels(
                                agent="root_cause", outcome="accepted"
                            ).inc()
                        # Accumulate incident cost from LLM usage.
                        usage = getattr(response, "usage", None) or {}
                        cost = estimate_cost_usd(response.model, usage)
                        if cost > 0:
                            INCIDENT_COST_USD.labels(incident_id=incident_id).inc(cost)
                            await add_incident_cost(incident_id, cost)
                        # Stage completion for the Orchestrator (Prompt 4): the
                        # pipeline advance is signal-driven, not polled. (Bus may be
                        # None in unit tests, where there is no orchestrator.)
                        if self._bus is not None:
                            await self._bus.publish("pgai.stage_completed", {
                                "stage": "root_cause",
                                "incident_id": incident_id,
                                "confidence": parsed["confidence"],
                            })
                        logger.info(
                            "Root cause recorded for %s (confidence %d%%, %d cited point(s))",
                            incident_id, parsed["confidence"], len(parsed["cited"]),
                        )
                        return parsed
                    last_rejection = parsed["reason"]
                    logger.warning(
                        "Root-cause answer for %s REJECTED (attempt %d): %s -- never surfacing uncited claims",
                        incident_id, attempt, last_rejection,
                    )

                # Deterministic fallback after the retry also failed: an uncited claim
                # must never reach the record, so the stored text is a fixed meta-note
                # with NO model wording echoed into it (full rejection context stays in
                # the logs only).
                await self._persist_with_retry(
                    incident_id,
                    "Automatic diagnosis withheld: model output failed the evidence-citation "
                    "check. Requires human review.",
                    0,
                    [],
                    status="needs_human_review",
                )
                AGENT_OUTCOME_COUNT.labels(
                    agent="root_cause", outcome="rejected"
                ).inc()
                logger.error(
                    "Incident %s moved to needs_human_review after failed validation: %s",
                    incident_id, last_rejection,
                )
                return None
        finally:
            current_agent_var.reset(agent_token)
            current_incident_var.reset(incident_token)

    async def _persist_with_retry(self, incident_id: str, hypothesis: str,
                                  confidence: int, cited: list[dict], status: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                if await save_root_cause(incident_id, hypothesis, confidence, cited, status):
                    return
                last_error = RuntimeError(f"incident {incident_id} not found")
            except Exception as exc:
                last_error = exc
            logger.warning("Root-cause write attempt %d/%d failed: %s",
                           attempt, self._retry_attempts, last_error)
            await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"Root-cause write failed after {self._retry_attempts} attempts") from last_error


# ---------------------------------------------------------------------------
# Deterministic parsing + citation enforcement (no LLM trust)
# ---------------------------------------------------------------------------

_HYPOTHESIS_RE = re.compile(
    # Tolerate harmless trailing punctuation after the parenthetical
    # ("... (confidence 87%)."): the citation rules stay strict, sentence
    # endings do not need to be.
    r"most likely cause:\s*(?P<cause>.+?)\s*(?:\(confidence\s*(?P<conf>\d{1,3})\s*%\s*\))?\s*\.?\s*$",
    re.IGNORECASE,
)
_INSUFFICIENT_RE = re.compile(r"insufficient evidence", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(?P<text>.+)$")


def _parse_and_validate(content: str, anomaly_summary: dict, evidence: dict) -> dict:
    """Parse the model answer and enforce the citation contract in code.

    A point is valid only when its bracket tag (or bare id) resolves to
    something real: a sensor type from the anomaly summary, a doc id from the
    retrieved SOP chunks, or a source/id from the matched history."""
    text = (content or "").strip()
    if _INSUFFICIENT_RE.search(text) and "-" not in text.split("\n")[0]:
        # Honest insufficiency is a correct outcome -> needs_human_review.
        return {"valid": True, "insufficient": True, "hypothesis": text.splitlines()[0],
                "confidence": 0, "cited": [], "reason": "model reported insufficient evidence"}

    lines = text.splitlines()
    hypothesis, confidence = None, None
    for line in lines:
        match = _HYPOTHESIS_RE.search(line.strip())
        if match:
            hypothesis = match.group("cause").strip()
            conf = match.group("conf")
            confidence = min(100, max(0, int(conf))) if conf else None
            break
    if not hypothesis:
        return {"valid": False, "reason": "no 'Most likely cause: ... (confidence N%)' line found"}

    cited: list[dict] = []
    invalid: list[str] = []
    for line in lines:
        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        point = bullet.group("text").strip()
        verdict = _validate_point(point, anomaly_summary, evidence)
        if verdict is None:
            invalid.append(point)
        else:
            cited.append({"type": verdict[0], "ref": verdict[1], "text": point})

    if not cited:
        return {"valid": False, "reason": "no evidence point carried a verifiable citation"}
    if invalid:
        return {"valid": False,
                "reason": f"{len(invalid)} evidence point(s) failed citation verification: {invalid[:2]}"}
    if confidence is None:
        return {"valid": False, "reason": "confidence score missing"}
    return {"valid": True, "insufficient": False, "hypothesis": hypothesis,
            "confidence": confidence, "cited": cited, "reason": ""}


def _validate_point(point: str, anomaly_summary: dict, evidence: dict) -> tuple[str, str] | None:
    lowered = point.lower()
    sensor_types = {str(s.get("sensor_type", "")).lower(): str(s.get("sensor_type"))
                    for s in anomaly_summary.get("sensors") or []}
    sop_ids = {str(c.get("docId", "")).lower(): str(c.get("docId"))
               for c in evidence.get("sop_chunks") or []}
    hist_refs: dict[str, str] = {}
    for h in evidence.get("matched_history") or []:
        hist_refs[str(h.get("source", "")).lower()] = str(h.get("source"))
        hist_refs[str(h.get("id", "")).lower()] = str(h.get("source"))

    if "[sensor reading]" in lowered:
        for key, display in sensor_types.items():
            if key and (key in lowered or key.replace("_", " ") in lowered):
                return ("sensor", display)
        return None
    if "[sop citation" in lowered or "sop citation" in lowered:
        for key, display in sop_ids.items():
            if key and key in lowered:
                return ("sop", display)
        return None
    if "[historical incident" in lowered or "historical incident" in lowered:
        for key, display in hist_refs.items():
            if key and key in lowered:
                return ("history", display)
        return None
    # Bare-reference tolerance: a point that names a known doc/source id
    # without the bracket tag still resolves to something real.
    for key, display in sop_ids.items():
        if key and key in lowered:
            return ("sop", display)
    for key, display in hist_refs.items():
        if key and key in lowered:
            return ("history", display)
    return None


def _build_user_prompt(anomaly_summary: dict, evidence: dict) -> str:
    lines = ["ANOMALY SUMMARY:"]
    for s in anomaly_summary.get("sensors") or []:
        lines.append(
            f"- sensor: {s.get('sensor_type')} value={s.get('value')}{s.get('unit') or ''} "
            f"({s.get('reason')}; static_margin={s.get('static_margin')}, drift_z={s.get('drift_z')})"
        )
    lines.append("")
    lines.append("RETRIEVED EVIDENCE:")
    for c in evidence.get("sop_chunks") or []:
        lines.append(f"- SOP [{c.get('docId')}] {c.get('title')} -- {c.get('section')} (score {c.get('score')}):")
        for chunk_line in str(c.get("excerpt", "")).splitlines()[:6]:
            lines.append(f"    {chunk_line}")
    for h in evidence.get("matched_history") or []:
        lines.append(
            f"- HIST [{h.get('source')}] {h.get('equipmentName')} ({h.get('occurredAt', '')[:10]}): "
            f"symptoms={h.get('symptoms')}; root cause: {h.get('rootCause')}; resolution: {h.get('resolution')}"
        )
    if not evidence.get("sop_chunks") and not evidence.get("matched_history"):
        lines.append("- (no SOP or historical evidence was retrieved)")
    lines.append("")
    lines.append("Produce the root-cause analysis in the exact format from your instructions.")
    return "\n".join(lines)
