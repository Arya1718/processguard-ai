"""RBAC + isolation integration tests against the LIVE compose stack (Prompt 7).

Proves, at the API level (not through any UI):
  * both backend layers independently reject missing / expired / tampered tokens
    (the .NET middleware is tested directly, the Python agent-service is tested
    directly -- each layer stands on its own);
  * incident data is site-scoped: a Site 07 user cannot read a Site 12
    incident by id, even with a known/guessed UUID;
  * the role -> HITL-level policy is enforced on the approval endpoints
    (Operator 403 on Level 2; MaintenanceEngineer ok on 2, 403 on 3;
    PlantManager ok on both) with 403s, never misleading 404s;
  * the dev-token stub from Prompt 1 is really gone.

Requires: `docker compose up -d` with the stack healthy.
Run:  cd agent-service && python -m pytest tests/test_rbac_auth.py -v
"""
from __future__ import annotations

import subprocess
import uuid

import httpx
import pytest

from oidc_helpers import (
    AGENT_SERVICE,
    MAINTENANCE_S12,
    MIDDLEWARE,
    OPERATOR_S07,
    OPERATOR_S12,
    MANAGER_S12,
    SITE_07,
    SITE_12,
    bearer,
    mint,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _craft_incident(required_level: int) -> str:
    """Insert a deterministic awaiting_approval incident directly in Postgres.

    Approval decisions are one-shot (the state machine moves on), so each
    approval test crafts its own incident rather than racing the live
    pipeline. Site 12 / first equipment / high severity.
    """
    incident_id = str(uuid.uuid4())
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-c",
         f'INSERT INTO "Incidents" ("Id","Name","EquipmentId","SiteId","Status",'
         f'"Severity","DetectedAt","CreatedAt","UpdatedAt","RequiredApprovalLevel","HitlLevel","RiskSeverity") '
         f"VALUES ('{incident_id}','RBAC test L{required_level}',"
         f"(SELECT \"Id\" FROM \"Equipment\" WHERE \"SiteId\"='{SITE_12}' LIMIT 1),"
         f"'{SITE_12}','awaiting_approval','high',NOW(),NOW(),NOW(),{required_level},"
         f"{required_level},'high');"],
        check=True, capture_output=True, timeout=60,
    )
    return incident_id


def _delete_incident(incident_id: str) -> None:
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-c",
         f'DELETE FROM "Incidents" WHERE "Id"=\'{incident_id}\';'],
        check=False, capture_output=True, timeout=60,
    )


@pytest.fixture
def crafted(request):
    """Craft an incident with the given HITL level; always clean up."""
    level = request.param
    incident_id = _craft_incident(level)
    yield incident_id
    _delete_incident(incident_id)


# ---------------------------------------------------------------------------
# layer-independent token validation
# ---------------------------------------------------------------------------

class TestTokenValidationBothLayers:
    def test_python_rejects_missing_token(self):
        response = httpx.get(f"{AGENT_SERVICE}/incidents", timeout=15)
        assert response.status_code == 401

    def test_python_rejects_expired_token(self):
        response = httpx.get(f"{AGENT_SERVICE}/incidents",
                             headers=bearer(mint(MAINTENANCE_S12, expired=True)), timeout=15)
        assert response.status_code == 401

    def test_python_rejects_tampered_token(self):
        token = mint(MAINTENANCE_S12)
        response = httpx.get(f"{AGENT_SERVICE}/incidents",
                             headers=bearer(token[:-8] + "deadbeef"), timeout=15)
        assert response.status_code == 401

    def test_python_accepts_valid_token(self):
        response = httpx.post(f"{AGENT_SERVICE}/auth/validate",
                              json={"token": mint(MAINTENANCE_S12)}, timeout=15)
        assert response.status_code == 200
        assert response.json()["valid"] is True

    def test_middleware_rejects_missing_token(self):
        response = httpx.get(f"{MIDDLEWARE}/incidents", timeout=15)
        assert response.status_code == 401

    def test_middleware_rejects_expired_token(self):
        response = httpx.get(f"{MIDDLEWARE}/incidents",
                             headers=bearer(mint(MAINTENANCE_S12, expired=True)), timeout=15)
        assert response.status_code == 401

    def test_middleware_rejects_tampered_token(self):
        token = mint(MAINTENANCE_S12)
        response = httpx.get(f"{MIDDLEWARE}/incidents",
                             headers=bearer(token[:-8] + "deadbeef"), timeout=15)
        assert response.status_code == 401

    def test_middleware_accepts_valid_token(self):
        response = httpx.get(f"{MIDDLEWARE}/incidents",
                             headers=bearer(mint(MAINTENANCE_S12)), timeout=15)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# site isolation
# ---------------------------------------------------------------------------

class TestSiteIsolation:
    def test_incident_lists_are_site_scoped(self):
        for email, site in [(OPERATOR_S12, SITE_12), (OPERATOR_S07, SITE_07)]:
            response = httpx.get(f"{MIDDLEWARE}/incidents",
                                 headers=bearer(mint(email)), timeout=15)
            assert response.status_code == 200
            for item in response.json()["items"]:
                assert item["siteId"] == site, f"{email} saw a foreign-site incident"

    def test_cross_site_detail_is_403_not_data(self):
        """A Site 07 user fetching a Site 12 incident BY KNOWN ID must be
        refused -- 403 policy refusal, not a filtered empty list."""
        site12_incident = _craft_incident(required_level=2)
        try:
            response = httpx.get(f"{MIDDLEWARE}/incidents/{site12_incident}",
                                 headers=bearer(mint(OPERATOR_S07)), timeout=15)
            assert response.status_code == 403
            assert SITE_12 not in response.text  # no site-12 data leaks in the error either
        finally:
            _delete_incident(site12_incident)

    def test_same_site_detail_is_allowed(self):
        incident_id = _craft_incident(required_level=2)
        try:
            response = httpx.get(f"{MIDDLEWARE}/incidents/{incident_id}",
                                 headers=bearer(mint(OPERATOR_S12)), timeout=15)
            assert response.status_code == 200
            assert response.json()["id"] == incident_id
        finally:
            _delete_incident(incident_id)


# ---------------------------------------------------------------------------
# role -> HITL approval policy
# ---------------------------------------------------------------------------

class TestApprovalPolicy:
    @pytest.mark.parametrize("crafted", [2], indirect=True)
    def test_level2_operator_is_403(self, crafted):
        response = httpx.post(f"{MIDDLEWARE}/incidents/{crafted}/approve",
                              json={}, headers=bearer(mint(OPERATOR_S12)), timeout=15)
        assert response.status_code == 403

    @pytest.mark.parametrize("crafted", [2], indirect=True)
    def test_level2_maintenance_succeeds(self, crafted):
        response = httpx.post(f"{MIDDLEWARE}/incidents/{crafted}/approve",
                              json={}, headers=bearer(mint(MAINTENANCE_S12)), timeout=15)
        assert response.status_code == 200

    @pytest.mark.parametrize("crafted", [3], indirect=True)
    def test_level3_maintenance_is_403(self, crafted):
        response = httpx.post(f"{MIDDLEWARE}/incidents/{crafted}/approve",
                              json={}, headers=bearer(mint(MAINTENANCE_S12)), timeout=15)
        assert response.status_code == 403

    @pytest.mark.parametrize("crafted", [3], indirect=True)
    def test_level3_manager_succeeds(self, crafted):
        response = httpx.post(f"{MIDDLEWARE}/incidents/{crafted}/approve",
                              json={}, headers=bearer(mint(MANAGER_S12)), timeout=15)
        assert response.status_code == 200

    @pytest.mark.parametrize("crafted", [2], indirect=True)
    def test_level2_manager_also_succeeds(self, crafted):
        response = httpx.post(f"{MIDDLEWARE}/incidents/{crafted}/approve",
                              json={}, headers=bearer(mint(MANAGER_S12)), timeout=15)
        assert response.status_code == 200

    @pytest.mark.parametrize("crafted", [2], indirect=True)
    def test_cross_site_role_cannot_approve(self, crafted):
        response = httpx.post(f"{MIDDLEWARE}/incidents/{crafted}/approve",
                              json={}, headers=bearer(mint(OPERATOR_S07)), timeout=15)
        assert response.status_code == 403

    @pytest.mark.parametrize("crafted", [2], indirect=True)
    def test_approval_records_identity_and_level(self, crafted):
        httpx.post(f"{MIDDLEWARE}/incidents/{crafted}/approve",
                   json={}, headers=bearer(mint(MAINTENANCE_S12)), timeout=15)
        detail = httpx.get(f"{MIDDLEWARE}/incidents/{crafted}",
                           headers=bearer(mint(MAINTENANCE_S12)), timeout=15).json()
        # The Action Agent may already have executed between approve and this
        # read -- anything at or past 'approved' proves the decision landed.
        assert detail["status"] in ("approved", "action_taken", "resolved")
        approvals = detail.get("approvals") or []
        assert approvals and approvals[0]["decidedBy"] == MAINTENANCE_S12
        assert approvals[0]["decision"] == "approved"


# ---------------------------------------------------------------------------
# the Prompt 1 stub is gone
# ---------------------------------------------------------------------------

class TestDevTokenRemoved:
    def test_dev_token_endpoint_no_longer_exists(self):
        response = httpx.post(f"{MIDDLEWARE}/auth/dev-token",
                              json={"userName": "x"}, timeout=15)
        assert response.status_code in (404, 405)
