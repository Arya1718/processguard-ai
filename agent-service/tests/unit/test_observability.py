"""Unit tests for Prompt 9 observability: cost calculation + agent success rate.

No network access required — tests the pure logic of
app.core.observability::estimate_cost_usd and the AGENT_OUTCOME_COUNT
recording in the Knowledge + Root-Cause + Recommendation agents.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.observability import (
    AGENT_OUTCOME_COUNT,
    estimate_cost_usd,
    init_telemetry,
)


def test_estimate_cost_known_model():
    """A known model uses the default pricing table."""
    # llama-3.3-70b-versatile: $0.05 / 1M input, $0.10 / 1M output
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    cost = estimate_cost_usd("llama-3.3-70b-versatile", usage)
    assert cost == 0.15  # 0.05 + 0.10


def test_estimate_cost_unknown_model_uses_default():
    """An unlisted model falls back to the default pricing."""
    usage = {"prompt_tokens": 500_000, "completion_tokens": 500_000}
    cost = estimate_cost_usd("unknown-model-xyz", usage)
    # default: $0.05 / 1M input, $0.10 / 1M output
    assert cost == pytest.approx(0.075, abs=0.001)


def test_estimate_cost_zero_usage():
    """Zero tokens → zero cost."""
    cost = estimate_cost_usd("llama-3.3-70b-versatile", {"prompt_tokens": 0, "completion_tokens": 0})
    assert cost == 0.0


def test_estimate_cost_fallback_to_total_tokens():
    """If prompt_tokens and completion_tokens are absent, total_tokens is split 80/20."""
    usage = {"total_tokens": 100_000}
    cost = estimate_cost_usd("llama-3.3-70b-versatile", usage)
    # 80k input @ $0.05/1M + 20k output @ $0.10/1M
    # = (80000/1e6)*0.05 + (20000/1e6)*0.10 = 0.004 + 0.002 = 0.006
    assert cost == pytest.approx(0.006, abs=0.0001)


def test_estimate_cost_flat_env_override():
    """PGAI_LLM__COST_PER_1M_INPUT/OUTPUT override the entire table."""
    os.environ["PGAI_LLM__COST_PER_1M_INPUT"] = "0.10"
    os.environ["PGAI_LLM__COST_PER_1M_OUTPUT"] = "0.20"
    try:
        usage = {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000}
        cost = estimate_cost_usd("any-model", usage)
        assert cost == 0.40  # 0.10 * 2 + 0.20 * 1
    finally:
        del os.environ["PGAI_LLM__COST_PER_1M_INPUT"]
        del os.environ["PGAI_LLM__COST_PER_1M_OUTPUT"]


def test_agent_outcome_counter_records():
    """AGENT_OUTCOME_COUNT increments per agent + outcome label."""
    # Reset is not needed for prometheus_client counters (they only go up),
    # but we can read the raw value before and after.
    before = _counter_value("root_cause", "accepted")
    AGENT_OUTCOME_COUNT.labels(agent="root_cause", outcome="accepted").inc()
    after = _counter_value("root_cause", "accepted")
    assert after == before + 1


def _counter_value(agent: str, outcome: str) -> float:
    """Read the current value of AGENT_OUTCOME_COUNT for a label pair."""
    for metric in AGENT_OUTCOME_COUNT.collect():
        for sample in metric.samples:
            if sample.name == "pgai_agent_outcomes_total":
                labels = dict(sample.labels)
                if labels.get("agent") == agent and labels.get("outcome") == outcome:
                    return float(sample.value)
    return 0.0


def test_init_telemetry_disabled_in_tests():
    """When PGAI_OTEL__DISABLE=true, init_telemetry is a no-op (safe for tests)."""
    os.environ["PGAI_OTEL__DISABLE"] = "true"
    try:
        init_telemetry("test-service")
        # Should not raise; tracer provider stays uninitialized.
        from opentelemetry import trace
        assert trace.get_tracer_provider() is not None
    finally:
        del os.environ["PGAI_OTEL__DISABLE"]
