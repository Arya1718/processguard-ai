"""Integration tests for Prompt 9 telemetry API endpoints.

Tests use a mocked DB pool (no Postgres required) to verify the JSON contract
of GET /api/v1/telemetry/summary and GET /api/v1/incidents/{id}/cost.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.security import Principal
from app.api import telemetry as telemetry_module


@pytest.fixture
def mock_principal():
    return Principal(
        token="test-token",
        payload={
            "site_id": "11111111-1111-1111-1111-111111111111",
            "roles": ["incident_reader"],
            "preferred_username": "test-user",
        },
    )


@pytest.fixture
def mock_pool():
    """A mock asyncpg pool that returns canned query results."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=conn)
    return pool


@pytest.fixture(autouse=True)
def patch_pool(mock_pool):
    """Patch get_pool in both app.core.db and app.api.telemetry modules."""
    with patch.object(telemetry_module, "get_pool", return_value=mock_pool):
        yield


@pytest.fixture(autouse=True)
def patch_uuid():
    with patch("app.api.telemetry._parse_uuid", side_effect=lambda v: v):
        yield


class TestTelemetrySummary:
    @pytest.mark.asyncio
    async def test_summary_structure(self, mock_pool, mock_principal):
        """The /telemetry/summary endpoint returns the expected JSON keys."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[
                {
                    "Status": "investigating",
                    "Severity": "high",
                    "HitlLevel": 2,
                    "RequiredApprovalLevel": 2,
                    "RootCauseHypothesis": "pump failure",
                    "Confidence": 85,
                    "RiskSeverity": "high",
                    "DetectedAt": datetime.now(timezone.utc),
                    "ResolvedAt": None,
                },
                {
                    "Status": "approved",
                    "Severity": "high",
                    "HitlLevel": 3,
                    "RequiredApprovalLevel": 3,
                    "RootCauseHypothesis": None,
                    "Confidence": None,
                    "RiskSeverity": None,
                    "DetectedAt": datetime.now(timezone.utc),
                    "ResolvedAt": datetime.now(timezone.utc),
                },
            ]
        )

        result = await telemetry_module.telemetry_summary(
            window_hours=24, principal=mock_principal
        )

        assert result["window_hours"] == 24
        assert result["site_id"] == "11111111-1111-1111-1111-111111111111"
        assert result["incidents"]["total"] == 2
        assert "investigating" in result["incidents"]["by_status"]
        assert result["incidents"]["by_status"]["investigating"] == 1
        assert result["incidents"]["by_status"]["approved"] == 1
        assert result["root_cause_agent"]["accepted"] == 1
        assert result["root_cause_agent"]["success_rate"] == pytest.approx(1.0)
        assert result["root_cause_agent"]["confidence_avg"] == 85.0

    @pytest.mark.asyncio
    async def test_summary_empty_incidents(self, mock_pool, mock_principal):
        """When no incidents match the window, the summary still returns valid structure."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[]
        )

        result = await telemetry_module.telemetry_summary(
            window_hours=1, principal=mock_principal
        )

        assert result["incidents"]["total"] == 0
        assert result["root_cause_agent"]["success_rate"] is None
        assert result["llm"]["total_cost_usd"] >= 0
        assert result["llm"]["cost_per_incident_usd"] == 0


class TestIncidentCost:
    @pytest.mark.asyncio
    async def test_cost_endpoint_returns_404_for_invalid_id(self, mock_principal):
        """A malformed incident ID returns 404."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await telemetry_module.incident_cost(
                incident_id="not-a-uuid", principal=mock_principal
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cost_endpoint_404_for_missing_incident(self, mock_pool, mock_principal):
        """A valid-but-unknown UUID returns 404."""
        from fastapi import HTTPException
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value=None
        )
        with pytest.raises(HTTPException) as exc_info:
            await telemetry_module.incident_cost(
                incident_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                principal=mock_principal,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cost_endpoint_returns_cost_breakdown(self, mock_pool, mock_principal):
        """A found incident returns cost fields with the expected keys."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "Id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "SiteId": "11111111-1111-1111-1111-111111111111",
                "RootCauseHypothesis": "test hypothesis",
                "Confidence": 85,
                "Status": "investigating",
            }
        )

        result = await telemetry_module.incident_cost(
            incident_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            principal=mock_principal,
        )

        assert result["incident_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        assert result["status"] == "investigating"
        assert result["confidence"] == 85
        assert result["has_root_cause"] is True
        assert "cost_usd" in result
        assert "total_tokens" in result
        assert "llm_calls" in result
