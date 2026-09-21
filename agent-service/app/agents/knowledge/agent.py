"""Knowledge/RAG Agent (Prompt 3).

Subscribes to the AnomalyDetected event on the EventBus (the Redis pub/sub
stand-in for Azure Service Bus -- the same topic the Detection Agent
published in Prompt 2). For each incident it retrieves:

  (a) relevant SOP chunks from the RetrievalIndex (the local TF-IDF stand-in
      for Azure AI Search) using the anomaly's sensor pattern as the query;
  (b) similar past incidents from the HistoricalIncidents table -- same
      equipment + overlapping symptom pattern is enough (see
      app/core/db.py::find_similar_history; deliberately not a second ML
      model).

It writes the retrieved evidence onto the incident's Evidence jsonb column
and publishes an EvidenceCollected event so the Root-Cause Agent can react
as soon as retrieval completes (the pipeline is also robust to the Root-Cause
Agent being triggered directly by AnomalyDetected -- see root_cause/agent.py
and docs/diagnosis-provenance.md for the ordering contract).
"""
from __future__ import annotations

import asyncio
import json

from app.core.db import find_similar_history, get_incident, get_pool, save_incident_evidence
from app.core.logging_config import get_logger
from app.core.observability import (
    current_agent_var,
    current_incident_var,
    get_tracer,
    record_agent_stage,
)
from app.eventbus.base import EventBus, utcnow_iso
from app.rag.retrieval import RetrievalIndex

logger = get_logger(__name__)

TOPIC_ANOMALY = "pgai.anomalies"
TOPIC_EVIDENCE = "pgai.evidence"
TOPIC_ROOT_CAUSE_NUDGE = "pgai.diagnosis"


class KnowledgeAgent:
    """Retrieves SOP + historical evidence for a detected anomaly."""

    name = "knowledge"

    def __init__(self, index: RetrievalIndex, bus: EventBus, retry_attempts: int = 3) -> None:
        self._index = index
        self._bus = bus
        self._retry_attempts = retry_attempts
        self._sop_texts: dict[str, dict] = {}

    async def start(self) -> None:
        await self._bus.subscribe(TOPIC_ANOMALY, self._on_anomaly)
        logger.info("Knowledge agent subscribed to %s", TOPIC_ANOMALY)

    async def stop(self) -> None:
        pass

    # -- event handling -----------------------------------------------------

    async def _on_anomaly(self, payload: dict) -> None:
        """Handler for AnomalyDetected: incident_id + anomaly_summary in.

        The event shape (docs/anomaly-event-schema.md) carries `sensors` at
        the top level; derive the working summary from it when a nested
        `summary` object is absent."""
        incident_id = payload.get("incident_id")
        anomaly_summary = payload.get("summary") or payload.get("anomaly_summary") or {
            "sensors": payload.get("sensors") or [],
            "equipment_id": payload.get("equipment_id"),
        }
        if not incident_id or not anomaly_summary.get("sensors"):
            logger.error("AnomalyDetected event missing incident_id/sensors: %s", payload)
            return
        try:
            await self.handle(incident_id, anomaly_summary)
        except Exception as exc:
            logger.error(
                "Knowledge agent failed for incident %s after retries: %s -- "
                "evidence not attached (see dead-letter note in docs/failure-modes.md)",
                incident_id, exc,
            )

    async def handle(self, incident_id: str, anomaly_summary: dict) -> dict:
        """Retrieve evidence for one incident and persist it. Idempotent: a
        repeated event for the same incident simply rewrites the same evidence
        (retrieval is a pure function of the summary)."""
        agent_token = current_agent_var.set("knowledge")
        incident_token = current_incident_var.set(incident_id)
        tracer = get_tracer()
        try:
            with record_agent_stage("knowledge", "retrieve"), tracer.start_as_current_span(
                "knowledge_retrieve", attributes={"incident_id": incident_id}
            ):
                sensors = anomaly_summary.get("sensors") or []
                sensor_types = [s.get("sensor_type") for s in sensors if s.get("sensor_type")]
                equipment_id = anomaly_summary.get("equipment_id", "")

                # (a) SOP retrieval: query built from the sensor pattern so the
                # combined "flow drop + vibration" signature ranks SOP-COOL-014 above
                # the chemistry-only and temperature-only SOPs.
                query = _build_query(anomaly_summary, sensor_types)
                chunks = self._index.search(query, top_k=4)

                # Early-refresh guard: an event may fire on a PARTIAL correlated
                # summary (often a single sensor), where retrieval can legitimately
                # favor a temperature-only SOP. The summary is re-read fresh from the
                # incident (it may have grown since); if the full 5-sensor signature
                # has since folded in, refresh the query and retrieve again.
                if len(sensor_types) < 5:
                    fresh = await get_incident(incident_id)
                    fresh_types = [
                        s.get("sensorType") or s.get("sensor_type")
                        for s in ((fresh or {}).get("anomalySummary") or {}).get("sensors", [])
                    ]
                    if len([t for t in fresh_types if t]) >= 5:
                        sensor_types = [t for t in fresh_types if t]
                        anomaly_summary = dict(fresh["anomalySummary"])
                        query = _build_query(anomaly_summary, sensor_types)
                        chunks = self._index.search(query, top_k=4)
                sop_hits = [
                    {
                        "docId": c.doc_id,
                        "title": c.title,
                        "section": c.section,
                        "score": c.score,
                        "excerpt": c.text[:600],
                        "citation": f"{c.doc_id} / {c.title} -- {c.section}",
                        # Prompt 8 provenance: public_real chunks carry a working
                        # citation URL; illustrative chunks are authored for this
                        # project. Surfaced in every citation -- never implied.
                        "sourceType": c.metadata.get("source_type", "illustrative"),
                        "sourceUrl": c.metadata.get("source_url"),
                    }
                    for c in chunks
                ]
                # Keep the full text of the top SOP around for the Root-Cause prompt.
                for c in chunks:
                    self._sop_texts.setdefault(c.doc_id, {"title": c.title, "section": c.section, "text": c.text})

                # (b) Historical similarity match (same equipment + symptom overlap).
                history = await find_similar_history(equipment_id, sensor_types, limit=3)

                evidence = {
                    "incident_id": incident_id,
                    "collected_at": utcnow_iso(),
                    "query": query,
                    "sop_chunks": sop_hits,
                    "matched_history": history,
                }
                await self._persist_with_retry(incident_id, evidence)
                logger.info(
                    "Evidence attached to incident %s: %d SOP chunk(s), %d history match(es)",
                    incident_id, len(sop_hits), len(history),
                )

                # Tell the Root-Cause Agent the evidence is ready (it also triggers
                # directly off AnomalyDetected as a fallback -- whichever arrives
                # second is a no-op thanks to its done/idempotency guard).
                await self._bus.publish(
                    TOPIC_ROOT_CAUSE_NUDGE,
                    {"incident_id": incident_id, "summary": anomaly_summary, "evidence": evidence},
                )
                return evidence
        finally:
            current_agent_var.reset(agent_token)
            current_incident_var.reset(incident_token)

    async def _persist_with_retry(self, incident_id: str, evidence: dict) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                if await save_incident_evidence(incident_id, evidence):
                    return
                last_error = RuntimeError(f"incident {incident_id} not found")
            except Exception as exc:
                last_error = exc
            logger.warning("Evidence write attempt %d/%d failed: %s",
                           attempt, self._retry_attempts, last_error)
            await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"Evidence write failed after {self._retry_attempts} attempts") from last_error

    # -- helpers ------------------------------------------------------------

    def sop_text(self, doc_id: str) -> dict | None:
        """Full indexed text of one SOP document (for the root-cause prompt)."""
        return self._sop_texts.get(doc_id)


def _build_query(anomaly_summary: dict, sensor_types: list[str]) -> str:
    """Free-text query from the anomaly: sensor types + biggest deviations."""
    parts = list(sensor_types)
    parts.append("cooling water pump")
    for s in anomaly_summary.get("sensors") or []:
        reason = s.get("reason")
        if reason:
            parts.append(str(reason))
    for h in anomaly_summary.get("matched_history") or []:
        parts.append(h.get("rootCause", ""))
    return ", ".join(parts)
