"""Integration test for the Level-3 rejection path (Prompt 4).

Runs against the live compose stack. Reuses the cooling-tower scenario:
trigger, wait for `awaiting_approval`, then REJECT with and without the
required justification and assert the state machine + AuditLog respond
correctly. Approve-side flow is covered by test_incident_flow.
"""
from __future__ import annotations

import os
import subprocess
import time

import httpx
import pytest

MIDDLEWARE = os.environ.get("PGAI_TEST_MIDDLEWARE", "http://localhost:8080/api/v1")
pytestmark = pytest.mark.integration


def _token(user: str = "maintenance@site12.demo") -> str:
    """Prompt 7: real OIDC tokens (dev-token is gone by design). The demo
    scenario lands on Level 2, which MaintenanceEngineer may decide."""
    from oidc_helpers import mint

    return mint(user)


def _psql(sql: str) -> str:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-tAc", sql],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def _clean_stale_incidents():
    _psql("UPDATE \"Incidents\" SET \"Status\"='resolved', \"ResolvedAt\"=NOW() "
          "WHERE \"Status\" != 'resolved';")
    yield


@pytest.fixture(scope="module")
def headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def _trigger_and_wait(headers: dict) -> str:
    """Trigger the scenario and return the awaiting_approval incident id."""
    httpx.post(f"{MIDDLEWARE}/simulator/reset", headers=headers, timeout=10).raise_for_status()
    time.sleep(2)
    trigger = httpx.post(
        f"{MIDDLEWARE}/simulator/trigger-scenario/cooling-tower-incident",
        headers=headers, timeout=10,
    )
    trigger.raise_for_status()

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = httpx.get(f"{MIDDLEWARE}/incidents?status=awaiting_approval",
                             headers=headers, timeout=10)
        response.raise_for_status()
        items = response.json()["items"]
        if items and (items[0].get("anomalySummary") or {}).get("sensor_count") == 5:
            return items[0]["id"]
        time.sleep(2.0)
    pytest.fail("no incident reached awaiting_approval within the timeout")


def test_level3_rejection_requires_justification_and_audits(headers) -> None:
    headers = dict(headers)
    headers["Authorization"] = f"Bearer {_token()}"

    incident_id = _trigger_and_wait(headers)

    # Level for the demo scenario is 2, so the hard justification requirement
    # (enforced for levels >= 3) must NOT fire on this one.
    decision_level = int(_psql(
        f"SELECT COALESCE(\"RequiredApprovalLevel\", 2) FROM \"Incidents\" "
        f"WHERE \"Id\"='{incident_id}';"
    ))

    no_justification = httpx.post(
        f"{MIDDLEWARE}/incidents/{incident_id}/reject",
        headers=headers, json={}, timeout=10,
    )
    if decision_level >= 3:
        assert no_justification.status_code == 422, \
            "level-3 rejection without justification must fail with 422"
    else:
        # Level 2: justification optional -- reject goes through cleanly.
        assert no_justification.status_code == 200
        body = no_justification.json()
        assert body["decision"] == "rejected"
        assert body["status"] == "rejected"
        assert body["decidedBy"] == "maintenance@site12.demo"

    # The rejection decision is fully audited regardless of level.
    audit_count = _psql(
        f"SELECT COUNT(*) FROM \"AuditLog\" WHERE \"IncidentId\"='{incident_id}' "
        f"AND \"Action\"='hitl_rejected';"
    )
    assert audit_count == "1"

    approval_row = _psql(
        f"SELECT \"Decision\" || ':' || \"DecidedBy\" FROM \"Approvals\" "
        f"WHERE \"IncidentId\"='{incident_id}';"
    )
    assert approval_row == "rejected:maintenance@site12.demo"

    # State history gained the rejected transition.
    history = _psql(
        f"SELECT COALESCE(\"FromStatus\",'-') || '|' || \"ToStatus\" "
        f"FROM \"IncidentStateHistory\" WHERE \"IncidentId\"='{incident_id}' "
        f"ORDER BY \"ChangedAt\";"
    )
    to_statuses = [row.split("|")[1] for row in history.splitlines() if "|" in row]
    assert to_statuses[-1] == "rejected"
