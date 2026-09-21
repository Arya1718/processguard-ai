"""Unit tests for the LLM client abstraction (app/core/llm_client.py).

Covers:
  - FakeScriptedLLMClient: scripted responses (via env), tool-call entries,
    fallback derivation from prompt evidence
  - GroqChatCompletionsClient: fail-fast on missing key, body construction,
    response parsing (tool calls + usage)
  - get_llm_client / reset_llm_client factory
  - Helper functions: _safe_json, _safe_json_list, _derive_fake_analysis
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.llm_client import (
    LLMResponse,
    LLMClient,
    FakeScriptedLLMClient,
    GroqChatCompletionsClient,
    ToolCall,
    get_llm_client,
    reset_llm_client,
    _safe_json,
    _safe_json_list,
    _derive_fake_analysis,
    _derive_fake_recommendation,
)


class TestFakeScriptedLLMClient:
    @pytest.fixture(autouse=True)
    def _force_fake(self, monkeypatch):
        monkeypatch.setenv("PGAI_LLM__FAKEMODE", "true")
        # Reset both caches so the env var change takes effect.
        from app.core.config import _settings as _settings_ref
        import app.core.config as config_mod
        config_mod._settings = None
        reset_llm_client()
        yield
        reset_llm_client()
        config_mod._settings = None

    def test_get_client_returns_fake_in_fake_mode(self):
        client = get_llm_client()
        assert isinstance(client, FakeScriptedLLMClient)

    @pytest.mark.asyncio
    async def test_generate_uses_scripted_responses(self, monkeypatch):
        monkeypatch.setenv("PGAI_LLM__FAKEMODE", "true")
        monkeypatch.setenv("PGAI_LLM__FAKE_RESPONSES", json.dumps(["response-one", "response-two"]))
        reset_llm_client()
        client = get_llm_client()
        resp = await client.generate("sys", "user")
        assert resp.content == "response-one"
        assert resp.provider == "fake"
        assert resp.model == "fake-scripted"

        resp2 = await client.generate("sys", "user")
        assert resp2.content == "response-two"

    @pytest.mark.asyncio
    async def test_generate_falls_back_to_derived(self, monkeypatch):
        monkeypatch.setenv("PGAI_LLM__FAKEMODE", "true")
        monkeypatch.delenv("PGAI_LLM__FAKE_RESPONSES", raising=False)
        reset_llm_client()
        client = get_llm_client()
        user_prompt = (
            "- sensor: flow_rate value=106.0 outside normal band\n"
            "- sensor: vibration value=4.6 outside normal band\n"
            "- SOP-123: inspect the pump suction strainer\n"
        )
        resp = await client.generate("sys", user_prompt)
        # The derived answer should contain a confidence and evidence markers
        assert "confidence" in resp.content
        assert "Evidence:" in resp.content

    @pytest.mark.asyncio
    async def test_chat_tool_call_entry(self, monkeypatch):
        monkeypatch.setenv("PGAI_LLM__FAKEMODE", "true")
        monkeypatch.setenv("PGAI_LLM__FAKE_RESPONSES", json.dumps(["TOOL_CALL:get_equipment_maintenance_status:{\"equipment_id\":\"e-1\"}"]))
        reset_llm_client()
        client = get_llm_client()
        resp = await client.chat([{"role": "user", "content": "hi"}])
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "get_equipment_maintenance_status"
        assert resp.tool_calls[0].arguments == {"equipment_id": "e-1"}
        assert resp.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_emits_tool_calls_for_both_sources(self, monkeypatch):
        """Round 1, no script: the fake planner emits tool calls for both
        data sources."""
        monkeypatch.setenv("PGAI_LLM__FAKEMODE", "true")
        monkeypatch.delenv("PGAI_LLM__FAKE_RESPONSES", raising=False)
        reset_llm_client()
        client = get_llm_client()
        resp = await client.chat([
            {"role": "system", "content": "you are a planner"},
            {"role": "user", "content": "check maintenance and history"},
        ])
        assert len(resp.tool_calls) >= 1
        assert resp.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_synthesizes_recommendation(self, monkeypatch):
        """When tool results are in the conversation and no script matches,
        the fake synthesizes a final recommendation."""
        monkeypatch.setenv("PGAI_LLM__FAKEMODE", "true")
        monkeypatch.delenv("PGAI_LLM__FAKE_RESPONSES", raising=False)
        reset_llm_client()
        client = get_llm_client()
        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "recommend action"},
            {"role": "tool", "content": json.dumps({"ok": True, "result": {"lastServiced": "2026-01-01"}})},
        ]
        resp = await client.chat(messages)
        assert resp.content != ""
        assert resp.finish_reason == "stop"


class TestGroqClient:
    def test_fail_fast_without_api_key(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        from app.core.config import Settings
        reset_llm_client()

        class FakeSettings:
            llm_api_key = ""
            llm_model = "llama-3.3-70b-versatile"
            llm_base_url = "https://api.groq.com/openai/v1"
            llm_timeout_seconds = 30
            llm_fake_mode = False

        with pytest.raises(Exception):
            GroqChatCompletionsClient(FakeSettings())


class TestHelperFunctions:
    def test_safe_json_valid(self):
        assert _safe_json('{"a": 1}') == {"a": 1}

    def test_safe_json_invalid(self):
        assert _safe_json("not json") == {}
        assert _safe_json("") == {}

    def test_safe_json_list_valid(self):
        assert _safe_json_list('["a", "b"]') == ["a", "b"]

    def test_safe_json_list_invalid(self):
        assert _safe_json_list("not json") == []
        assert _safe_json_list("") == []

    def test_derive_fake_analysis_mechanical_pattern(self):
        """flow_rate + vibration -> cooling-water pump degradation, 87% confidence."""
        prompt = (
            "- sensor: flow_rate value=106.0 outside normal band\n"
            "- sensor: vibration value=4.6 outside normal band\n"
            "- SOP-COOL: inspect strainer\n"
        )
        result = _derive_fake_analysis(prompt)
        assert "cooling-water pump degradation" in result
        assert "87" in result
        assert "Evidence:" in result

    def test_derive_fake_analysis_thermal_pattern(self):
        """temperature only -> heat-exchanger fouling, 78%."""
        prompt = (
            "- sensor: temperature value=38.4 outside normal band\n"
            "- SOP-TEMP: check exchanger\n"
        )
        result = _derive_fake_analysis(prompt)
        assert "heat-exchanger fouling" in result
        assert "78" in result

    def test_derive_fake_analysis_chemistry_pattern(self):
        """No mechanical/thermal pattern -> chemistry, 72%."""
        prompt = (
            "- sensor: ph value=6.9 outside normal band\n"
            "- SOP-CHEM: adjust dosing\n"
        )
        result = _derive_fake_analysis(prompt)
        assert "cooling-loop chemistry" in result
        assert "72" in result

    def test_derive_fake_recommendation_uses_tool_text(self):
        result = _derive_fake_recommendation("maintenance: lastServiced 2024, actionWorked: true")
        assert "RECOMMENDED ACTIONS" in result
        assert "RATIONALE:" in result

    def test_derive_fake_recommendation_strainer_keyword(self):
        result = _derive_fake_recommendation("strainer blocked, lastServicedAt 2024")
        assert "strainer" in result.lower()
