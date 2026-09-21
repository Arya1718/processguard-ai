"""Shared helpers for tests that exercise the REAL OIDC flow (Prompt 7).

Tokens are minted by the local OIDC provider's API-key-protected test
endpoint -- the same RS256 keys, claims, and lifetimes the real authorize
endpoint issues, so these tests exercise production-shaped tokens without
driving the browser flow.

Requires the live compose stack (pytest -m integration):
    docker compose up -d
"""
from __future__ import annotations

import os

import httpx

PROVIDER = os.environ.get("PGAI_TEST_PROVIDER", "http://localhost:8090")
MIDDLEWARE = os.environ.get("PGAI_TEST_MIDDLEWARE", "http://localhost:8080/api/v1")
AGENT_SERVICE = os.environ.get("PGAI_TEST_AGENTSERVICE", "http://localhost:8001/api/v1")
# Matches OIDC_API_KEY in .env.example / docker-compose.yml (local demo only).
API_KEY = os.environ.get("PGAI_TEST_OIDC_API_KEY", "oidc-dev-key-change-me")

SITE_12 = "11111111-1111-1111-1111-111111111111"
SITE_07 = "22222222-2222-2222-2222-222222222222"

OPERATOR_S12 = "operator@site12.demo"
MAINTENANCE_S12 = "maintenance@site12.demo"
MANAGER_S12 = "manager@site12.demo"
OPERATOR_S07 = "operator@site07.demo"


def mint(email: str, *, claims: dict | None = None, expired: bool = False) -> str:
    """Mint an access token from the provider's test endpoint.

    `expired=True` (or `claims={"exp": <epoch>}`) produces a token that is
    correctly signed but past its lifetime -- exactly what the rejection
    tests need.
    """
    body: dict = {"email": email}
    if expired:
        body["claims"] = {"exp": 1}
    elif claims:
        body["claims"] = claims
    response = httpx.post(
        f"{PROVIDER}/test/tokens",
        json=body,
        headers={"X-Api-Key": API_KEY},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
