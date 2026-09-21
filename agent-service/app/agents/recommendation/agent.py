"""Recommendation Agent (Prompt 4).

Uses the llm_client WITH tool calling (the interface built in Prompt 3).
Before finalizing, the model may call two typed tools:

  * get_equipment_maintenance_status(equipment_id) -- last serviced date +
    current condition flags (from the Equipment table's seeded maintenance
    stub; a CMMS integration replaces this in Prompt 5);
  * get_similar_incident_outcomes(equipment_id, symptom_pattern) -- what
    action was taken last time and whether it worked (HistoricalIncidents).

PLANNING MUST BE VISIBLE: every tool invocation -- name, arguments, result,
timestamp -- is recorded in order into the incident's tool_call_log jsonb.
That log is the proof that planner + tool calling are real.

Final output written to the incident: recommended_actions jsonb, a plain-
language rationale an operator can read, and an explicit note of the HITL
level (from the Risk Agent) that applies to this recommendation. Status
moves to awaiting_approval (levels 2-3) or auto_informed (level 1) via
save_recommendation (which also appends the state-history row).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.core.db import (
    add_incident_cost,
    get_equipment_maintenance_status,
    get_incident_diagnosis_state,
    get_incident_for_decision,
    get_similar_incident_outcomes,
    save_recommendation,
)
from app.core.llm_client import LLMClient
from app.core.logging_config import get_logger
from app.core.observability import (
    AGENT_OUTCOME_COUNT,
    INCIDENT_COST_USD,
    current_agent_var,
    current_incident_var,
    estimate_cost_usd,
    get_tracer,
    record_agent_stage,
)

logger = get_logger(__name__)

MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """You are the Recommendation Agent for ProcessGuard AI, an industrial water-treatment \
monitoring system. You receive a diagnosed anomaly (root-cause hypothesis, cited evidence, risk \
assessment) and produce the recommended response.

You have tools available. Use get_similar_incident_outcomes to learn what action was taken last \
time for similar symptoms and whether it worked, and get_equipment_maintenance_status to check \
the equipment's service history and condition flags -- BEFORE finalizing your recommendation.

Then respond in EXACTLY this format:

RECOMMENDED ACTIONS:
1. <specific action>
2. <specific action>
...
RATIONALE: <2-3 plain-language sentences a non-technical plant operator can read and act on>
HITL NOTE: <one sentence stating which human-in-the-loop level applies and what it means>

Rules: be specific (name the equipment and the checks/limits), never invent equipment, values, \
or history that was not provided, and never recommend bypassing the approval workflow.

If the recommendation requires a treatment chemical or consumable that must be ordered, you may \
append ONE extra line naming the product (use exactly one of: coagulant, biocide, corrosion_inhibitor), \
the quantity, and the reason, in this exact format:
PRODUCT REORDER: <coagulant|biocide|corrosion_inhibitor> qty=<number> because <one short reason>"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_equipment_maintenance_status",
            "description": "Get the last serviced date and current condition flags for a piece of equipment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "equipment_id": {"type": "string", "description": "UUID of the equipment"},
                },
                "required": ["equipment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_similar_incident_outcomes",
            "description": "Find what action was taken for similar past incidents on this equipment and whether it worked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "equipment_id": {"type": "string", "description": "UUID of the equipment (may be empty)"},
                    "symptom_pattern": {"type": "string", "description": "Space-separated symptom keywords, e.g. 'flow vibration temperature'"},
                },
                "required": ["symptom_pattern"],
            },
        },
    },
]


class RecommendationAgent:
    """LLM planner with typed tools; writes the final recommendation."""

    name = "recommendation"

    def __init__(self, llm: LLMClient, bus=None, retry_attempts: int = 3) -> None:
        self._llm = llm
        self._bus = bus  # EventBus; None only in unit tests
        self._retry_attempts = retry_attempts

    # -- tool execution ------------------------------------------------------

    async def _execute_tool(self, name: str, arguments: dict) -> dict:
        """Dispatch one tool call. Unknown tools return an error payload the
        model can see (never a silent failure)."""
        try:
            if name == "get_equipment_maintenance_status":
                return {"ok": True,
                        "result": await get_equipment_maintenance_status(arguments.get("equipment_id", ""))}
            if name == "get_similar_incident_outcomes":
                return {"ok": True,
                        "result": await get_similar_incident_outcomes(
                            arguments.get("equipment_id", ""),
                            arguments.get("symptom_pattern", ""))}
            return {"ok": False, "error": f"unknown tool '{name}'"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _planner_loop(self, incident_id: str, context: dict) -> tuple[str, list[dict]]:
        """Run the tool-calling loop; returns (final_text, tool_call_log)."""
        agent_token = current_agent_var.set("recommendation")
        incident_token = current_incident_var.set(incident_id)
        tracer = get_tracer()
        try:
            with record_agent_stage("recommendation", "plan"), tracer.start_as_current_span(
                "recommendation_plan", attributes={"incident_id": incident_id}
            ):
                user_prompt = _build_user_prompt(context)
                messages: list[dict] = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ]
                tool_log: list[dict] = []

                for round_no in range(1, MAX_TOOL_ROUNDS + 1):
                    response = await self._llm.chat(messages, tools=TOOL_SCHEMAS)

                    if not response.tool_calls:
                        AGENT_OUTCOME_COUNT.labels(agent="recommendation", outcome="completed").inc()
                        return response.content, tool_log

                    messages.append({
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                            for tc in response.tool_calls
                        ],
                    })
                    for tc in response.tool_calls:
                        result = await self._execute_tool(tc.name, tc.arguments)
                        entry = {
                            "round": round_no,
                            "tool": tc.name,
                            "arguments": tc.arguments,
                            "result": result,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                        tool_log.append(entry)
                        # THE PLANNING PROOF: every call, its args, and its result are
                        # logged before the final recommendation is written.
                        logger.info(
                            "Tool call (incident=%s round=%d): %s(%s) -> %s",
                            incident_id, round_no, tc.name,
                            json.dumps(tc.arguments), json.dumps(result)[:400],
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result),
                        })
                logger.warning("Planner hit max tool rounds (%d) for %s; forcing final answer", MAX_TOOL_ROUNDS, incident_id)
                messages.append({"role": "user",
                                 "content": "Stop calling tools. Produce the final recommendation in the required format now."})
                response = await self._llm.chat(messages, tools=None)
                AGENT_OUTCOME_COUNT.labels(agent="recommendation", outcome="max_rounds_reached").inc()
                # Accumulate incident cost.
                usage = getattr(response, "usage", None) or {}
                cost = estimate_cost_usd(response.model, usage)
                if cost > 0:
                    INCIDENT_COST_USD.labels(incident_id=incident_id).inc(cost)
                    await add_incident_cost(incident_id, cost)
                return response.content, tool_log
        finally:
            current_agent_var.reset(agent_token)
            current_incident_var.reset(incident_token)

    # -- core ----------------------------------------------------------------

    async def handle(self, payload: dict) -> dict:
        incident_id = payload["incident_id"]
        state = await get_incident_diagnosis_state(incident_id)
        if state is None:
            raise RuntimeError(f"incident {incident_id} not found for recommendation")
        risk = await get_incident_for_decision(incident_id)
        if not risk or risk.get("risk_severity") is None:
            raise RuntimeError(f"incident {incident_id} has no risk assessment; orchestrator ordering violated")

        context = {
            "incident_id": incident_id,
            "summary": state.get("anomaly_summary") or {},
            "hypothesis": state.get("root_cause_hypothesis") or "",
            "confidence": state.get("confidence"),
            "risk_severity": risk.get("risk_severity"),
            "hitl_level": risk.get("hitl_level"),
            "consequences": [
                c.get("consequence") for c in (risk.get("consequences") or [])
                if isinstance(c, dict) and c.get("consequence")
            ],
        }

        final_text, tool_log = await self._planner_loop(incident_id, context)
        parsed = _parse_recommendation(final_text)

        hitl = int(risk.get("hitl_level") or 2)
        next_status = "auto_informed" if hitl <= 1 else "awaiting_approval"
        rationale = (
            f"[HITL Level {hitl} applies to this recommendation: "
            + ("informational -- recorded and visible, no approval required.]" if hitl <= 1
               else "a human must approve before any action is executed.]")
            + "\n" + parsed["rationale"]
        )

        await self._persist_with_retry(
            incident_id,
            actions=parsed["actions"],
            rationale=rationale,
            tool_call_log=tool_log,
            next_status=next_status,
        )
        logger.info(
            "Recommendation written for %s: %d action(s), %d tool call(s) logged, status -> %s (hitl=%d)",
            incident_id, len(parsed["actions"]), len(tool_log), next_status, hitl,
        )
        # Stage completion for the Orchestrator (Prompt 4).
        if self._bus is not None:
            await self._bus.publish("pgai.stage_completed", {
                "stage": "recommendation",
                "incident_id": incident_id,
                "next_status": next_status,
                "hitl_level": hitl,
            })
        return {"incident_id": incident_id, "hitl_level": hitl, "next_status": next_status}

    async def _persist_with_retry(self, incident_id, actions, rationale, tool_call_log, next_status) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                if await save_recommendation(incident_id, actions, rationale, tool_call_log, next_status):
                    return
                last_error = RuntimeError(f"incident {incident_id} not found")
            except Exception as exc:
                last_error = exc
            logger.warning("Recommendation write attempt %d/%d failed: %s",
                           attempt, self._retry_attempts, last_error)
            await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"Recommendation write failed after {self._retry_attempts} attempts") from last_error


# ---------------------------------------------------------------------------
# Parsing the final answer
# ---------------------------------------------------------------------------

def _parse_recommendation(text: str) -> dict:
    """Extract actions + rationale from the required output format, plus the
    optional Prompt 5 PRODUCT REORDER line (carried as an extra action with
    type=product_reorder so the Action Agent's typed mapping can execute it
    without any free-text interpretation). Falls back to a single action
    wrapping the whole text (still recorded)."""
    lines = (text or "").splitlines()
    actions: list[dict] = []
    rationale_parts: list[str] = []
    product_reorder: dict | None = None
    in_rationale = False
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("PRODUCT REORDER:"):
            product_reorder = _parse_product_reorder(stripped[len("PRODUCT REORDER:"):])
            continue
        if stripped.upper().startswith("RATIONALE:"):
            in_rationale = True
            rationale_parts.append(stripped[len("RATIONALE:"):].strip())
            continue
        if in_rationale:
            if stripped.upper().startswith("HITL NOTE"):
                in_rationale = False
                continue
            rationale_parts.append(stripped)
            continue
        for prefix in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "-", "*"):
            if stripped.startswith(prefix) and stripped != prefix:
                actions.append({
                    "action": stripped[len(prefix):].strip(),
                    "priority": len(actions) + 1,
                    # Typed action vocabulary (Prompt 5): maintenance phrasing
                    # maps to a CMMS work order via agents.action.mapping.
                    "type": "work_order",
                })
                break
    if product_reorder:
        actions.append({
            "action": (
                f"Reorder {product_reorder['quantity']:g} of {product_reorder['product_key']} "
                f"({product_reorder['reason']})"
            ),
            "priority": len(actions) + 1,
            "type": "product_reorder",
            "product_key": product_reorder["product_key"],
            "quantity": product_reorder["quantity"],
        })
    if not actions:
        actions = [{"action": (text or "No specific action parsed").strip()[:400],
                    "priority": 1, "type": "manual"}]
    return {"actions": actions, "rationale": " ".join(p for p in rationale_parts if p).strip()}


def _parse_product_reorder(raw: str) -> dict | None:
    """'<coagulant|biocide|corrosion_inhibitor> qty=<n> because <reason>' ->
    a typed product_reorder action payload; None if unparsable."""
    import re

    match = re.match(
        r"\s*(coagulant|biocide|corrosion_inhibitor)\s+qty\s*=\s*([0-9.]+)\s*(?:because\s+(.*))?$",
        raw.strip(), re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "product_key": match.group(1).lower(),
        "quantity": float(match.group(2)),
        "reason": (match.group(3) or "recommended by the Recommendation Agent").strip(),
    }


def _build_user_prompt(context: dict) -> str:
    lines = ["DIAGNOSED ANOMALY:"]
    lines.append(f"- hypothesis: {context.get('hypothesis')} (confidence {context.get('confidence')}%)")
    for s in context.get("summary", {}).get("sensors") or []:
        lines.append(f"- sensor: {s.get('sensor_type')} value={s.get('value')}{s.get('unit') or ''} ({s.get('reason')})")
    lines.append("")
    lines.append("RISK ASSESSMENT:")
    lines.append(f"- severity: {context.get('risk_severity')}")
    lines.append(f"- potential consequences: {', '.join(context.get('consequences') or []) or 'none identified'}")
    lines.append(f"- applicable HITL level: {context.get('hitl_level')}")
    lines.append(f"- equipment_id: {context.get('summary', {}).get('equipment_id', '')}")
    lines.append("")
    lines.append("Check maintenance status and similar past outcomes with your tools, then produce the final recommendation in the required format.")
    return "\n".join(lines)
