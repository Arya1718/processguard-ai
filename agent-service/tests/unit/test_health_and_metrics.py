"""Unit tests for app/api/health.py and app/api/metrics.py."""
from __future__ import annotations

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")

from app.main import app


@pytest.fixture
@pytest.mark.asyncio
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


class TestHealthEndpoints:
    async def test_live(self, client):
        resp = await client.get("/api/v1/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_ready_healthy(self, client, monkeypatch):
        async def _fake_pg(dsn):
            return True
        async def _fake_redis(url):
            return True
        monkeypatch.setattr("app.api.health.check_postgres", _fake_pg)
        monkeypatch.setattr("app.api.health.check_redis", _fake_redis)
        resp = await client.get("/api/v1/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["dependencies"]["postgres"] == "ok"
        assert data["dependencies"]["redis"] == "ok"

    async def test_ready_degraded(self, client, monkeypatch):
        async def _fake_pg(dsn):
            return False
        async def _fake_redis(url):
            return False
        monkeypatch.setattr("app.api.health.check_postgres", _fake_pg)
        monkeypatch.setattr("app.api.health.check_redis", _fake_redis)
        resp = await client.get("/api/v1/health/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["dependencies"]["postgres"] == "failed"
        assert data["dependencies"]["redis"] == "failed"

    async def test_startup_not_complete(self, client):
        from app.api import health
        health._startup_state["complete"] = False
        resp = await client.get("/api/v1/health/startup")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_started"
        health._startup_state["complete"] = False

    async def test_startup_complete(self, client):
        from app.api import health
        health._startup_state["complete"] = True
        resp = await client.get("/api/v1/health/startup")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        health._startup_state["complete"] = False


class TestMetricsMiddleware:
    def test_metric_paths_set(self):
        from app.api.metrics import METRIC_PATHS
        assert "/api/v1/health/live" in METRIC_PATHS
        assert "/metrics" in METRIC_PATHS

    @pytest.mark.asyncio
    async def test_record_error(self):
        from app.api.metrics import _record_error
        await _record_error("/test", "GET")
