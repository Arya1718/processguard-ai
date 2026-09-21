"""Unit tests for app/api/ask.py (grounded Q&A endpoint)."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
_POLICY_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "rbac-policy.json"))
os.environ.setdefault("PGAI_RBAC__POLICYFILE", _POLICY_PATH)

from app.main import app
from app.core.security import Principal, require_principal

TEST_SITE = "site-A"
TEST_USERNAME = "test_operator"
TEST_TOKEN = "test-token"


def _make_principal() -> Principal:
    return Principal(
        token=TEST_TOKEN,
        payload={
            "site_id": TEST_SITE,
            "preferred_username": TEST_USERNAME,
            "roles": ["MaintenanceEngineer"],
        },
    )


@pytest.fixture
@pytest.mark.asyncio
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _patch_auth():
    def _fake_require_principal():
        return _make_principal()
    app.dependency_overrides[require_principal] = _fake_require_principal
    yield
    app.dependency_overrides.pop(require_principal, None)


class TestAskEndpoint:
    @pytest.fixture
    def _incident(self, monkeypatch):
        from app.core import db

        async def _fake_get(_ii):
            return {
                "id": "inc-1", "siteId": TEST_SITE, "status": "resolved",
                "rootCause": {"hypothesis": "bearing failure", "confidence": 90, "citedEvidence": []},
                "retrievedEvidence": {"sop_chunks": [], "matched_history": []},
                "risk": {"severity": "high", "hitlLevel": 2, "consequences": []},
                "recommendation": {"rationale": "replaced bearing", "actions": []},
                "anomalySummary": {"sensors": [{"sensor_type": "vibration", "value": 8.5, "unit": "mm/s"}]},
            }
        monkeypatch.setattr("app.api.ask.get_incident", _fake_get)
        return _fake_get

    async def test_ask_success(self, client, _incident, monkeypatch):
        from app.core import llm_client
        fake_resp = MagicMock()
        # No specific citation that would fail _validate_point — just narrative
        fake_resp.content = "The root cause was bearing failure due to vibration spike."
        fake_resp.model = "gpt-4"
        fake_resp.provider = "openai"

        async def _fake_gen(*args, **kwargs):
            return fake_resp
        monkeypatch.setattr("app.api.ask.get_llm_client", lambda: type("C", (), {"generate": _fake_gen})())

        resp = await client.post("/api/v1/incidents/inc-1/ask", json={"question": "what was the root cause?"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["question"] == "what was the root cause?"
        assert "bearing" in data["answer"].lower()

    async def test_ask_empty_question(self, client, _incident):
        resp = await client.post("/api/v1/incidents/inc-1/ask", json={"question": ""})
        assert resp.status_code == 422

    async def test_ask_question_too_long(self, client, _incident):
        resp = await client.post("/api/v1/incidents/inc-1/ask", json={"question": "x" * 501})
        assert resp.status_code == 422

    async def test_ask_incident_not_found(self, client, monkeypatch):
        monkeypatch.setattr("app.api.ask.get_incident", AsyncMock(return_value=None))
        resp = await client.post("/api/v1/incidents/nonexistent/ask", json={"question": "what?"})
        assert resp.status_code == 404

    async def test_ask_cross_site(self, client, monkeypatch):
        async def _fake_get(_ii):
            return {"id": "inc-1", "siteId": "other-site", "status": "open"}
        monkeypatch.setattr("app.api.ask.get_incident", _fake_get)
        resp = await client.post("/api/v1/incidents/inc-1/ask", json={"question": "what?"})
        assert resp.status_code == 403

    async def test_ask_fallback_insufficient(self, client, _incident, monkeypatch):
        """When the model says it has no evidence, the honest fallback is returned."""
        from app.core import llm_client
        fake_resp = MagicMock()
        fake_resp.content = "I don't have evidence for that."
        fake_resp.model = "gpt-4"
        fake_resp.provider = "openai"

        async def _fake_gen(*args, **kwargs):
            return fake_resp
        monkeypatch.setattr("app.api.ask.get_llm_client", lambda: type("C", (), {"generate": _fake_gen})())

        resp = await client.post("/api/v1/incidents/inc-1/ask", json={"question": "how tall is the moon?"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "I don't have evidence for that."
        assert data["grounded"] is False
