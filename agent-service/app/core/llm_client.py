"""LLM client abstraction -- stand-in for Azure OpenAI.

One interface for every agent:

    client = get_llm_client()
    response = await client.generate(
        system_prompt="...", user_prompt="...", tools=[...]  # tools optional
    )

AZURE OPENAI NOTE: the active implementation (GroqChatCompletionsClient) is a
STAND-IN. It speaks the OpenAI-compatible chat-completions protocol against
Groq's endpoint. A second implementation (AzureOpenAIClient) can be swapped in
later with NO change to any agent code that calls llm_client.generate(...) --
only the wiring inside get_llm_client() changes. The interface already carries
OpenAI-style tool/function-calling so the Recommendation Agent (Prompt 4+) can
use it without another interface break.

FAKE MODE: setting PGAI_LLM__FAKEMODE=true swaps in a deterministic scripted
client so the pipeline runs and is verifiable without an API key (offline dev,
CI, and demo rehearsal). It is clearly labeled in logs as fake and never used
unless explicitly enabled.
"""
from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field

from app.core.config import MissingEnvironmentVariable, Settings, get_settings
from app.core.logging_config import get_logger
from app.core.observability import (
    LLM_CALL_COUNT,
    LLM_COST_USD,
    LLM_TOKEN_USAGE,
    current_agent_var,
    current_incident_var,
    estimate_cost_usd,
)

logger = get_logger(__name__)


def _record_llm_metrics(
    response: "LLMResponse", provider: str, model: str
) -> None:
    """Record token usage, call count, and estimated cost for one LLM call.

    Agent and incident_id are read from contextvars set by the calling agent
    (so the metrics pipeline knows which agent + incident the call cost
    accrues to). Missing contextvars default to "unknown" / "".
    """
    agent = current_agent_var.get() or "unknown"
    incident_id = current_incident_var.get() or ""
    usage = response.usage or {}
    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0
    total_tokens = usage.get("total_tokens", 0) or 0

    LLM_CALL_COUNT.labels(
        agent=agent, provider=provider, model=model, incident_id=incident_id
    ).inc()
    LLM_TOKEN_USAGE.labels(
        agent=agent, provider=provider, model=model, incident_id=incident_id
    ).inc(prompts_and_completions := prompt_tokens + completion_tokens)
    cost = estimate_cost_usd(model, usage)
    LLM_COST_USD.labels(
        agent=agent, provider=provider, model=model, incident_id=incident_id
    ).inc(cost)


@dataclass(frozen=True)
class ToolCall:
    """One OpenAI-style tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict  # parsed JSON arguments


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    provider: str = "groq"


class LLMClient(abc.ABC):
    """Transport-agnostic LLM interface used by all agents.

    NOTE: implementations are stand-ins for Azure OpenAI; swapping one for
    another must not change any agent code (see module docstring).
    """

    @abc.abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """One chat-completion round trip.

        tools: optional OpenAI-style tool schemas
        [{"type":"function","function":{...}}]. Implementations must pass them
        through and surface any requested calls on LLMResponse.tool_calls.
        """

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Multi-turn chat with tool results fed back (default: build on
        generate by serializing history into the user turn). Providers with
        native multi-turn support override this; the Recommendation Agent's
        planner loop uses it for tool result rounds."""
        system_prompt = ""
        convo: list[str] = []
        for m in messages:
            if m.get("role") == "system":
                system_prompt = m.get("content", "")
            elif m.get("role") == "tool":
                convo.append(f"[tool result] {m.get('content', '')}")
            else:
                convo.append(m.get("content", ""))
        user_prompt = "\n\n".join(convo)
        return await self.generate(system_prompt, user_prompt, tools)


class GroqChatCompletionsClient(LLMClient):
    """OpenAI-compatible chat-completions client backed by the Groq API.

    Fail fast: constructed only when GROQ_API_KEY is present (the same
    fail-fast pattern as Prompt 1's config loader -- never start silently
    misconfigured). In staging/prod the key arrives from Azure Key Vault via
    managed identity instead of a .env file.
    """

    provider = "groq"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise MissingEnvironmentVariable(
                "Missing required environment variable 'GROQ_API_KEY'. "
                "Add it to .env (or set PGAI_LLM__FAKEMODE=true for offline "
                "dev/tests). (In staging/prod this key comes from Azure Key "
                "Vault via managed identity.)"
            )
        self._api_key = settings.llm_api_key
        self._model = settings.llm_model
        self._base_url = settings.llm_base_url.rstrip("/")
        self._timeout = settings.llm_timeout_seconds

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        import httpx

        body: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(
                f"{self._base_url}/chat/completions", json=body, headers=headers
            )
            response.raise_for_status()
            data = response.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=_safe_json((tc.get("function") or {}).get("arguments", "{}")),
            )
            for tc in message.get("tool_calls") or []
        ]
        response = LLMResponse(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            model=data.get("model", self._model),
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage") or {},
            provider=self.provider,
        )
        _record_llm_metrics(response, self.provider, self._model)
        return response


    # Multi-turn: native message history (incl. role=tool results) is sent
    # straight through -- no serialization loss for the planner loop.
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        import httpx

        body: dict = {"model": self._model, "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(f"{self._base_url}/chat/completions", json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=_safe_json((tc.get("function") or {}).get("arguments", "{}")),
            )
            for tc in message.get("tool_calls") or []
        ]
        response = LLMResponse(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            model=data.get("model", self._model),
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage") or {},
            provider=self.provider,
        )
        _record_llm_metrics(response, self.provider, self._model)
        return response


class FakeScriptedLLMClient(LLMClient):
    """Deterministic fake for offline dev / CI / demo rehearsal.

    Enabled ONLY by PGAI_LLM__FAKEMODE=true. Behavior:
      * PGAI_LLM__FAKE_RESPONSES (JSON array of strings), if set, is consumed
        first-in-first-out so tests can script exact replies;
      * otherwise it derives a plausible, fully-cited root-cause answer from
        the evidence block embedded in the user prompt.
    Every response is logged as FAKE -- never mistake it for a model answer.
    """

    provider = "fake"

    def __init__(self) -> None:
        raw = _env_or("PGAI_LLM__FAKE_RESPONSES", "")
        self._script: list[str] = _safe_json_list(raw) if raw else []
        self._calls = 0

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        self._calls += 1
        if self._script:
            content = self._script.pop(0)
        else:
            content = _derive_fake_analysis(user_prompt)
        logger.warning("FAKE LLM response #%d returned (PGAI_LLM__FAKEMODE=true)", self._calls)
        response = LLMResponse(content=content, model="fake-scripted", finish_reason="stop", provider=self.provider)
        _record_llm_metrics(response, self.provider, "fake-scripted")
        return response

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        """Planner-aware fake: with scripted TOOL_CALL responses it drives the
        planner loop deterministically. Script entries may be
          * "TOOL_CALL:name:{json args}" -> emits one tool call, or
          * any other string              -> plain assistant content.
        When the conversation already contains tool results (round >= 2) and
        nothing is scripted, it synthesizes the final recommendation from
        what the tools actually returned -- same contract as a real model."""
        self._calls += 1
        if self._script:
            entry = self._script.pop(0)
            if entry.startswith("TOOL_CALL:"):
                _, name, args_raw = entry.split(":", 2)
                response = LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id=f"fake-{self._calls}", name=name,
                                         arguments=_safe_json(args_raw))],
                    model="fake-scripted", finish_reason="tool_calls", provider=self.provider,
                )
                _record_llm_metrics(response, self.provider, "fake-scripted")
                return response
            response = LLMResponse(content=entry, model="fake-scripted",
                                   finish_reason="stop", provider=self.provider)
            _record_llm_metrics(response, self.provider, "fake-scripted")
            return response

        saw_tool_results = any(m.get("role") == "tool" for m in messages)
        if saw_tool_results:
            # Final round: ground the recommendation in the tool results.
            tool_text = "\n".join(
                str(m.get("content", "")) for m in messages if m.get("role") == "tool"
            )
            response = LLMResponse(
                content=_derive_fake_recommendation(tool_text),
                model="fake-scripted", finish_reason="stop", provider=self.provider,
            )
            _record_llm_metrics(response, self.provider, "fake-scripted")
            return response
        # Round 1, no script: behave like a real planner -- emit actual tool
        # calls (parsed ToolCall objects, not text) for BOTH data sources
        # before answering. The equipment id is lifted from the user prompt
        # the Recommendation Agent built.
        import re as _re

        user_text = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        )
        match = _re.search(r"equipment_id:\s*([0-9a-fA-F-]{36})", user_text)
        equipment_id = match.group(1) if match else ""
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"fake-{self._calls}-maint",
                    name="get_equipment_maintenance_status",
                    arguments={"equipment_id": equipment_id},
                ),
                ToolCall(
                    id=f"fake-{self._calls}-similar",
                    name="get_similar_incident_outcomes",
                    arguments={"equipment_id": equipment_id, "symptom_pattern": "flow"},
                ),
            ],
            model="fake-scripted", finish_reason="tool_calls", provider=self.provider,
        )
        _record_llm_metrics(response, self.provider, "fake-scripted")
        return response


def _derive_fake_recommendation(tool_text: str) -> str:
    """Final recommendation grounded in the tool results actually returned."""
    worked = ('actionWorked' in tool_text and 'true' in tool_text.lower()) or 'actionWorked: True' in tool_text
    maintenance = "lastServicedAt" in tool_text
    lines = ["RECOMMENDED ACTIONS:"]
    if "strainer" in tool_text.lower() or "filter" in tool_text.lower():
        lines.append("1. Inspect the pump suction strainer and backwash or replace the filter.")
    lines.append("2. Verify pump bearing condition and schedule maintenance if vibration stays above 2.5 mm/s.")
    lines.append("3. Reduce process load by 10% if supply temperature exceeds 40 degC.")
    lines.append("")
    lines.append("RATIONALE: " + (
        "The same combined flow/vibration signature was resolved before by clearing the suction strainer "
        "and servicing the pump, so start there; monitor temperature while the pump is inspected."
        if worked else
        "Historical outcomes for this equipment support inspecting the suction path and pump condition "
        "first; protect the heat exchangers by reducing load if temperature keeps rising."
    ))
    if maintenance:
        lines.append("Maintenance status was checked before finalizing this plan.")
    return "\n".join(lines)


_client: LLMClient | None = None


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Return the process-wide LLM client (fake mode wins if enabled)."""
    global _client
    if _client is None:
        s = settings or get_settings()
        if s.llm_fake_mode:
            _client = FakeScriptedLLMClient()
            logger.warning("LLM client: FAKE scripted mode (PGAI_LLM__FAKEMODE=true)")
        else:
            _client = GroqChatCompletionsClient(s)
            logger.info("LLM client: Groq chat completions (model=%s)", s.llm_model)
    return _client


def reset_llm_client() -> None:
    """Test hook: drop the cached client so the next get_llm_client() re-reads env."""
    global _client
    _client = None


def _env_or(name: str, default: str) -> str:
    import os

    return os.environ.get(name) or default


def _safe_json(raw: str) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _safe_json_list(raw: str) -> list:
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def _derive_fake_analysis(user_prompt: str) -> str:
    """A cited, demo-shaped analysis derived from the evidence actually passed
    in -- the fake never invents citations that are not in the prompt.

    Like a real model, it picks the SOP that matches the flagged sensor
    pattern (mechanical -> COOL, thermal-only -> TEMP, chemistry -> CHEM),
    so its answer follows the retrieved evidence rather than a fixed id."""
    sensor_lines = [ln.strip() for ln in user_prompt.splitlines() if ln.strip().startswith("- sensor:")]
    sop_lines = [ln.strip() for ln in user_prompt.splitlines() if ln.strip().startswith("- SOP [")]
    hist_lines = [ln.strip() for ln in user_prompt.splitlines() if ln.strip().startswith("- HIST [")]

    types: set[str] = set()
    for line in sensor_lines:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "sensor:":
            types.add(parts[2])
    if {"flow_rate", "vibration"} & types:
        prefer, hypothesis, confidence = "COOL", "cooling-water pump degradation", 87
    elif "temperature" in types:
        prefer, hypothesis, confidence = "TEMP", "heat-exchanger fouling", 78
    else:
        prefer, hypothesis, confidence = "CHEM", "cooling-loop chemistry excursion", 72

    chosen_sop = next((ln for ln in sop_lines if f"SOP-{prefer}" in ln), sop_lines[0] if sop_lines else None)
    chosen_hist = next((ln for ln in hist_lines if "HIST-2026-0141" in ln), hist_lines[0] if hist_lines else None)

    parts = [f"Most likely cause: {hypothesis} (confidence {confidence}%).", "", "Evidence:"]
    for line in sensor_lines[:4]:
        parts.append(f"- {line} [sensor reading]")
    if chosen_sop:
        parts.append(f"- {chosen_sop} [SOP citation]")
    if chosen_hist:
        parts.append(f"- {chosen_hist} [historical incident]")
    if not sop_lines and not hist_lines:
        parts.append("- The evidence is insufficient for a confident conclusion; human review required.")
    return "\n".join(parts)
